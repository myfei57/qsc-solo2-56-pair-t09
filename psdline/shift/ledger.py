"""班次台账。

班次只有三种状态：当班（``active``）、交接中（``handing_over``）、已封存
（``closed``）。封存的班次拒绝任何改写——历史不可变，交班后发现的错漏只能
记到下一班，不能回头涂改上一班。当前班次指针单独落盘，重启后按指针恢复。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import RailError
from ..store import JsonStore

ACTIVE = "active"
HANDING_OVER = "handing_over"
CLOSED = "closed"
STATES = (ACTIVE, HANDING_OVER, CLOSED)


@dataclass(frozen=True)
class Shift:
    """一个班次。"""

    id: str
    label: str
    operator: str
    state: str
    opened_at: float
    closed_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "operator": self.operator,
            "state": self.state,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Shift":
        return cls(
            id=str(payload.get("id", "")),
            label=str(payload.get("label", "")),
            operator=str(payload.get("operator", "")),
            state=str(payload.get("state", "")),
            opened_at=float(payload.get("opened_at", 0.0)),
            closed_at=float(payload.get("closed_at", 0.0)),
        )


class ShiftRegistry:
    """班次台账，落在 ``shift/ledger/<id>`` 文档上。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(shift_id: str) -> str:
        return f"shift/ledger/{shift_id}"

    @staticmethod
    def _pointer_id() -> str:
        return "shift/current"

    def get(self, shift_id: str) -> Shift | None:
        """读取一个班次，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(shift_id))
        if not isinstance(raw, dict):
            return None
        return Shift.from_dict(raw)

    def current_id(self) -> str:
        """返回当前班次标识；没有时返回空串。"""

        raw = self._store.read(self._pointer_id())
        if isinstance(raw, dict):
            return str(raw.get("shift_id", ""))
        return ""

    def current(self) -> Shift | None:
        """返回当前班次；指针缺失或悬空时返回 ``None``。"""

        shift_id = self.current_id()
        return self.get(shift_id) if shift_id else None

    def set_current(self, shift_id: str) -> None:
        """把当前班次指针指向某班次。"""

        self._store.write(self._pointer_id(), {"shift_id": shift_id})

    def open(self, shift: Shift) -> Shift:
        """登记一个新班次并把指针拨过去；标识重复时拒绝。"""

        if not shift.id:
            raise RailError("班次标识为空", component="shift")
        if self.get(shift.id) is not None:
            raise RailError(f"班次 {shift.id} 已存在", component="shift")
        self._store.write(self._doc_id(shift.id), shift.as_dict())
        self.set_current(shift.id)
        return shift

    def save(self, shift: Shift) -> Shift:
        """改写一个未封存的班次；已封存的拒绝改写。"""

        existing = self.get(shift.id)
        if existing is None:
            raise RailError(f"班次不存在: {shift.id}", component="shift")
        if existing.state == CLOSED:
            raise RailError(f"班次 {shift.id} 已封存，历史不可改", component="shift")
        self._store.write(self._doc_id(shift.id), shift.as_dict())
        return shift

    def list(self) -> list[Shift]:
        """按开班时间列出全部班次。"""

        found: list[Shift] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("shift/ledger/"):
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(Shift.from_dict(raw))
        return sorted(found, key=lambda item: (item.opened_at, item.id))
