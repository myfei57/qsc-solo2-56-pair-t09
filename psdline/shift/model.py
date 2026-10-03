"""班次交接的数据模型。

四类对象覆盖"谁在值班、在办什么事、交接进行到哪一步、值班台当前归谁"：

``Operator``    值班人员（普通值班员或班组长），班组长负责会签与强制收口。
``Shift``       班次：计划 → 在值 → 冻结 → 已交接 / 已强制收口。
``Handover``    一次交接：开启 → 逐项确认 → （挂起 / 会签）→ 完成。
``WorkItem``    在办事项：批量开门、挂起故障、门控指令、通用事项。

事项状态机::

    OPEN ── begin ──▶ PRESENTED ── ack ──▶ ACKNOWLEDGED ── resume/complete ──▶ DONE
                          │
                          └── 超时 / 强制收口 ──▶ SUSPENDED ── resume(组长会签) ──▶ RESUMED

挂起项在"发车放行"里被一票否决；RESUMED / ACKNOWLEDGED / DONE 不再拦截放行。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---- 人员角色 ---------------------------------------------------------------

ROLE_OPERATOR = "operator"
ROLE_LEADER = "leader"
ROLES = (ROLE_OPERATOR, ROLE_LEADER)

# ---- 班次状态 ---------------------------------------------------------------

SHIFT_PLANNED = "planned"      # 已计划、未开班
SHIFT_ACTIVE = "active"        # 在值（值班台权威代际归属本班）
SHIFT_FROZEN = "frozen"        # 交接冻结窗口中，安全动作一律拒绝
SHIFT_HANDED = "handed"        # 正常交接完成
SHIFT_FORCED = "forced"        # 接班人缺位，班组长强制收口

# ---- 交接阶段 ---------------------------------------------------------------

HO_OPEN = "open"               # 交班方逐项陈述中
HO_REVIEW = "review"           # 存在挂起项，等待班组长会签
HO_DONE = "done"               # 交接完成
HO_FORCED = "forced"           # 强制收口

# ---- 在办事项状态 -----------------------------------------------------------

WI_OPEN = "open"
WI_PRESENTED = "presented"
WI_ACKNOWLEDGED = "acknowledged"
WI_SUSPENDED = "suspended"
WI_RESUMED = "resumed"
WI_DONE = "done"
WI_ABORTED = "aborted"

# 事项类型
WI_BATCH = "batch_open"
WI_FAULT = "fault"
WI_COMMAND = "command"
WI_GENERIC = "generic"

# 会拦截发车放行的事项状态
BLOCKING_ITEM_STATES = frozenset({WI_OPEN, WI_PRESENTED, WI_SUSPENDED})
SAFETY_ITEM_KINDS = frozenset({WI_BATCH, WI_FAULT, WI_COMMAND})


@dataclass
class Operator:
    """值班人员。"""

    id: str
    name: str = ""
    role: str = ROLE_OPERATOR
    enabled: bool = True

    @property
    def is_leader(self) -> bool:
        return self.role == ROLE_LEADER

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Operator":
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            role=str(payload.get("role", ROLE_OPERATOR)),
            enabled=bool(payload.get("enabled", True)),
        )


@dataclass
class Shift:
    """一个班次。"""

    id: str
    state: str = SHIFT_PLANNED
    roster: list[str] = field(default_factory=list)   # 本班值班员
    leader_id: str = ""                               # 本班班组长
    opened_at: float = 0.0
    opened_by: str = ""
    closed_at: float = 0.0
    close_reason: str = ""
    epoch: int = 0                                    # 开班时接管的权威代际

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "roster": list(self.roster),
            "leader_id": self.leader_id,
            "opened_at": self.opened_at,
            "opened_by": self.opened_by,
            "closed_at": self.closed_at,
            "close_reason": self.close_reason,
            "epoch": self.epoch,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Shift":
        roster = payload.get("roster", [])
        return cls(
            id=str(payload.get("id", "")),
            state=str(payload.get("state", SHIFT_PLANNED)),
            roster=[str(item) for item in roster] if isinstance(roster, list) else [],
            leader_id=str(payload.get("leader_id", "")),
            opened_at=float(payload.get("opened_at", 0.0)),
            opened_by=str(payload.get("opened_by", "")),
            closed_at=float(payload.get("closed_at", 0.0)),
            close_reason=str(payload.get("close_reason", "")),
            epoch=int(payload.get("epoch", 0)),
        )


@dataclass
class Handover:
    """一次班次交接（一次交接一份文档）。"""

    id: str
    shift_id: str                                     # 交班班次
    incoming_shift_id: str = ""                       # 接班班次（缺位时为空）
    state: str = HO_OPEN
    opened_at: float = 0.0
    opened_by: str = ""
    deadline: float = 0.0                             # 逐项确认截止时刻
    item_ids: list[str] = field(default_factory=list)
    suspended: list[str] = field(default_factory=list)
    acknowledged: list[str] = field(default_factory=list)
    leader_id: str = ""                               # 会签班组长
    leader_at: float = 0.0
    completed_at: float = 0.0
    completed_by: str = ""
    forced: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "shift_id": self.shift_id,
            "incoming_shift_id": self.incoming_shift_id,
            "state": self.state,
            "opened_at": self.opened_at,
            "opened_by": self.opened_by,
            "deadline": self.deadline,
            "item_ids": list(self.item_ids),
            "suspended": list(self.suspended),
            "acknowledged": list(self.acknowledged),
            "leader_id": self.leader_id,
            "leader_at": self.leader_at,
            "completed_at": self.completed_at,
            "completed_by": self.completed_by,
            "forced": self.forced,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Handover":
        def names(key: str) -> list[str]:
            value = payload.get(key, [])
            return [str(item) for item in value] if isinstance(value, list) else []

        return cls(
            id=str(payload.get("id", "")),
            shift_id=str(payload.get("shift_id", "")),
            incoming_shift_id=str(payload.get("incoming_shift_id", "")),
            state=str(payload.get("state", HO_OPEN)),
            opened_at=float(payload.get("opened_at", 0.0)),
            opened_by=str(payload.get("opened_by", "")),
            deadline=float(payload.get("deadline", 0.0)),
            item_ids=names("item_ids"),
            suspended=names("suspended"),
            acknowledged=names("acknowledged"),
            leader_id=str(payload.get("leader_id", "")),
            leader_at=float(payload.get("leader_at", 0.0)),
            completed_at=float(payload.get("completed_at", 0.0)),
            completed_by=str(payload.get("completed_by", "")),
            forced=bool(payload.get("forced", False)),
            reason=str(payload.get("reason", "")),
        )

    def remaining(self) -> list[str]:
        """还没完成逐项确认的事项。"""

        return [item_id for item_id in self.item_ids if item_id not in self.acknowledged]


@dataclass
class WorkItem:
    """一件在办事项。

    ``ref`` 指向现场对象（批量开门单号 / 门区标识 / 指令标识）；
    ``payload`` 保留该事项的现场快照（哪些门区未开、锁定原因等），
    冻结期间事实可能继续变化，恢复时以快照为线索重新过联锁，不盲信快照。
    """

    id: str
    kind: str
    title: str
    ref: str = ""
    station_id: str = ""
    state: str = WI_OPEN
    owner_shift_id: str = ""
    created_by: str = ""
    created_at: float = 0.0
    presented_in: str = ""                           # 在哪次交接里被逐项陈述
    acknowledged_by: str = ""
    acknowledged_at: float = 0.0
    suspended_at: float = 0.0
    suspend_reason: str = ""
    resumed_by: str = ""
    resumed_at: float = 0.0
    resume_leader: str = ""                          # 恢复时的会签班组长
    completed_by: str = ""
    completed_at: float = 0.0
    abort_reason: str = ""
    payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "ref": self.ref,
            "station_id": self.station_id,
            "state": self.state,
            "owner_shift_id": self.owner_shift_id,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "presented_in": self.presented_in,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at,
            "suspended_at": self.suspended_at,
            "suspend_reason": self.suspend_reason,
            "resumed_by": self.resumed_by,
            "resumed_at": self.resumed_at,
            "resume_leader": self.resume_leader,
            "completed_by": self.completed_by,
            "completed_at": self.completed_at,
            "abort_reason": self.abort_reason,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WorkItem":
        nested = payload.get("payload", {})
        return cls(
            id=str(payload.get("id", "")),
            kind=str(payload.get("kind", WI_GENERIC)),
            title=str(payload.get("title", "")),
            ref=str(payload.get("ref", "")),
            station_id=str(payload.get("station_id", "")),
            state=str(payload.get("state", WI_OPEN)),
            owner_shift_id=str(payload.get("owner_shift_id", "")),
            created_by=str(payload.get("created_by", "")),
            created_at=float(payload.get("created_at", 0.0)),
            presented_in=str(payload.get("presented_in", "")),
            acknowledged_by=str(payload.get("acknowledged_by", "")),
            acknowledged_at=float(payload.get("acknowledged_at", 0.0)),
            suspended_at=float(payload.get("suspended_at", 0.0)),
            suspend_reason=str(payload.get("suspend_reason", "")),
            resumed_by=str(payload.get("resumed_by", "")),
            resumed_at=float(payload.get("resumed_at", 0.0)),
            resume_leader=str(payload.get("resume_leader", "")),
            completed_by=str(payload.get("completed_by", "")),
            completed_at=float(payload.get("completed_at", 0.0)),
            abort_reason=str(payload.get("abort_reason", "")),
            payload=dict(nested) if isinstance(nested, dict) else {},
        )
