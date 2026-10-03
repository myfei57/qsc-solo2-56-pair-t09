"""班次交接。

班次台账记录谁在当班，交接单记录两班之间哪些在办事项交接到哪一步。未确认
的事项自动挂起并卡住发车放行，接班人逐项确认后接着办；交接完成后交接单与
交班班次一起封存，历史不可改。名册决定谁能开班、接班、复核与作废。
"""

from __future__ import annotations

from .handover import (
    ABORTED,
    ACCEPTED,
    ACCEPTING,
    BLOCKING,
    COMPLETED,
    DONE,
    OFFERED,
    PENDING,
    RESUMED,
    SUSPENDED,
    Handover,
    HandoverItem,
    HandoverRegistry,
)
from .items import (
    BATCH_PARTIAL,
    DOOR_OPEN,
    FAULT_ACTIVE,
    GAP_BLOCKED,
    GATES_OPEN,
    ITEM_KINDS,
    QUEUED_COMMAND,
    OpenItem,
    collect_open_items,
    note_batch_remainder,
)
from .ledger import ACTIVE, CLOSED, HANDING_OVER, Shift, ShiftRegistry
from .roster import OPERATOR, SUPERVISOR, Roster
from .service import ShiftService

__all__ = [
    "ABORTED",
    "ACCEPTED",
    "ACCEPTING",
    "ACTIVE",
    "BATCH_PARTIAL",
    "BLOCKING",
    "CLOSED",
    "COMPLETED",
    "DONE",
    "DOOR_OPEN",
    "FAULT_ACTIVE",
    "GAP_BLOCKED",
    "GATES_OPEN",
    "HANDING_OVER",
    "Handover",
    "HandoverItem",
    "HandoverRegistry",
    "ITEM_KINDS",
    "OFFERED",
    "OPERATOR",
    "OpenItem",
    "PENDING",
    "QUEUED_COMMAND",
    "RESUMED",
    "Roster",
    "SUPERVISOR",
    "SUSPENDED",
    "Shift",
    "ShiftRegistry",
    "ShiftService",
    "collect_open_items",
    "note_batch_remainder",
]
