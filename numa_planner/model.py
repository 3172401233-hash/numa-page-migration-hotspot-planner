"""Data model for the NUMA page migration planner.

All page access information comes from offline traces; nothing here
inspects a live system.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

BASE_PAGE_SIZE = 4096
HUGE_PAGE_SIZE = 2 * 1024 * 1024


@dataclass
class Page:
    """A single page (base or huge) observed in an offline access trace."""

    address: int
    node: int
    process: str
    heat_by_node: Dict[int, float] = field(default_factory=dict)
    shared_with: Tuple[str, ...] = ()
    pinned: bool = False
    huge: bool = False
    size: int = BASE_PAGE_SIZE
    sub_heat: List[float] = None

    @property
    def total_heat(self):
        return sum(self.heat_by_node.values())

    @property
    def remote_heat(self):
        return sum(h for n, h in self.heat_by_node.items() if n != self.node)

    @property
    def mappings(self):
        return 1 + len(self.shared_with)

    def remote_heat_relative_to(self, node):
        return sum(h for n, h in self.heat_by_node.items() if n != node)


@dataclass
class Node:
    """A NUMA node with its free capacity and remote-bandwidth budget."""

    node_id: int
    free_capacity: int
    remote_bandwidth_limit: float
    remote_bandwidth_used: float = 0.0


@dataclass
class Migration:
    address: int
    src: int
    dst: int
    size: int
    process: str
    benefit: float
    cost: float
    mappings_updated: int = 1
    split: bool = False
    thrash_reversal: bool = False


@dataclass
class Rejection:
    address: int
    reason: str


@dataclass
class WindowPlan:
    window: int
    migrations: List[Migration] = field(default_factory=list)
    rejected: List[Rejection] = field(default_factory=list)
    predicted_remote_load: Dict[int, float] = field(default_factory=dict)
    expected_benefit: float = 0.0
    thrash_risk: float = 0.0

    def reasons(self):
        return {r.address: r.reason for r in self.rejected}
