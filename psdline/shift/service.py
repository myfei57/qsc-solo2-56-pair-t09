"""班次交接服务：值班台唯一控制点。

本模块把"班次—交接—在办事项—权威代际"串成一条状态机，并对外提供三类入口：

* 交接流程：``plan_shift`` / ``open_shift`` / ``roster_incoming`` /
  ``begin_handover`` / ``acknowledge_item`` / ``suspend_pending`` /
  ``countersign`` / ``complete_handover`` / ``force_close`` /
  ``adopt_orphans``；
* 在办事项：``register_item`` / ``resume_item`` / ``complete_item`` /
  ``abort_item``，以及平台侧回调 ``note_fault_set`` / ``note_fault_cleared`` /
  ``note_batch_open`` / ``discover_open_items``；
* 门控与联锁的统一闸门：``guard``（冻结窗口拒绝一切安全动作）、
  ``release_blockers``（挂起/未交接项一票否决发车放行）。

安全取舍（见 ``docs/shift.md``）：

1. **硬冻结**。``begin_handover`` 之后值班台进入 ``frozen``，除交接本身外的
   所有安全动作都被 ``guard`` 拒绝，直到交接完成、权威代际切换；
2. **逐项确认 + 自动挂起**。每件在办事项必须被接班人显式确认；到截止时刻仍未
   确认的，``suspend_pending``（``tick`` 自动触发）自动挂起；
3. **挂起即扣发放行**。挂起项全局一票否决发车放行，恢复挂起项必须班组长会签
   并在恢复时重新过联锁；
4. **权威代际**。交接完成原子推进 ``epoch``，上一班签发的会话全部失效，跨班
   迟到命令被 ``guard`` 拒绝；
5. **台账在先**。每个状态变化先追加哈希链台账，再改投影，最后写审计；崩溃恢复
   以台账重放为准。
"""

from __future__ import annotations

import threading
import uuid
from typing import Callable

from ..audit import AuditLog
from ..clock import Clock
from ..errors import RailError
from ..store import JsonStore, JsonlJournal
from .ledger import LEDGER_TOPIC, ShiftLedger
from .model import (
    ROLE_LEADER,
    ROLE_OPERATOR,
    SAFETY_ITEM_KINDS,
    Handover,
    Operator,
    Shift,
    SHIFT_ACTIVE,
    SHIFT_FROZEN,
    SHIFT_HANDED,
    SHIFT_FORCED,
    SHIFT_PLANNED,
    HO_OPEN,
    HO_REVIEW,
    HO_DONE,
    HO_FORCED,
    WI_ABORTED,
    WI_ACKNOWLEDGED,
    WI_BATCH,
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

# 冻结窗口放行闸门返回的拒绝原因
DENY_NO_DESK = "desk_inactive"            # 尚无在值班次（未启用班次管理）
DENY_BAD_SESSION = "bad_session"          # 会话无效 / 已被跨班作废
DENY_NOT_ON_DUTY = "not_on_duty"          # 操作员不属于当前在值班次
DENY_STALE_EPOCH = "stale_epoch"          # 持有的权威代际已过期
DENY_FROZEN = "handover_frozen"           # 交接冻结窗口

DENY_MESSAGES = {
    DENY_BAD_SESSION: "值班会话无效或已跨班作废，请重新签到",
    DENY_NOT_ON_DUTY: "操作员不属于当前在值班次",
    DENY_STALE_EPOCH: "命令来自上一班的旧代际，已拒绝",
    DENY_FROZEN: "交接冻结窗口：安全动作一律暂停，完成交接后由接班人继续",
}


class ShiftService:
    """班次交接与值班台权威控制。"""

    def __init__(
        self,
        store: JsonStore,
        clock: Clock,
        audit: AuditLog,
        *,
        handover_deadline_s: float = 300.0,
        journal_segment: int = 512,
    ) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit
        self._deadline_s = float(handover_deadline_s)
        self._segment_limit = int(journal_segment)
        self.operators = OperatorRegistry(store)
        self.shifts = ShiftRegistry(store)
        self.handovers = HandoverRegistry(store)
        self.items = WorkItemRegistry(store)
        self.ledger = ShiftLedger(
            JsonlJournal(store.root, LEDGER_TOPIC, segment_limit=self._segment_limit)
        )
        self._lock = threading.RLock()
        # token -> {"operator_id", "shift_id", "epoch"}；仅驻内存，重启重签。
        self._sessions: dict[str, dict] = {}
        self._epoch = 0
        # 平台注入的现场回调（见 app 装配）。
        self.hooks: dict[str, Callable] = {}
        self.recover()

    # ================================================================
    # 台账 → 投影：所有状态变化都走"先记账、再 apply"，恢复时重放同一套 apply
    # ================================================================

    def _append(self, kind: str, actor: str, payload: dict, *, at: float | None = None):
        return self.ledger.append(kind, self._clock.now() if at is None else at, actor, payload)

    def _apply(self, kind: str, payload: dict) -> None:
        handler = getattr(self, f"_apply_{kind}", None)
        if handler is not None:
            handler(payload)

    def _apply_operator_registered(self, p: dict) -> None:
        self.operators.upsert(Operator.from_dict(p["operator"]))

    def _apply_shift_planned(self, p: dict) -> None:
        shift = self.shifts.get(p["shift_id"]) or Shift(id=p["shift_id"], state=SHIFT_PLANNED)
        shift.state = SHIFT_PLANNED
        self.shifts.save(shift)

    def _apply_shift_opened(self, p: dict) -> None:
        shift = self.shifts.get(p["shift_id"]) or Shift(id=p["shift_id"])
        shift.state = SHIFT_ACTIVE
        shift.roster = list(p.get("roster", []))
        shift.leader_id = p.get("leader_id", "")
        shift.opened_at = p["at"]
        shift.opened_by = p.get("by", "")
        shift.epoch = int(p.get("epoch", self._epoch))
        self.shifts.save(shift)

    def _apply_shift_rostered(self, p: dict) -> None:
        shift = self.shifts.require(p["shift_id"])
        shift.roster = list(p.get("roster", []))
        shift.leader_id = p.get("leader_id", "")
        self.shifts.save(shift)

    def _apply_work_registered(self, p: dict) -> None:
        self.items.save(WorkItem.from_dict(p["item"]))

    def _apply_handover_began(self, p: dict) -> None:
        self.handovers.save(Handover.from_dict(p["handover"]))
        shift = self.shifts.require(p["shift_id"])
        shift.state = SHIFT_FROZEN
        self.shifts.save(shift)
        for item_id in p.get("item_ids", []):
            item = self.items.require(item_id)
            item.state = WI_PRESENTED
            item.presented_in = p["handover_id"]
            self.items.save(item)

    def _apply_work_acknowledged(self, p: dict) -> None:
        handover = self.handovers.require(p["handover_id"])
        item = self.items.require(p["item_id"])
        item.state = WI_ACKNOWLEDGED
        item.acknowledged_by = p.get("by", "")
        item.acknowledged_at = p["at"]
        self.items.save(item)
        if item.id not in handover.acknowledged:
            handover.acknowledged.append(item.id)
        self.handovers.save(handover)

    def _apply_work_suspended(self, p: dict) -> None:
        handover = self.handovers.require(p["handover_id"])
        for item_id in p.get("item_ids", []):
            item = self.items.require(item_id)
            item.state = WI_SUSPENDED
            item.suspended_at = p["at"]
            item.suspend_reason = p.get("reason", "")
            self.items.save(item)
            if item_id not in handover.suspended:
                handover.suspended.append(item_id)
        handover.state = HO_REVIEW
        self.handovers.save(handover)

    def _apply_handover_signed(self, p: dict) -> None:
        handover = self.handovers.require(p["handover_id"])
        handover.leader_id = p.get("leader_id", "")
        handover.leader_at = p["at"]
        self.handovers.save(handover)

    def _apply_handover_completed(self, p: dict) -> None:
        handover = self.handovers.require(p["handover_id"])
        handover.state = HO_FORCED if p.get("forced") else HO_DONE
        handover.completed_at = p["at"]
        handover.completed_by = p.get("by", "")
        handover.leader_id = p.get("leader_id", handover.leader_id)
        handover.reason = p.get("reason", "")
        self.handovers.save(handover)

        outgoing = self.shifts.require(p["shift_id"])
        outgoing.state = SHIFT_FORCED if p.get("forced") else SHIFT_HANDED
        outgoing.closed_at = p["at"]
        outgoing.close_reason = "forced" if p.get("forced") else "handed"
        self.shifts.save(outgoing)

        incoming_id = p.get("incoming_shift_id", "")
        if incoming_id:
            incoming = self.shifts.require(incoming_id)
            incoming.state = SHIFT_ACTIVE
            incoming.epoch = int(p["epoch"])
            if not incoming.opened_at:
                incoming.opened_at = p["at"]
                incoming.opened_by = p.get("by", "")
            self.shifts.save(incoming)
            for item_id in p.get("transferred", []):
                item = self.items.get(item_id)
                if item is not None:
                    item.owner_shift_id = incoming_id
                    self.items.save(item)
        else:
            # 强制收口：未办结事项全部成为无主孤儿（已经挂起的保持挂起），
            # 等待新班开班后由班组长认领。
            for item_id in p.get("transferred", []):
                item = self.items.get(item_id)
                if item is None:
                    continue
                if item.state != WI_SUSPENDED:
                    item.state = WI_SUSPENDED
                    item.suspended_at = p["at"]
                    item.suspend_reason = "forced_close"
                item.owner_shift_id = ""
                self.items.save(item)

        self._epoch = int(p["epoch"])

    def _apply_work_resumed(self, p: dict) -> None:
        item = self.items.require(p["item_id"])
        item.state = WI_RESUMED
        item.resumed_by = p.get("by", "")
        item.resumed_at = p["at"]
        item.resume_leader = p.get("leader_id", "")
        self.items.save(item)

    def _apply_work_completed(self, p: dict) -> None:
        item = self.items.require(p["item_id"])
        item.state = WI_DONE
        item.completed_by = p.get("by", "")
        item.completed_at = p["at"]
        self.items.save(item)

    def _apply_work_aborted(self, p: dict) -> None:
        item = self.items.require(p["item_id"])
        item.state = WI_ABORTED
        item.abort_reason = p.get("reason", "")
        item.completed_by = p.get("by", "")
        item.completed_at = p["at"]
        self.items.save(item)

    def _apply_orphan_adopted(self, p: dict) -> None:
        for item_id in p.get("item_ids", []):
            item = self.items.require(item_id)
            item.owner_shift_id = p["shift_id"]
            item.state = WI_ACKNOWLEDGED
            item.acknowledged_by = p.get("by", "")
            item.acknowledged_at = p["at"]
            item.suspend_reason = ""
            self.items.save(item)

    def recover(self) -> dict:
        """从哈希链台账重放重建投影，返回恢复摘要。

        台账校验失败直接抛 :class:`~psdline.errors.StorageError`——宁可拒启动，
        也不带篡改过的历史跑。会话不持久化，恢复后所有人重新签到。
        """

        with self._lock:
            report = self.ledger.verify()
            if not report.ok:
                raise RailError(
                    f"班次台账校验失败（第 {report.first_bad_seq} 条: {report.reason}）",
                    component="shift",
                )
            self._epoch = 0
            entries = self.ledger.entries()
            for entry in entries:
                self._apply(entry.kind, {"at": entry.at, "by": entry.actor, **entry.payload})
            self._sessions.clear()
            active = self.shifts.active()
            if active is not None and active.state == SHIFT_ACTIVE:
                self._epoch = max(self._epoch, active.epoch)
            return {
                "entries": len(entries),
                "epoch": self._epoch,
                "active_shift": active.id if active else "",
                "open_items": len(self.items.open_items()),
            }

    # ================================================================
    # 人员与会话
    # ================================================================

    def _bootstrap(self) -> bool:
        """尚未启用班次管理（台账里没有任何开班记录）。"""

        return not any(shift.state != SHIFT_PLANNED for shift in self.shifts.list())

    def register_operator(
        self,
        actor: dict | None,
        operator_id: str,
        name: str = "",
        role: str = ROLE_OPERATOR,
    ) -> dict:
        """登记值班人员。首名班组长可在冷启动（无人在册）时自举登记。"""

        if role not in (ROLE_OPERATOR, ROLE_LEADER):
            raise RailError(f"未知角色: {role}", component="shift")
        if not operator_id:
            raise RailError("值班人员标识为空", component="shift")
        # 冷启动自举：台账里还没有任何开班/计划班次时，第一批人可直接登记；
        # 一旦班次管理启动，班组长只能由既有的在值班组长登记。
        cold = not self.shifts.list()
        if not cold:
            # 管理动作：班组长凭在册身份自证（冻结窗口没有有效值班会话时也要能补员）。
            self._require_leader_identity(actor)

        operator = Operator(id=operator_id, name=name, role=role)
        with self._lock:
            self._append("operator_registered", operator_id, {"operator": operator.as_dict()})
            self._apply_operator_registered({"operator": operator.as_dict()})
            self._audit.record("shift_operator_registered", "", "", operator_id=operator_id, role=role)
        return {"ok": True, "operator": operator.as_dict()}

    def check_in(self, actor: dict | None, operator_id: str, shift_id: str) -> dict:
        """值班员签到，领取绑定代际的会话令牌。"""

        operator = self.operators.require_enabled(operator_id)
        shift = self.shifts.require(shift_id)
        if shift.state not in (SHIFT_ACTIVE, SHIFT_FROZEN, SHIFT_PLANNED):
            raise RailError(f"班次 {shift_id} 不接受签到（{shift.state}）", component="shift")
        if operator_id not in shift.roster and operator.id != shift.leader_id:
            raise RailError("操作员不在该班次名册中", component="shift")
        token = uuid.uuid4().hex
        with self._lock:
            self._sessions[token] = {
                "operator_id": operator.id,
                "shift_id": shift.id,
                "epoch": self._epoch,
            }
        self._audit.record("shift_checkin", "", "", operator_id=operator_id, shift_id=shift_id)
        return {
            "ok": True,
            "token": token,
            "operator_id": operator.id,
            "shift_id": shift.id,
            "epoch": self._epoch,
            "role": operator.role,
        }

    def sign_out(self, actor: dict | None) -> dict:
        token = str((actor or {}).get("token", ""))
        with self._lock:
            existed = self._sessions.pop(token, None) is not None
        return {"ok": True, "signed_out": existed}

    def _session(self, actor: dict | None) -> dict | None:
        token = str((actor or {}).get("token", ""))
        return self._sessions.get(token)

    def _require_leader(self, actor: dict | None) -> Operator:
        session = self._session(actor)
        if session is None:
            raise RailError(DENY_MESSAGES[DENY_BAD_SESSION], component="shift")
        operator = self.operators.require_enabled(session["operator_id"])
        if not operator.is_leader:
            raise RailError("该动作需要班组长权限", component="shift")
        return operator

    def _require_leader_identity(self, actor: dict | None) -> Operator:
        """解析班组长身份：优先有效会话，其次在册身份自证（开班前引导用）。"""

        session = self._session(actor)
        operator_id = session["operator_id"] if session else str((actor or {}).get("operator_id", ""))
        if not operator_id:
            raise RailError(DENY_MESSAGES[DENY_BAD_SESSION], component="shift")
        operator = self.operators.require_enabled(operator_id)
        if not operator.is_leader:
            raise RailError("该动作需要班组长权限", component="shift")
        return operator

    def _active_or_frozen(self) -> Shift | None:
        return self.shifts.active()

    # ================================================================
    # 班次计划与开班
    # ================================================================

    def plan_shift(self, actor: dict | None, shift_id: str) -> dict:
        """计划一个新班次。冷启动自举；已启用后仅班组长可计划。"""

        if not shift_id:
            raise RailError("班次标识为空", component="shift")
        if self.shifts.get(shift_id) is not None:
            raise RailError(f"班次已存在: {shift_id}", component="shift")
        if not self._bootstrap():
            leader = self._require_leader_identity(actor)
            actor_id = leader.id
        else:
            actor_id = self._session(actor)["operator_id"] if self._session(actor) else str(
                (actor or {}).get("operator_id", "bootstrap")
            )
        with self._lock:
            self._append("shift_planned", actor_id, {"shift_id": shift_id})
            self._apply_shift_planned({"shift_id": shift_id})
            self._audit.record("shift_planned", "", "", shift_id=shift_id, by=actor_id)
        return {"ok": True, "shift_id": shift_id, "state": SHIFT_PLANNED}

    def open_shift(self, actor: dict | None, shift_id: str) -> dict:
        """开班接班：计划班次转在值，当前不得有其他在值/冻结班次。"""

        shift = self.shifts.require(shift_id)
        if shift.state != SHIFT_PLANNED:
            raise RailError(f"班次 {shift_id} 当前 {shift.state}，不能开班", component="shift")
        current = self._active_or_frozen()
        if current is not None:
            raise RailError(f"值班台被班次 {current.id} 占用，先完成交接", component="shift")
        operator_id = self._actor_id(actor)
        operator = self.operators.require_enabled(operator_id)
        if operator_id not in shift.roster and operator.id != shift.leader_id:
            raise RailError("开班人必须在班次名册中", component="shift")
        at = self._clock.now()
        with self._lock:
            # 开班即接管新代际；本班签到者会话提升到新代际，他班会话保留但无法
            # 通过在值班次闸门（见 guard）。
            self._epoch += 1
            payload = {
                "shift_id": shift_id,
                "roster": list(shift.roster),
                "leader_id": shift.leader_id,
                "by": operator_id,
                "at": at,
                "epoch": self._epoch,
            }
            self._append("shift_opened", operator_id, payload)
            self._apply_shift_opened(payload)
            # 开班：作废旧班残留会话；本班提前签到者的会话提升到新代际继续有效。
            self._activate_shift_sessions(shift_id, self._epoch)
            self._audit.record("shift_opened", "", "", shift_id=shift_id, by=operator_id, epoch=self._epoch)
            discovered = self.discover_open_items(actor)
        return {
            "ok": True,
            "shift_id": shift_id,
            "state": SHIFT_ACTIVE,
            "epoch": self._epoch,
            "discovered_items": discovered,
        }

    def roster_incoming(
        self,
        actor: dict | None,
        shift_id: str,
        roster: list[str],
        leader_id: str = "",
    ) -> dict:
        """班组长为计划中的接班班次编排值班员与班组长。

        开班前还没有值班会话，班组长以在册身份自证（``operator_id``）即可；
        在值期间同样接受会话令牌。
        """

        leader = self._require_leader_identity(actor)
        shift = self.shifts.require(shift_id)
        if shift.state != SHIFT_PLANNED:
            raise RailError(f"接班班次 {shift_id} 必须是计划态", component="shift")
        members = [str(item) for item in roster if item]
        if not members:
            raise RailError("接班班次至少需要一名值班员", component="shift")
        for member in members:
            self.operators.require_enabled(member)
        if leader_id:
            leader_op = self.operators.require_enabled(leader_id)
            if not leader_op.is_leader:
                raise RailError(f"{leader_id} 不是班组长", component="shift")
        with self._lock:
            payload = {"shift_id": shift_id, "roster": members, "leader_id": leader_id}
            self._append("shift_rostered", leader.id, payload)
            self._apply_shift_rostered(payload)
            self._audit.record(
                "shift_rostered", "", "", shift_id=shift_id, leader_id=leader_id, roster=members
            )
        return {"ok": True, "shift_id": shift_id, "roster": members, "leader_id": leader_id}

    # ================================================================
    # 交接主流程
    # ================================================================

    def begin_handover(
        self,
        actor: dict | None,
        incoming_shift_id: str = "",
        *,
        deadline_s: float | None = None,
    ) -> dict:
        """交班人开启交接：值班台立即进入硬冻结，并逐项列出在办事项。"""

        shift = self._require_current_active()
        actor_id = self._actor_id(actor)
        if actor_id != shift.leader_id and actor_id not in shift.roster:
            raise RailError("只有本班值班员或班组长能发起交接", component="shift")
        incoming: Shift | None = None
        if incoming_shift_id:
            incoming = self.shifts.require(incoming_shift_id)
            if incoming.state != SHIFT_PLANNED:
                raise RailError(f"接班班次 {incoming_shift_id} 必须是计划态", component="shift")
        wait = self._deadline_s if deadline_s is None else float(deadline_s)
        if wait <= 0:
            raise RailError("交接确认时限必须为正", component="shift")
        at = self._clock.now()

        with self._lock:
            if self.handovers.open_one() is not None:
                raise RailError("已有未完成的交接", component="shift")
            self.discover_open_items(actor)
            open_items = [
                item for item in self.items.open_items() if item.owner_shift_id == shift.id
            ]
            handover_id = f"HO-{self.ledger.length() + 1:06d}"
            handover = Handover(
                id=handover_id,
                shift_id=shift.id,
                incoming_shift_id=incoming.id if incoming else "",
                state=HO_OPEN,
                opened_at=at,
                opened_by=actor_id,
                deadline=at + wait,
                item_ids=[item.id for item in open_items],
            )
            payload = {
                "handover": handover.as_dict(),
                "handover_id": handover_id,
                "shift_id": shift.id,
                "incoming_shift_id": handover.incoming_shift_id,
                "item_ids": handover.item_ids,
                "deadline": handover.deadline,
                "at": at,
            }
            self._append("handover_began", actor_id, payload)
            self._apply_handover_began(payload)
            self._audit.record(
                "handover_began",
                "",
                "",
                handover_id=handover_id,
                shift_id=shift.id,
                incoming=handover.incoming_shift_id,
                items=len(handover.item_ids),
            )
        return {
            "ok": True,
            "handover_id": handover.id,
            "state": HO_OPEN,
            "shift_id": shift.id,
            "incoming_shift_id": handover.incoming_shift_id,
            "deadline": handover.deadline,
            "items": [self._item_brief(item_id) for item_id in handover.item_ids],
            "frozen": True,
        }

    def acknowledge_item(self, actor: dict | None, handover_id: str, item_id: str) -> dict:
        """接班人逐项确认一项在办事项。"""

        session = self._require_session(actor)
        handover = self.handovers.require(handover_id)
        if handover.state != HO_OPEN:
            raise RailError(f"交接 {handover_id} 不在确认阶段（{handover.state}）", component="shift")
        incoming = self.shifts.require(handover.incoming_shift_id) if handover.incoming_shift_id else None
        actor_id = session["operator_id"]
        if incoming is not None and actor_id not in incoming.roster and actor_id != incoming.leader_id:
            raise RailError("只有接班班次名册中的值班员能逐项确认", component="shift")
        item = self.items.require(item_id)
        if item_id not in handover.item_ids:
            raise RailError(f"事项 {item_id} 不属于本次交接", component="shift")
        if item.state != WI_PRESENTED:
            raise RailError(f"事项 {item_id} 当前 {item.state}，无需确认", component="shift")
        at = self._clock.now()
        with self._lock:
            payload = {"handover_id": handover_id, "item_id": item_id, "by": actor_id, "at": at}
            self._append("work_acknowledged", actor_id, payload)
            self._apply_work_acknowledged(payload)
            self._audit.record(
                "handover_ack", "", "", handover_id=handover_id, item_id=item_id, by=actor_id
            )
        return {"ok": True, "item_id": item_id, "state": WI_ACKNOWLEDGED, "by": actor_id}

    def suspend_pending(self, actor: dict | None, handover_id: str, reason: str = "") -> dict:
        """人工挂起：确认时限之前只有班组长能提前执行（接受会话或在册身份自证）。"""

        handover = self.handovers.require(handover_id)
        if handover.state != HO_OPEN:
            raise RailError(f"交接 {handover_id} 不在确认阶段", component="shift")
        session = self._session(actor)
        actor_id = session["operator_id"] if session else self._require_leader_identity(actor).id
        operator = self.operators.require_enabled(actor_id)
        early = self._clock.now() < handover.deadline
        if early and not operator.is_leader:
            raise RailError("确认时限未到，只有班组长能提前挂起未交事项", component="shift")
        return self._auto_suspend(handover, actor_id, reason or "leader_suspend")

    def _auto_suspend(self, handover: Handover, actor_id: str, reason: str) -> dict:
        """挂起内核：到点系统挂起与人工提前挂起共用同一条状态路径。"""

        remaining = handover.remaining()
        at = self._clock.now()
        with self._lock:
            if remaining:
                payload = {
                    "handover_id": handover.id,
                    "item_ids": remaining,
                    "reason": reason,
                    "at": at,
                }
                self._append("work_suspended", actor_id, payload)
                self._apply_work_suspended(payload)
                self._audit.record(
                    "handover_suspended",
                    "",
                    "",
                    handover_id=handover.id,
                    items=remaining,
                    reason=reason,
                )
        return {
            "ok": True,
            "handover_id": handover.id,
            "state": HO_REVIEW if remaining else HO_OPEN,
            "suspended": remaining,
        }

    def countersign(self, actor: dict | None, handover_id: str) -> dict:
        """班组长对一次含挂起项的交接复核会签。

        会签可能发生在值班台冻结、接班人尚未签到的时刻，班组长以在册身份自证。
        """

        leader = self._require_leader_identity(actor)
        handover = self.handovers.require(handover_id)
        if handover.state not in (HO_OPEN, HO_REVIEW):
            raise RailError(f"交接 {handover_id} 已结束，不能会签", component="shift")
        if not handover.suspended:
            raise RailError("没有挂起项的交接无需班组长会签", component="shift")
        outgoing = self.shifts.require(handover.shift_id)
        if leader.id == handover.opened_by or leader.id == outgoing.leader_id or leader.id in outgoing.roster:
            raise RailError("会签班组长不能来自交班班次，必须由独立的第三方班组长复核", component="shift")
        at = self._clock.now()
        with self._lock:
            payload = {"handover_id": handover_id, "leader_id": leader.id, "at": at}
            self._append("handover_signed", leader.id, payload)
            self._apply_handover_signed(payload)
            self._audit.record(
                "handover_countersign", "", "", handover_id=handover_id, leader_id=leader.id
            )
        return {"ok": True, "handover_id": handover_id, "leader_id": leader.id}

    def complete_handover(self, actor: dict | None, handover_id: str) -> dict:
        """完成交接：零挂起双方签认即可；有挂起必须先经班组长会签。"""

        handover = self.handovers.require(handover_id)
        if handover.state not in (HO_OPEN, HO_REVIEW):
            raise RailError(f"交接 {handover_id} 已结束", component="shift")
        actor_id = self._actor_id(actor)
        session = self._require_session(actor)

        remaining = [
            item_id
            for item_id in handover.remaining()
            if item_id not in handover.suspended
        ]
        if remaining:
            raise RailError(f"还有 {len(remaining)} 项未逐项确认，不能完成交接", component="shift")
        suspended = list(handover.suspended)
        if suspended and not handover.leader_id:
            raise RailError("存在挂起项，必须先由班组长复核会签", component="shift")
        if not handover.incoming_shift_id:
            raise RailError("接班班次缺位，不能正常完成交接，请走强制收口", component="shift")
        incoming = self.shifts.require(handover.incoming_shift_id)
        if actor_id not in incoming.roster and actor_id != incoming.leader_id:
            raise RailError("只有接班值班员能签收完成交接", component="shift")
        if handover.leader_id and handover.leader_id == actor_id:
            raise RailError("签收人不能同时是会签班组长", component="shift")

        at = self._clock.now()
        with self._lock:
            self._epoch += 1
            # 已确认事项与挂起事项一并移交归属；挂起项保持 SUSPENDED、继续扣
            # 发车放行，等接班班组长会签后才能恢复（见 resume_item）。
            transferred = list(handover.acknowledged)
            for item_id in suspended:
                if item_id not in transferred:
                    transferred.append(item_id)
            payload = {
                "handover_id": handover_id,
                "handover": handover.as_dict(),
                "shift_id": handover.shift_id,
                "incoming_shift_id": incoming.id,
                "by": actor_id,
                "leader_id": handover.leader_id,
                "forced": False,
                "reason": "",
                "transferred": transferred,
                "suspended": suspended,
                "epoch": self._epoch,
                "at": at,
            }
            self._append("handover_completed", actor_id, payload)
            self._apply_handover_completed(payload)
            # 接班人签到用于逐项确认，交接完成后其会话提升到新代际继续有效；
            # 交班班次会话作废，杜绝跨班迟到命令。
            self._activate_shift_sessions(incoming.id, self._epoch)
            self._audit.record(
                "handover_completed",
                "",
                "",
                handover_id=handover_id,
                shift_id=handover.shift_id,
                incoming=incoming.id,
                epoch=self._epoch,
                suspended=suspended,
            )
        return {
            "ok": True,
            "handover_id": handover_id,
            "state": HO_DONE,
            "active_shift": incoming.id,
            "epoch": self._epoch,
            "transferred": transferred,
            "suspended": suspended,
            "release_blocked": bool(suspended),
        }

    def force_close(self, actor: dict | None, handover_id: str, reason: str) -> dict:
        """接班人始终缺位时，由班组长强制收口：当班结束、事项挂起成孤儿。"""

        leader = self._require_leader_identity(actor)
        handover = self.handovers.require(handover_id)
        if handover.state not in (HO_OPEN, HO_REVIEW):
            raise RailError(f"交接 {handover_id} 已结束", component="shift")
        if handover.incoming_shift_id:
            raise RailError("接班班次已排班，应完成正常交接而非强制收口", component="shift")
        if not reason:
            raise RailError("强制收口必须填写原因", component="shift")
        at = self._clock.now()
        with self._lock:
            remaining = handover.remaining()
            if remaining:
                suspend_payload = {
                    "handover_id": handover_id,
                    "item_ids": remaining,
                    "reason": "forced_close",
                    "at": at,
                }
                self._append("work_suspended", leader.id, suspend_payload)
                self._apply_work_suspended(suspend_payload)
            handover = self.handovers.require(handover_id)
            self._epoch += 1
            open_ids = [item.id for item in self.items.open_items() if item.owner_shift_id == handover.shift_id]
            payload = {
                "handover_id": handover_id,
                "handover": handover.as_dict(),
                "shift_id": handover.shift_id,
                "incoming_shift_id": "",
                "by": leader.id,
                "leader_id": leader.id,
                "forced": True,
                "reason": reason,
                "transferred": open_ids,
                "suspended": list(handover.suspended),
                "epoch": self._epoch,
                "at": at,
            }
            self._append("handover_completed", leader.id, payload)
            self._apply_handover_completed(payload)
            self._invalidate_sessions()
            self._audit.record(
                "handover_forced",
                "",
                "",
                handover_id=handover_id,
                shift_id=handover.shift_id,
                leader_id=leader.id,
                reason=reason,
                items=open_ids,
            )
        return {
            "ok": True,
            "handover_id": handover_id,
            "state": HO_FORCED,
            "epoch": self._epoch,
            "orphans": open_ids,
            "reason": reason,
        }

    def adopt_orphans(self, actor: dict | None, item_ids: list[str] | None = None) -> dict:
        """新班在值后，由班组长认领强制收口留下的无主挂起事项。"""

        leader = self._require_leader(actor)
        shift = self._require_current_active()
        orphans = self.items.orphans()
        targets = [item for item in orphans if item_ids is None or item.id in item_ids]
        if item_ids is not None and len(targets) != len(set(item_ids)):
            raise RailError("存在不属于无主挂起集合的事项标识", component="shift")
        if not targets:
            return {"ok": True, "adopted": [], "shift_id": shift.id}
        at = self._clock.now()
        ids = [item.id for item in targets]
        with self._lock:
            payload = {
                "shift_id": shift.id,
                "item_ids": ids,
                "by": leader.id,
                "at": at,
            }
            self._append("orphan_adopted", leader.id, payload)
            self._apply_orphan_adopted(payload)
            self._audit.record(
                "shift_orphans_adopted", "", "", shift_id=shift.id, items=ids, leader_id=leader.id
            )
        return {"ok": True, "adopted": ids, "shift_id": shift.id}

    # ================================================================
    # 在办事项
    # ================================================================

    def register_item(
        self,
        actor: dict | None,
        kind: str,
        title: str,
        *,
        ref: str = "",
        station_id: str = "",
        payload: dict | None = None,
    ) -> dict:
        """交班班内登记一件在办事项（只有在值、非冻结时允许）。"""

        session = self._require_session(actor)
        shift = self._require_current_active()
        if shift.state == SHIFT_FROZEN:
            raise RailError(DENY_MESSAGES[DENY_FROZEN], component="shift")
        if kind not in (WI_BATCH, WI_FAULT, WI_COMMAND, WI_GENERIC):
            raise RailError(f"未知事项类型: {kind}", component="shift")
        if not title:
            raise RailError("事项标题为空", component="shift")
        item = WorkItem(
            id=f"WI-{self.ledger.length() + 1:06d}",
            kind=kind,
            title=title,
            ref=ref,
            station_id=station_id,
            state=WI_OPEN,
            owner_shift_id=shift.id,
            created_by=session["operator_id"],
            created_at=self._clock.now(),
            payload=dict(payload or {}),
        )
        with self._lock:
            p = {"item": item.as_dict()}
            self._append("work_registered", session["operator_id"], p)
            self._apply_work_registered(p)
            self._audit.record(
                "work_registered",
                "",
                station_id,
                item_id=item.id,
                kind=kind,
                ref=ref,
            )
        return {"ok": True, "item": self._item_brief(item.id)}

    def resume_item(self, actor: dict | None, item_id: str, leader_id: str = "") -> dict:
        """恢复一件事项。

        * SUSPENDED（自动挂起 / 强制收口孤儿已认领）：必须班组长会签；
        * ACKNOWLEDGED 的安全类事项（批量开门 / 故障 / 门控指令）：同样必须班组长
          会签，并由平台回调按**当前**现场重新过联锁后才能继续；
        * ACKNOWLEDGED 的通用事项：接班人自行恢复即可。
        """

        session = self._require_session(actor)
        shift = self._require_current_active()
        item = self.items.require(item_id)
        actor_id = session["operator_id"]
        if item.owner_shift_id != shift.id:
            raise RailError("事项不属于当前在值班次", component="shift")
        if item.state not in (WI_SUSPENDED, WI_ACKNOWLEDGED):
            raise RailError(f"事项 {item_id} 当前 {item.state}，不能恢复", component="shift")

        cosign = ""
        if item.state == WI_SUSPENDED or item.kind in SAFETY_ITEM_KINDS:
            if not leader_id:
                raise RailError("恢复安全动作必须经班组长会签（leader_id）", component="shift")
            leader = self.operators.require_enabled(leader_id)
            if not leader.is_leader:
                raise RailError(f"{leader_id} 不是班组长", component="shift")
            if leader.id == actor_id:
                raise RailError("会签班组长不能是恢复操作人本人", component="shift")
            cosign = leader.id

        result: dict = {}
        with self._lock:
            # 安全动作恢复：交给平台按当前事实重新过联锁，不盲信交接快照。
            if item.kind == WI_BATCH:
                continuer = self.hooks.get("continue_batch")
                if continuer is None:
                    raise RailError("批量开门恢复回调未装配", component="shift")
                result = continuer(item)
                if not result.get("ok", False):
                    raise RailError(
                        f"联锁复核未通过，批量开门保持挂起: {result.get('violations', [])}",
                        component="shift",
                    )
                item = self.items.require(item_id)
            at = self._clock.now()
            payload = {"item_id": item_id, "by": actor_id, "leader_id": cosign, "at": at}
            self._append("work_resumed", actor_id, payload)
            self._apply_work_resumed(payload)
            self._audit.record(
                "work_resumed",
                "",
                item.station_id,
                item_id=item_id,
                item_kind=item.kind,
                leader_id=cosign,
            )
        return {"ok": True, "item_id": item_id, "state": WI_RESUMED, "leader_id": cosign, **result}

    def complete_item(self, actor: dict | None, item_id: str) -> dict:
        """办结一件事项（在值值班员或班组长）。"""

        session = self._require_session(actor)
        shift = self._require_current_active()
        item = self.items.require(item_id)
        if item.owner_shift_id != shift.id:
            raise RailError("事项不属于当前在值班次", component="shift")
        if item.state not in (WI_OPEN, WI_ACKNOWLEDGED, WI_RESUMED):
            raise RailError(f"事项 {item_id} 当前 {item.state}，不能办结", component="shift")
        at = self._clock.now()
        with self._lock:
            payload = {"item_id": item_id, "by": session["operator_id"], "at": at}
            self._append("work_completed", session["operator_id"], payload)
            self._apply_work_completed(payload)
            self._audit.record("work_completed", "", item.station_id, item_id=item_id, item_kind=item.kind)
        return {"ok": True, "item_id": item_id, "state": WI_DONE}

    def abort_item(self, actor: dict | None, item_id: str, reason: str) -> dict:
        """终止一件事项。批量开门走安全收口（关好已开门）；故障不允许终止。"""

        session = self._require_session(actor)
        shift = self._require_current_active()
        item = self.items.require(item_id)
        if item.owner_shift_id != shift.id:
            raise RailError("事项不属于当前在值班次", component="shift")
        if not reason:
            raise RailError("终止事项必须填写原因", component="shift")
        if item.kind == WI_FAULT:
            raise RailError("挂起故障不能终止，必须走故障清除流程", component="shift")
        if item.state not in (WI_OPEN, WI_ACKNOWLEDGED, WI_RESUMED, WI_SUSPENDED):
            raise RailError(f"事项 {item_id} 当前 {item.state}，不能终止", component="shift")
        with self._lock:
            if item.kind == WI_BATCH:
                secure = self.hooks.get("secure_close_batch")
                if secure is None:
                    raise RailError("批量开门收口回调未装配", component="shift")
                secure(item)
            at = self._clock.now()
            payload = {
                "item_id": item_id,
                "by": session["operator_id"],
                "reason": reason,
                "at": at,
            }
            self._append("work_aborted", session["operator_id"], payload)
            self._apply_work_aborted(payload)
            self._audit.record(
                "work_aborted", "", item.station_id, item_id=item_id, reason=reason
            )
        return {"ok": True, "item_id": item_id, "state": WI_ABORTED, "reason": reason}

    # ================================================================
    # 平台现场回调：故障 / 批量开门 自动入项
    # ================================================================

    def note_fault_set(self, psd_id: str, station_id: str = "") -> None:
        """平台置位故障后调用：在值期间自动挂账一件故障事项。"""

        shift = self.shifts.active()
        if shift is None or shift.state != SHIFT_ACTIVE:
            return
        if self.items.by_ref(WI_FAULT, psd_id) is not None:
            return
        item = WorkItem(
            id=f"WI-{self.ledger.length() + 1:06d}",
            kind=WI_FAULT,
            title=f"门区故障挂起: {psd_id}",
            ref=psd_id,
            station_id=station_id,
            state=WI_OPEN,
            owner_shift_id=shift.id,
            created_by="system",
            created_at=self._clock.now(),
        )
        with self._lock:
            p = {"item": item.as_dict()}
            self._append("work_registered", "system", p)
            self._apply_work_registered(p)
            self._audit.record("work_registered", "", station_id, item_id=item.id, item_kind=WI_FAULT, ref=psd_id)

    def note_fault_cleared(self, psd_id: str) -> None:
        """平台清除故障后调用：自动办结对应故障事项。"""

        item = self.items.by_ref(WI_FAULT, psd_id)
        if item is None or item.state in (WI_DONE, WI_ABORTED):
            return
        with self._lock:
            at = self._clock.now()
            payload = {"item_id": item.id, "by": "system", "at": at}
            self._append("work_completed", "system", payload)
            self._apply_work_completed(payload)
            self._audit.record("work_completed", "", item.station_id, item_id=item.id, item_kind=WI_FAULT)

    def note_batch_open(self, station_id: str, outcome: dict) -> dict | None:
        """平台批量开门后调用：有未完成门区时登记/更新一件批量开门事项。"""

        shift = self.shifts.active()
        if shift is None or shift.state != SHIFT_ACTIVE:
            return None
        deferred = list(outcome.get("deferred", []))
        skipped = dict(outcome.get("skipped", {}))
        if not deferred and not skipped:
            # 本批一次开完，若曾挂账则办结。
            existing = self.items.by_ref(WI_BATCH, station_id)
            if existing is not None and existing.owner_shift_id == shift.id:
                with self._lock:
                    payload = {"item_id": existing.id, "by": "system", "at": self._clock.now()}
                    self._append("work_completed", "system", payload)
                    self._apply_work_completed(payload)
            return None
        existing = self.items.by_ref(WI_BATCH, station_id)
        snapshot = {
            "order": list(outcome.get("order", [])),
            "opened": list(outcome.get("opened", [])),
            "deferred": deferred,
            "skipped": skipped,
        }
        with self._lock:
            if existing is None or existing.owner_shift_id != shift.id:
                item = WorkItem(
                    id=f"WI-{self.ledger.length() + 1:06d}",
                    kind=WI_BATCH,
                    title=f"批量开门未完成: {station_id}",
                    ref=station_id,
                    station_id=station_id,
                    state=WI_OPEN,
                    owner_shift_id=shift.id,
                    created_by="system",
                    created_at=self._clock.now(),
                    payload=snapshot,
                )
                p = {"item": item.as_dict()}
                self._append("work_registered", "system", p)
                self._apply_work_registered(p)
                self._audit.record(
                    "work_registered", "", station_id, item_id=item.id, item_kind=WI_BATCH, ref=station_id
                )
                return self._item_brief(item.id)
            existing.payload = snapshot
            if existing.state == WI_OPEN:
                self.items.save(existing)
            return self._item_brief(existing.id)

    def discover_open_items(self, actor: dict | None = None) -> list[str]:
        """开班 / 开交接时，从现场重新发现未入项的在办事项（故障、半截批量）。"""

        shift = self.shifts.active()
        if shift is None or shift.state != SHIFT_ACTIVE:
            return []
        discovered: list[str] = []
        active_faults = self.hooks.get("active_fault_ids")
        if active_faults is not None:
            for psd_id in active_faults():
                if self.items.by_ref(WI_FAULT, psd_id) is None:
                    self.note_fault_set(psd_id)
                    item = self.items.by_ref(WI_FAULT, psd_id)
                    if item is not None:
                        discovered.append(item.id)
        discoverer = self.hooks.get("discover_batches")
        if discoverer is not None:
            for station_id, outcome in discoverer().items():
                brief = self.note_batch_open(station_id, outcome)
                if brief is not None:
                    discovered.append(brief["id"])
        return discovered

    # ================================================================
    # 门控 / 联锁统一闸门与发车放行
    # ================================================================

    def managed(self) -> bool:
        """是否已启用班次管理（台账里出现过开班）。"""

        return any(shift.state in (SHIFT_ACTIVE, SHIFT_FROZEN) for shift in self.shifts.list()) or bool(
            [s for s in self.shifts.list() if s.state in (SHIFT_HANDED, SHIFT_FORCED)]
        )

    def guard(self, actor: dict | None) -> dict:
        """安全动作统一闸门，返回 ``{"allowed": bool, "reason": str, "shift_id": str}``。

        * 未启用班次管理：放行（系统按无班模式运行，旧行为不变）；
        * 在值且会话有效、代际匹配：放行；
        * 冻结窗口：一律拒绝；
        * 会话无效 / 非本班 / 旧代际：拒绝并给出区分原因。
        """

        shift = self.shifts.active()
        if shift is None:
            if not self.managed():
                return {"allowed": True, "reason": "", "shift_id": "", "epoch": self._epoch}
            return {"allowed": False, "reason": DENY_NO_DESK, "shift_id": "", "epoch": self._epoch}
        session = self._session(actor)
        if session is None:
            return {"allowed": False, "reason": DENY_BAD_SESSION, "shift_id": shift.id, "epoch": self._epoch}
        if session["operator_id"] not in shift.roster and session["operator_id"] != shift.leader_id:
            return {"allowed": False, "reason": DENY_NOT_ON_DUTY, "shift_id": shift.id, "epoch": self._epoch}
        if session["epoch"] != self._epoch:
            return {"allowed": False, "reason": DENY_STALE_EPOCH, "shift_id": shift.id, "epoch": self._epoch}
        if shift.state == SHIFT_FROZEN:
            return {"allowed": False, "reason": DENY_FROZEN, "shift_id": shift.id, "epoch": self._epoch}
        return {"allowed": True, "reason": "", "shift_id": shift.id, "epoch": self._epoch}

    def require_allowed(self, actor: dict | None) -> dict:
        """命令式闸门：不满足直接抛联锁异常。"""

        verdict = self.guard(actor)
        if not verdict["allowed"]:
            raise RailError(DENY_MESSAGES.get(verdict["reason"], verdict["reason"]), component="shift")
        return verdict

    def release_blockers(self) -> list[dict]:
        """返回当前扣住发车放行的在办事项（OPEN / PRESENTED / SUSPENDED）。

        判定与归属无关：只要全台还存在未交接完或挂起的安全事项，任何一列车都
        不得放行。ACKNOWLEDGED / RESUMED / DONE / ABORTED 不拦截。
        """

        blockers: list[dict] = []
        for item in self.items.list():
            if item.state not in ("open", "presented", "suspended"):
                continue
            blockers.append(
                {
                    "item_id": item.id,
                    "kind": item.kind,
                    "state": item.state,
                    "title": item.title,
                    "ref": item.ref,
                    "station_id": item.station_id,
                    "owner_shift_id": item.owner_shift_id,
                    "reason": "suspended" if item.state == WI_SUSPENDED else "handover_incomplete",
                }
            )
        return sorted(blockers, key=lambda row: row["item_id"])

    def check_release(self, train_id: str, station_id: str) -> dict:
        """发车放行联动：返回可否放行；拒绝与放行都进不可变台账。"""

        blockers = self.release_blockers()
        at = self._clock.now()
        with self._lock:
            if blockers:
                self._append(
                    "release_denied",
                    "system",
                    {
                        "train_id": train_id,
                        "station_id": station_id,
                        "blockers": blockers,
                        "at": at,
                    },
                )
                self._audit.record(
                    "departure_release_denied",
                    train_id,
                    station_id,
                    blockers=[row["item_id"] for row in blockers],
                )
                return {"allowed": False, "train_id": train_id, "blockers": blockers}
            self._append(
                "release_granted",
                "system",
                {"train_id": train_id, "station_id": station_id, "at": at},
            )
        return {"allowed": True, "train_id": train_id, "blockers": []}

    # ================================================================
    # 周期推进与查询
    # ================================================================

    def tick(self) -> dict:
        """控制周期调用：到点以系统身份自动挂起未确认事项。"""

        actions: list[dict] = []
        handover = self.handovers.open_one()
        if handover is not None and handover.state == HO_OPEN and self._clock.now() >= handover.deadline:
            result = self._auto_suspend(handover, "system", "deadline")
            actions.append({"handover_id": handover.id, "suspended": result["suspended"]})
        return {"ok": True, "timeouts": actions}

    def handover_view(self, handover_id: str) -> dict:
        handover = self.handovers.require(handover_id)
        return {
            "handover": handover.as_dict(),
            "items": [self.items.require(item_id).as_dict() for item_id in handover.item_ids],
            "remaining": handover.remaining(),
            "release_blocked": bool(self.release_blockers()),
        }

    def desk(self) -> dict:
        """值班台综合状态（供平台 status / 控制台）。"""

        shift = self.shifts.active()
        handover = self.handovers.open_one()
        blockers = self.release_blockers()
        return {
            "managed": self.managed(),
            "epoch": self._epoch,
            "active_shift": shift.id if shift and shift.state == SHIFT_ACTIVE else "",
            "frozen_shift": shift.id if shift and shift.state == SHIFT_FROZEN else "",
            "shift_state": shift.state if shift else "",
            "open_handover": handover.id if handover else "",
            "handover_state": handover.state if handover else "",
            "deadline": handover.deadline if handover else 0.0,
            "release_blocked": bool(blockers),
            "blockers": blockers,
            "open_item_count": len(self.items.open_items()),
            "orphans": [item.id for item in self.items.orphans()],
            "sessions": len(self._sessions),
        }

    def list_items(self, include_done: bool = False) -> list[dict]:
        items = self.items.list()
        if not include_done:
            items = [item for item in items if item.state not in (WI_DONE, WI_ABORTED)]
        return [item.as_dict() for item in items]

    def list_handovers(self) -> list[dict]:
        return [handover.as_dict() for handover in self.handovers.list()]

    def verify_ledger(self) -> dict:
        """校验哈希链（巡检入口）。"""

        return self.ledger.verify().as_dict()

    def ledger_tail(self, limit: int = 30) -> list[dict]:
        return [entry.as_dict() for entry in self.ledger.tail(limit)]

    # ================================================================
    # 内部辅助
    # ================================================================

    def _actor_id(self, actor: dict | None, *, allow_system: bool = False) -> str:
        session = self._session(actor)
        if session is not None:
            return session["operator_id"]
        raw = str((actor or {}).get("operator_id", ""))
        if raw:
            return raw
        if allow_system:
            return "system"
        raise RailError(DENY_MESSAGES[DENY_BAD_SESSION], component="shift")

    def _require_session(self, actor: dict | None) -> dict:
        session = self._session(actor)
        if session is None:
            raise RailError(DENY_MESSAGES[DENY_BAD_SESSION], component="shift")
        return session

    def _require_current_active(self) -> Shift:
        shift = self._active_or_frozen()
        if shift is None:
            raise RailError("当前没有在值或交接中的班次", component="shift")
        return shift

    def _invalidate_sessions(self) -> None:
        """作废全部会话。交接完成/强制收口后，所有人用新代际重新签到。"""

        self._sessions.clear()

    def _activate_shift_sessions(self, shift_id: str, epoch: int) -> None:
        """开班时把本班会话提升到新代际，并清除其他班次的残留会话。"""

        keep = {
            token: {"operator_id": meta["operator_id"], "shift_id": shift_id, "epoch": epoch}
            for token, meta in self._sessions.items()
            if meta.get("shift_id") == shift_id
        }
        self._sessions.clear()
        self._sessions.update(keep)

    def _item_brief(self, item_id: str) -> dict:
        item = self.items.require(item_id)
        return {
            "id": item.id,
            "kind": item.kind,
            "title": item.title,
            "state": item.state,
            "ref": item.ref,
            "station_id": item.station_id,
        }
