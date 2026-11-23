import unittest

from numa_planner import (
    BASE_PAGE_SIZE,
    HUGE_PAGE_SIZE,
    MigrationPlanner,
    Node,
    Page,
    PlannerConfig,
)

DISTANCE = {(0, 1): 2.0, (1, 0): 2.0}
GB = 1024 ** 3


def make_planner(**overrides):
    config = PlannerConfig(**overrides)
    return MigrationPlanner(config=config, distance=DISTANCE)


def make_nodes(used0=0.0, used1=0.0, limit=1000.0, cap=GB):
    return [
        Node(node_id=0, free_capacity=cap,
             remote_bandwidth_limit=limit, remote_bandwidth_used=used0),
        Node(node_id=1, free_capacity=cap,
             remote_bandwidth_limit=limit, remote_bandwidth_used=used1),
    ]


class SingleHotspotTest(unittest.TestCase):
    def test_hot_pages_migrate_off_saturated_node(self):
        planner = make_planner()
        pages = [
            Page(address=0x1000 * i, node=0, process="db",
                 heat_by_node={1: 100.0})
            for i in range(4)
        ]
        nodes = make_nodes(used0=900.0, used1=100.0)

        plan = planner.plan_window(pages, nodes, window=0)

        self.assertEqual(len(plan.migrations), 4)
        self.assertTrue(all(m.dst == 1 for m in plan.migrations))
        self.assertTrue(all(p.node == 1 for p in pages))
        self.assertGreater(plan.expected_benefit, 0.0)
        self.assertEqual(plan.predicted_remote_load[0], 900.0 - 400.0)
        self.assertEqual(plan.predicted_remote_load[1], 100.0)
        self.assertEqual(plan.thrash_risk, 0.0)

    def test_pinned_page_stays(self):
        planner = make_planner()
        pages = [Page(address=0x2000, node=0, process="rt",
                      heat_by_node={1: 500.0}, pinned=True)]
        plan = planner.plan_window(pages, make_nodes(), window=0)

        self.assertEqual(plan.migrations, [])
        self.assertEqual(plan.reasons()[0x2000], "pinned")
        self.assertEqual(pages[0].node, 0)


class BidirectionalThrashTest(unittest.TestCase):
    def test_reverse_migration_flagged_and_weak_flip_suppressed(self):
        planner = make_planner()
        page = Page(address=0x3000, node=0, process="a",
                    shared_with=("b",), heat_by_node={1: 100.0})
        nodes = make_nodes()

        plan1 = planner.plan_window([page], nodes, window=0)
        self.assertEqual(len(plan1.migrations), 1)
        self.assertEqual(page.node, 1)

        page.heat_by_node = {0: 100.0}
        plan2 = planner.plan_window([page], nodes, window=1)
        self.assertEqual(len(plan2.migrations), 1)
        self.assertTrue(plan2.migrations[0].thrash_reversal)
        self.assertEqual(plan2.thrash_risk, 1.0)
        self.assertEqual(page.node, 0)

        page.heat_by_node = {1: 15.0}
        plan3 = planner.plan_window([page], nodes, window=2)
        self.assertEqual(plan3.migrations, [])
        self.assertEqual(plan3.reasons()[0x3000], "thrash_suppression")
        self.assertEqual(page.node, 0)


class SharedPageTest(unittest.TestCase):
    def test_shared_page_updates_all_mappings(self):
        planner = make_planner()
        hot = Page(address=0x4000, node=0, process="a",
                   shared_with=("b", "c", "d"),
                   heat_by_node={1: 100.0})
        cold = Page(address=0x5000, node=0, process="a",
                    shared_with=("b", "c", "d"),
                    heat_by_node={1: 30.0})
        plan = planner.plan_window([hot, cold], make_nodes(), window=0)

        self.assertEqual(len(plan.migrations), 1)
        migration = plan.migrations[0]
        self.assertEqual(migration.address, 0x4000)
        self.assertEqual(migration.mappings_updated, 4)
        self.assertEqual(migration.cost, 10.0 * 4)
        self.assertEqual(plan.reasons()[0x5000], "benefit_below_cost")


class BandwidthSaturationTest(unittest.TestCase):
    def test_competing_tasks_share_target_budget(self):
        planner = make_planner()
        pages = [
            Page(address=0x1000 * i, node=0, process=proc,
                 heat_by_node={0: 40.0, 1: 60.0})
            for i, proc in enumerate(["p1", "p1", "p2"])
        ]
        nodes = make_nodes(limit=100.0)

        plan = planner.plan_window(pages, nodes, window=0)

        self.assertEqual(len(plan.migrations), 2)
        rejected = plan.reasons()
        self.assertEqual(len(rejected), 1)
        self.assertEqual(list(rejected.values())[0], "bandwidth_saturation")
        self.assertLessEqual(plan.predicted_remote_load[1],
                             100.0 * planner.config.bandwidth_safety)
        processes = {m.process for m in plan.migrations}
        self.assertEqual(processes, {"p1", "p2"})


class HugePageSplitTest(unittest.TestCase):
    def test_huge_page_split_when_capacity_short(self):
        planner = make_planner()
        sub_heat = [0.0] * (HUGE_PAGE_SIZE // BASE_PAGE_SIZE)
        for i in range(10):
            sub_heat[i] = 50.0
        page = Page(address=0x200000, node=0, process="vm",
                    heat_by_node={1: 500.0}, huge=True,
                    size=HUGE_PAGE_SIZE, sub_heat=sub_heat)
        nodes = make_nodes(cap=GB)
        nodes[1].free_capacity = 100 * BASE_PAGE_SIZE

        plan = planner.plan_window([page], nodes, window=0)

        self.assertEqual(len(plan.migrations), 1)
        migration = plan.migrations[0]
        self.assertTrue(migration.split)
        self.assertEqual(migration.size, 10 * BASE_PAGE_SIZE)
        self.assertLess(migration.size, HUGE_PAGE_SIZE)
        self.assertEqual(nodes[1].free_capacity, 90 * BASE_PAGE_SIZE)

    def test_huge_page_migrates_whole_when_capacity_allows(self):
        planner = make_planner()
        page = Page(address=0x200000, node=0, process="vm",
                    heat_by_node={1: 100000.0}, huge=True,
                    size=HUGE_PAGE_SIZE)
        plan = planner.plan_window([page], make_nodes(), window=0)

        self.assertEqual(len(plan.migrations), 1)
        self.assertFalse(plan.migrations[0].split)
        self.assertEqual(plan.migrations[0].size, HUGE_PAGE_SIZE)
        self.assertEqual(page.node, 1)


if __name__ == "__main__":
    unittest.main()
