# numa-page-migration-hotspot-planner
规划 NUMA 热页迁移、远端带宽和抖动抑制

> 说明：本项目中所有页面访问数据均来自**离线轨迹（offline traces）**,
> 规划器不采集、不探测任何运行中系统的实时内存状态。

## 概述

`numa_planner` 是一个面向双路（可扩展多路）NUMA 系统的页迁移规划器，
目标运行环境为 Python 3.8.2。它按时间片消费离线访问轨迹，输出每批
应迁移的页集合、预期收益、抖动风险以及无法迁移的原因。

## 输入

- 页地址、按节点细分的访问热度（`heat_by_node`)、所属进程
- NUMA 距离矩阵（`distance`)、各节点远端带宽上限与当前占用
- 各节点本地剩余容量（字节）
- 透明大页标记（`huge`）及可选的子页热度（`sub_heat`)
- 固定页标记（`pinned`)、共享映射（`shared_with`)
- 迁移成本参数（`PlannerConfig.migration_cost_per_page` 等）

## 规划规则

- 固定页（pinned）不迁移，原因记为 `pinned`。
- 共享页迁移需更新所有映射，迁移成本随映射数量线性放大。
- 收益 = 迁移前后访问代价差（热度 x NUMA 距离），收益不高于
  成本 x 边际系数的页不迁移（`benefit_below_cost`)。
- 抖动抑制：在冷却窗口内发生反向迁移的页，需要收益超过
  成本 x `thrash_penalty` 才允许迁回，否则记为
  `thrash_suppression`；仍迁回的页计入 `thrash_risk`。
- 带宽预测：每个时间片内维护各节点下一窗口的预测远端负载，
  所有进程的候选迁移共享同一预算（按进程轮转调度），预测负载
  超过 `limit x bandwidth_safety` 的迁移被拒绝
  (`bandwidth_saturation`)，避免多个任务同时搬向同一节点。
- 容量不足时记为 `insufficient_capacity`；透明大页在整页迁移
  不划算或容量不足时，可按子页热度拆分为基页只迁移热子页。

## 输出

`WindowPlan` 包含：

- `migrations`：每批迁移页（源/目的节点、尺寸、收益、成本、
  更新映射数、是否拆分、是否反向抖动）
- `expected_benefit`：本窗口预期总收益
- `thrash_risk`：抖动风险（反向迁移占比，0~1)
- `rejected`：无法迁移的页及原因
- `predicted_remote_load`：迁移后各节点下一窗口的预测远端负载

## 用法

```python
from numa_planner import MigrationPlanner, Node, Page

planner = MigrationPlanner(distance={(0, 1): 2.0, (1, 0): 2.0})
nodes = [Node(0, free_capacity=1 << 30, remote_bandwidth_limit=1000.0,
              remote_bandwidth_used=900.0),
         Node(1, free_capacity=1 << 30, remote_bandwidth_limit=1000.0)]
pages = [Page(address=0x1000, node=0, process="db",
              heat_by_node={1: 100.0})]
plan = planner.plan_window(pages, nodes, window=0)
```

## 测试

```sh
python -m unittest
```

覆盖：单热点迁移、双向抖动抑制、共享页映射成本、带宽饱和下
多任务预算协调、透明大页拆分。
