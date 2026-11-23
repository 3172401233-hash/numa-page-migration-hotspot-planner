"""Time-sliced NUMA hot-page migration planner.

The planner consumes offline access traces window by window and emits,
for each window, the set of pages to migrate, the expected benefit, a
thrash-risk estimate and the reasons pages could not be migrated.
"""

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .model import (
    BASE_PAGE_SIZE,
    Migration,
    Node,
    Page,
    Rejection,
    WindowPlan,
)


@dataclass
class PlannerConfig:
    local_distance: float = 1.0
    default_remote_distance: float = 2.0
    migration_cost_per_page: float = 10.0
    huge_split_cost: float = 50.0
    benefit_margin: float = 1.0
    thrash_cooldown: int = 2
    thrash_penalty: float = 2.0
    bandwidth_safety: float = 0.9


class MigrationPlanner:
    def __init__(self, config=None, distance=None):
        self.config = config or PlannerConfig()
        self.distance = distance or {}
        self._history = {}

    def dist(self, a, b):
        if a == b:
            return self.config.local_distance
        return self.distance.get((a, b), self.config.default_remote_distance)

    def plan_window(self, pages, nodes, window):
        node_map = {n.node_id: n for n in nodes}
        predicted = {n.node_id: n.remote_bandwidth_used for n in nodes}
        plan = WindowPlan(window=window)

        candidates = []
        for page in pages:
            candidate = self._evaluate(page, node_map, window, plan)
            if candidate is not None:
                candidates.append(candidate)

        candidates.sort(key=lambda c: c[3] / max(c[4], 1e-9), reverse=True)

        by_process = {}
        for candidate in candidates:
            by_process.setdefault(candidate[0].process, []).append(candidate)
        ordered = []
        while by_process:
            for process in sorted(list(by_process)):
                ordered.append(by_process[process].pop(0))
                if not by_process[process]:
                    del by_process[process]

        for page, dst, size, benefit, cost, split, relief, load in ordered:
            self._apply(page, dst, size, benefit, cost, split, relief, load,
                        node_map, predicted, plan, window)

        plan.expected_benefit = sum(m.benefit for m in plan.migrations)
        reversals = sum(1 for m in plan.migrations if m.thrash_reversal)
        if plan.migrations:
            plan.thrash_risk = reversals / float(len(plan.migrations))
        plan.predicted_remote_load = dict(predicted)
        return plan

    def _access_cost(self, page, home):
        return sum(h * self.dist(home, n)
                   for n, h in page.heat_by_node.items())

    def _migration_cost(self, page, size):
        pages = max(1, size // BASE_PAGE_SIZE)
        return (self.config.migration_cost_per_page * pages
                * page.mappings)

    def _is_reversal(self, address, src, dst, window):
        for w, s, d in self._history.get(address, []):
            if window - w <= self.config.thrash_cooldown and s == dst and d == src:
                return True
        return False

    def _evaluate(self, page, node_map, window, plan):
        cfg = self.config
        if page.pinned:
            plan.rejected.append(Rejection(page.address, "pinned"))
            return None
        if not page.heat_by_node or page.remote_heat <= 0:
            plan.rejected.append(Rejection(page.address, "no_remote_pressure"))
            return None

        best = None
        for dst in node_map:
            if dst == page.node:
                continue
            benefit = self._access_cost(page, page.node) - self._access_cost(page, dst)
            if best is None or benefit > best[1]:
                best = (dst, benefit)
        dst, benefit = best

        cost = self._migration_cost(page, page.size)
        reversal = self._is_reversal(page.address, page.node, dst, window)
        margin = cfg.benefit_margin * (cfg.thrash_penalty if reversal else 1.0)
        if benefit <= cost * margin:
            if not reversal:
                split = self._try_split(page, dst)
                if split is not None:
                    size, sbenefit, scost, relief, load = split
                    return (page, dst, size, sbenefit, scost,
                            True, relief, load)
            reason = "thrash_suppression" if reversal else "benefit_below_cost"
            plan.rejected.append(Rejection(page.address, reason))
            return None

        relief = page.remote_heat
        load = page.remote_heat_relative_to(dst)
        return (page, dst, page.size, benefit, cost, False, relief, load)

    def _try_split(self, page, dst):
        cfg = self.config
        if not page.huge or not page.sub_heat:
            return None
        peak = max(page.sub_heat)
        if peak <= 0:
            return None
        hot = [h for h in page.sub_heat if h > 0]
        size = len(hot) * BASE_PAGE_SIZE
        if size >= page.size:
            return None
        benefit = sum(hot) * self.dist(page.node, dst)
        cost = (cfg.migration_cost_per_page * len(hot) * page.mappings
                + cfg.huge_split_cost)
        if benefit <= cost * cfg.benefit_margin:
            return None
        return (size, benefit, cost, sum(hot), sum(hot))

    def _apply(self, page, dst, size, benefit, cost, split, relief, load,
               node_map, predicted, plan, window):
        cfg = self.config
        src_node = node_map[page.node]
        dst_node = node_map[dst]

        if dst_node.free_capacity < size:
            if not split:
                fallback = self._try_split(page, dst)
                if fallback is None:
                    plan.rejected.append(
                        Rejection(page.address, "insufficient_capacity"))
                    return
                size, benefit, cost, relief, load = fallback
                split = True
            if dst_node.free_capacity < size:
                plan.rejected.append(
                    Rejection(page.address, "insufficient_capacity"))
                return

        new_src = predicted[page.node] - relief
        new_dst = predicted[dst] + load
        if new_dst > dst_node.remote_bandwidth_limit * cfg.bandwidth_safety:
            plan.rejected.append(
                Rejection(page.address, "bandwidth_saturation"))
            return

        src = page.node
        reversal = self._is_reversal(page.address, src, dst, window)
        predicted[src] = max(0.0, new_src)
        predicted[dst] = new_dst
        src_node.free_capacity += size
        dst_node.free_capacity -= size
        if not split:
            page.node = dst
        self._history.setdefault(page.address, []).append((window, src, dst))
        plan.migrations.append(Migration(
            address=page.address,
            src=src_node.node_id,
            dst=dst,
            size=size,
            process=page.process,
            benefit=benefit,
            cost=cost,
            mappings_updated=page.mappings,
            split=split,
            thrash_reversal=reversal,
        ))
