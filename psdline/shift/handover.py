"""交接单与交接状态机。

交接单把"谁交给谁、哪些在办事项、每项确认到哪一步"落成一份文档：交班发起
时快照在办事项（``pending``），接班到岗后未确认的事项自动挂起
（``suspended``），接班人逐项确认（``accepted``）后接着办（``resumed``），
事项对应的现场条件消失时自动办结（``done``)。``pending`` 与 ``suspended``
都卡住发车放行。交接单完成或作废后封存，任何改写都会被拒绝——历史只能追加，
不能涂改。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import RailError
from ..store import JsonStore

OFFERED = "offered"
ACCEPTING = "accepting"
COMPLETED = "completed"
ABORTED = "aborted"
TERMINAL = (COMPLETED, ABORTED)

PENDING = "pending"
SUSPENDED = "suspended"
ACCEPTED = "accepted"
RESUMED = "resumed"
DONE = "done"
BLOCKING = (PENDING, SUSPENDED)


@dataclass(frozen=True)
class HandoverItem:
    """交接单里的一条在办事项。"""

    key: str
    kind: str
    ref: str
    summary: str
    state: str
    since: float
    detail: dict = field(default_factory=dict)
    acked_by: str = ""
    acked_at: float = 0.0
    resumed_at: float = 0.0
    done_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "ref": self.ref,
            "summary": self.summary,
            "state": self.state,
            "since": self.since,
            "detail": dict(self.detail),
            "acked_by": self.acked_by,
            "acked_at": self.acked_at,
            "resumed_at": self.resumed_at,
            "done_at": self.done_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "HandoverItem":
        detail = payload.get("detail", {})
        return cls(
            key=str(payload.get("key", "")),
            kind=str(payload.get("kind", "")),
            ref=str(payload.get("ref", "")),
            summary=str(payload.get("summary", "")),
            state=str(payload.get("state", "")),
            since=float(payload.get("since", 0.0)),
            detail=dict(detail) if isinstance(detail, dict) else {},
            acked_by=str(payload.get("acked_by", "")),
            acked_at=float(payload.get("acked_at", 0.0)),
            resumed_at=float(payload.get("resumed_at", 0.0)),
            done_at=float(payload.get("done_at", 0.0)),
        )


@dataclass(frozen=True)
class Handover:
    """一份交接单。"""

    id: str
    from_shift: str
    to_shift: str
    state: str
    items: tuple[HandoverItem, ...]
    created_by: str
    created_at: float
    accepted_by: str = ""
    accepted_at: float = 0.0
    completed_at: float = 0.0
    approved_by: str = ""
    timeout_noted: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "from_shift": self.from_shift,
            "to_shift": self.to_shift,
            "state": self.state,
            "items": [item.as_dict() for item in self.items],
            "created_by": self.created_by,
            "created_at": self.created_at,
            "accepted_by": self.accepted_by,
            "accepted_at": self.accepted_at,
            "completed_at": self.completed_at,
            "approved_by": self.approved_by,
            "timeout_noted": self.timeout_noted,
            "blocking": len(self.blocking()),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Handover":
        items = payload.get("items", [])
        return cls(
            id=str(payload.get("id", "")),
            from_shift=str(payload.get("from_shift", "")),
            to_shift=str(payload.get("to_shift", "")),
            state=str(payload.get("state", "")),
            items=tuple(
                HandoverItem.from_dict(item) for item in items if isinstance(item, dict)
            ),
            created_by=str(payload.get("created_by", "")),
            created_at=float(payload.get("created_at", 0.0)),
            accepted_by=str(payload.get("accepted_by", "")),
            accepted_at=float(payload.get("accepted_at", 0.0)),
            completed_at=float(payload.get("completed_at", 0.0)),
            approved_by=str(payload.get("approved_by", "")),
            timeout_noted=bool(payload.get("timeout_noted", False)),
        )

    def item(self, key: str) -> HandoverItem | None:
        """按标识找到一条事项；没有时返回 ``None``。"""

        for item in self.items:
            if item.key == key:
                return item
        return None

    def blocking(self) -> list[HandoverItem]:
        """返回仍在挂起（未确认）的事项。"""

        return [item for item in self.items if item.state in BLOCKING]


class HandoverRegistry:
    """交接单台账，落在 ``shift/handovers/<id>`` 文档上。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(handover_id: str) -> str:
        return f"shift/handovers/{handover_id}"

    def get(self, handover_id: str) -> Handover | None:
        """读取一份交接单，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(handover_id))
        if not isinstance(raw, dict):
            return None
        return Handover.from_dict(raw)

    def save(self, handover: Handover) -> Handover:
        """写入一份交接单；已封存的拒绝改写。"""

        if not handover.id:
            raise RailError("交接单标识为空", component="shift")
        existing = self.get(handover.id)
        if existing is not None and existing.state in TERMINAL:
            raise RailError(f"交接单 {handover.id} 已封存，历史不可改", component="shift")
        self._store.write(self._doc_id(handover.id), handover.as_dict())
        return handover

    def list(self) -> list[Handover]:
        """按发起时间列出全部交接单。"""

        found: list[Handover] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("shift/handovers/"):
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(Handover.from_dict(raw))
        return sorted(found, key=lambda item: (item.created_at, item.id))

    def active(self) -> Handover | None:
        """返回正在进行中的交接单；没有时返回 ``None``。"""

        for handover in self.list():
            if handover.state not in TERMINAL:
                return handover
        return None
