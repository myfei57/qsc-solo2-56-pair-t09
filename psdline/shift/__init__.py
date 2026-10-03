"""班次交接。

本子系统在既有门控/联锁之上加入"值班台权威"：

* :class:`ShiftService` 是唯一控制点，硬冻结、逐项确认、自动挂起、权威代际都在
  这里收口；
* :class:`ShiftLedger` 是只追加的 SHA-256 哈希链台账，历史不可变；
* ``registries`` / ``model`` 是当前态投影与数据模型，可由台账重放重建。

与门控、联锁、审计的一致性约定见 ``docs/shift.md``。
"""

from __future__ import annotations

from .ledger import GENESIS_HASH, LEDGER_TOPIC, ChainReport, LedgerEntry, ShiftLedger
from .model import (
    BLOCKING_ITEM_STATES,
    ROLE_LEADER,
    ROLE_OPERATOR,
    SAFETY_ITEM_KINDS,
    Handover,
    Operator,
    Shift,
    WI_ABORTED,
    WI_ACKNOWLEDGED,
    WI_BATCH,
    WI_COMMAND,
    WI_DONE,
    WI_FAULT,
    WI_GENERIC,
    WI_OPEN,
    WI_PRESENTED,
    WI_RESUMED,
    WI_SUSPENDED,
    WorkItem,
)
from .registries import (
    HandoverRegistry,
    OperatorRegistry,
    ShiftRegistry,
    WorkItemRegistry,
)
from .service import ShiftService

__all__ = [
    "BLOCKING_ITEM_STATES",
    "ChainReport",
    "GENESIS_HASH",
    "Handover",
    "HandoverRegistry",
    "LEDGER_TOPIC",
    "LedgerEntry",
    "Operator",
    "OperatorRegistry",
    "ROLE_LEADER",
    "ROLE_OPERATOR",
    "SAFETY_ITEM_KINDS",
    "Shift",
    "ShiftLedger",
    "ShiftRegistry",
    "ShiftService",
    "WI_ABORTED",
    "WI_ACKNOWLEDGED",
    "WI_BATCH",
    "WI_COMMAND",
    "WI_DONE",
    "WI_FAULT",
    "WI_GENERIC",
    "WI_OPEN",
    "WI_PRESENTED",
    "WI_RESUMED",
    "WI_SUSPENDED",
    "WorkItem",
    "WorkItemRegistry",
]
