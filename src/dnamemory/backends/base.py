# -*- coding: utf-8 -*-
"""可插拔检索后端协议（docs/开发计划-可插拔后端.md）。

- TimeBackend：时间链（时间窗召回）后端；
- GraphBackend：空间链（实体种子多跳遍历）后端；
- 协议只负责返回 {node_id: score} 原始分；RRF/过滤/排序由 governance.recall 统一处理；
- 后端为查询只读适配器，写入仍走 SQLite（v1 不实现写穿透）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class TimeBackend(Protocol):
    """时间链后端：按时间中心与容差返回事件 node_id -> 时间分。

    分语义与 retrieval._time_path 一致：
    score = 1 / (1 + diff_days)，diff_days = abs((ts - t0).days)。
    """

    def time_window(self, t0: datetime, tol_days: int) -> dict[int, float]: ...

    def close(self) -> None: ...


@runtime_checkable
class GraphBackend(Protocol):
    """空间链后端：从实体种子出发 max_hops 遍历，返回 node_id -> 图谱分。

    分语义与 retrieval._graph_path 一致（返回全部可达节点，事件与实体）：
    - 事件：max(1/(hop+1) * weight * confidence * prev.value_score)；
    - 实体：max(1/(hop+1) * weight * confidence)。
    """

    def traverse(self, seed_ids: list[int], max_hops: int,
                 rel_filter: Optional[str] = None) -> dict[int, float]: ...

    def close(self) -> None: ...
