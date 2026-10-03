"""班次域的登记表（当前态投影）。

四张表都落在 :class:`~psdline.store.JsonStore` 上，文档是**投影**：唯一不可变
事实在 :class:`~psdline.shift.ledger.ShiftLedger` 的哈希链台账里。投影可以在
崩溃后通过 ``ShiftService.recover()`` 重放台账重建，因此投影写一半掉电不会造成
"事实分叉"——重放即恢复。
"""

from __future__ import annotations

from ..errors import RailError
from ..store import JsonStore
from .model import (
    Handover,
    Operator,
    Shift,
    WorkItem,
    WI_DONE,
)


class OperatorRegistry:
    """值班人员名册，文档 ``shift/operator/<id>``。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(operator_id: str) -> str:
        return f"shift/operator/{operator_id}"

    def upsert(self, operator: Operator) -> Operator:
        if not operator.id:
            raise RailError("值班人员标识为空", component="shift")
        if operator.role not in ("operator", "leader"):
            raise RailError(f"未知角色: {operator.role}", component="shift")
        self._store.write(self._doc_id(operator.id), operator.as_dict())
        return operator

    def get(self, operator_id: str) -> Operator | None:
        raw = self._store.read(self._doc_id(operator_id))
        return Operator.from_dict(raw) if isinstance(raw, dict) else None

    def require(self, operator_id: str) -> Operator:
        operator = self.get(operator_id)
        if operator is None:
            raise RailError(f"值班人员不存在: {operator_id}", component="shift")
        return operator

    def require_enabled(self, operator_id: str) -> Operator:
        operator = self.require(operator_id)
        if not operator.enabled:
            raise RailError(f"值班人员已停用: {operator_id}", component="shift")
        return operator

    def list(self) -> list[Operator]:
        found: list[Operator] = []
        for doc_id in self._store.list_ids():
            prefix = "shift/operator/"
            if not doc_id.startswith(prefix) or doc_id.count("/") != 2:
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(Operator.from_dict(raw))
        return sorted(found, key=lambda item: item.id)


class ShiftRegistry:
    """班次登记表，文档 ``shift/shift/<id>``。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(shift_id: str) -> str:
        return f"shift/shift/{shift_id}"

    def save(self, shift: Shift) -> Shift:
        if not shift.id:
            raise RailError("班次标识为空", component="shift")
        self._store.write(self._doc_id(shift.id), shift.as_dict())
        return shift

    def get(self, shift_id: str) -> Shift | None:
        raw = self._store.read(self._doc_id(shift_id))
        return Shift.from_dict(raw) if isinstance(raw, dict) else None

    def require(self, shift_id: str) -> Shift:
        shift = self.get(shift_id)
        if shift is None:
            raise RailError(f"班次不存在: {shift_id}", component="shift")
        return shift

    def list(self) -> list[Shift]:
        found: list[Shift] = []
        for doc_id in self._store.list_ids():
            prefix = "shift/shift/"
            if not doc_id.startswith(prefix) or doc_id.count("/") != 2:
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(Shift.from_dict(raw))
        return sorted(found, key=lambda item: item.opened_at)

    def in_state(self, *states: str) -> list[Shift]:
        return [shift for shift in self.list() if shift.state in states]

    def active(self) -> Shift | None:
        """返回当前在值班次（active 或 frozen，frozen 优先）。"""

        candidates = self.in_state("active", "frozen")
        frozen = [shift for shift in candidates if shift.state == "frozen"]
        if frozen:
            return frozen[0]
        return candidates[0] if candidates else None


class HandoverRegistry:
    """交接登记表，文档 ``shift/handover/<id>``。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(handover_id: str) -> str:
        return f"shift/handover/{handover_id}"

    def save(self, handover: Handover) -> Handover:
        self._store.write(self._doc_id(handover.id), handover.as_dict())
        return handover

    def get(self, handover_id: str) -> Handover | None:
        raw = self._store.read(self._doc_id(handover_id))
        return Handover.from_dict(raw) if isinstance(raw, dict) else None

    def require(self, handover_id: str) -> Handover:
        handover = self.get(handover_id)
        if handover is None:
            raise RailError(f"交接记录不存在: {handover_id}", component="shift")
        return handover

    def list(self) -> list[Handover]:
        found: list[Handover] = []
        for doc_id in self._store.list_ids():
            prefix = "shift/handover/"
            if not doc_id.startswith(prefix) or doc_id.count("/") != 2:
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(Handover.from_dict(raw))
        return sorted(found, key=lambda item: item.opened_at)

    def open_one(self) -> Handover | None:
        """返回当前未完成的交接（open / review 各至多一条）。"""

        pending = [item for item in self.list() if item.state in ("open", "review")]
        return pending[-1] if pending else None


class WorkItemRegistry:
    """在办事项登记表，文档 ``shift/item/<id>``。

    事项不做物理删除（历史不可变）；终态为 ``done`` / ``aborted`` / ``suspended``。
    """

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(item_id: str) -> str:
        return f"shift/item/{item_id}"

    def save(self, item: WorkItem) -> WorkItem:
        self._store.write(self._doc_id(item.id), item.as_dict())
        return item

    def get(self, item_id: str) -> WorkItem | None:
        raw = self._store.read(self._doc_id(item_id))
        return WorkItem.from_dict(raw) if isinstance(raw, dict) else None

    def require(self, item_id: str) -> WorkItem:
        item = self.get(item_id)
        if item is None:
            raise RailError(f"在办事项不存在: {item_id}", component="shift")
        return item

    def list(self) -> list[WorkItem]:
        found: list[WorkItem] = []
        for doc_id in self._store.list_ids():
            prefix = "shift/item/"
            if not doc_id.startswith(prefix) or doc_id.count("/") != 2:
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(WorkItem.from_dict(raw))
        return sorted(found, key=lambda item: item.created_at)

    def open_items(self) -> list[WorkItem]:
        """当前仍未办结的事项（含挂起）。"""

        terminal = {WI_DONE, "aborted"}
        return [item for item in self.list() if item.state not in terminal]

    def by_ref(self, kind: str, ref: str) -> WorkItem | None:
        """按类型与现场对象引用查找未办结事项（用于防止重复登记）。"""

        for item in self.open_items():
            if item.kind == kind and item.ref == ref:
                return item
        return None

    def orphans(self) -> list[WorkItem]:
        """没有归属在值班次的事项（强制收口后等待认领）。"""

        return [item for item in self.open_items() if not item.owner_shift_id]
