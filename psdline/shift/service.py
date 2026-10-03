"""班次交接编排。

交接流程固定为「交班发起 → 接班到岗 → 逐项确认 → 复核完成」：发起时把在办
事项快照进交接单，接班后未确认的事项自动挂起并卡住发车放行，接班人逐项确
认后接着办，全部确认后由班组长复核封存（配置可免复核，末项确认后自动完
成）。权限、状态迁移与审计都集中在这里；门控与联锁只查询挂起状态，不直接
改交接单。每次状态迁移只写一个原子文档，重启后由 ``recover`` 按交接单与班
次指针把半成品迁移修复成确定状态。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from ..audit import AuditLog
from ..clock import Clock
from ..errors import RailError
from ..eventbus import Event, EventBus
from ..ids import IdFactory
from ..store import JsonStore
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
    STATION_KINDS,
    TRAIN_KINDS,
    OpenItem,
    note_batch_remainder,
)
from .ledger import ACTIVE, CLOSED, HANDING_OVER, Shift, ShiftRegistry
from .roster import OPERATOR, SUPERVISOR, Roster


class ShiftService:
    """班次与交接的编排入口。"""

    def __init__(
        self,
        store: JsonStore,
        clock: Clock,
        bus: EventBus,
        audit: AuditLog,
        ids: IdFactory,
        *,
        require_supervisor: bool = True,
        handover_timeout_s: float = 1800.0,
        collect: Callable[[float], list[OpenItem]] | None = None,
    ) -> None:
        self._clock = clock
        self._bus = bus
        self._audit = audit
        self._ids = ids
        self._store = store
        self._require_supervisor = bool(require_supervisor)
        self._timeout_s = float(handover_timeout_s)
        self._collect = collect or (lambda now: [])
        self.roster = Roster(store)
        self.shifts = ShiftRegistry(store)
        self.handovers = HandoverRegistry(store)

    # ---- 名册 ---------------------------------------------------------

    def seed(self, operators: tuple[str, ...], supervisors: tuple[str, ...]) -> None:
        """空名册时按配置播种第一批人员。"""

        if not self.roster.empty():
            return
        for name in operators:
            self.roster.register(name, OPERATOR)
        for name in supervisors:
            self.roster.register(name, SUPERVISOR)

    def register(self, name: str, role: str, by: str = "") -> dict:
        """登记人员；名册非空时只有班组长能登记。"""

        if not self.roster.empty():
            self.roster.require(by, SUPERVISOR)
        result = self.roster.register(name, role)
        self._record("shift_roster_registered", name=name, role=role, by=by or "bootstrap")
        self._emit("roster_registered", name=name, role=role)
        return {"ok": True, **result, "violations": []}

    # ---- 班次生命周期 ---------------------------------------------------

    def open_shift(self, operator: str, label: str = "") -> dict:
        """开第一个班；已有未封存班次时必须走交接，不能直接开班。"""

        self.roster.require(operator, OPERATOR)
        current = self.shifts.current()
        if current is not None and current.state != CLOSED:
            if current.state == HANDING_OVER:
                raise RailError("当前班次正在交接，接班请走接班入口", component="shift")
            raise RailError(
                f"班次 {current.id} 仍在当班，开班前必须先交接", component="shift"
            )
        if self.handovers.active() is not None:
            raise RailError("存在进行中的交接，接班请走接班入口", component="shift")
        shift = Shift(
            id=self._ids.labeled("shift"),
            label=label,
            operator=operator,
            state=ACTIVE,
            opened_at=self._clock.now(),
        )
        self.shifts.open(shift)
        self._record("shift_opened", shift=shift)
        self._emit("shift_opened", shift_id=shift.id, operator=operator)
        return {"ok": True, "shift": shift.as_dict(), "violations": []}

    def accept_shift(self, operator: str, label: str = "") -> dict:
        """接班到岗：开新班次，未确认的事项自动挂起。"""

        self.roster.require(operator, OPERATOR)
        handover = self.handovers.active()
        if handover is None:
            raise RailError("没有待接班的交接", component="shift")
        if handover.state != OFFERED:
            raise RailError("交接已在接班流程中，不能重复接班", component="shift")
        from_shift = self.shifts.get(handover.from_shift)
        now = self._clock.now()
        to_shift = Shift(
            id=self._ids.labeled("shift"),
            label=label,
            operator=operator,
            state=ACTIVE,
            opened_at=now,
        )
        items = tuple(
            replace(item, state=SUSPENDED) if item.state == PENDING else item
            for item in handover.items
        )
        updated = replace(
            handover,
            to_shift=to_shift.id,
            state=ACCEPTING,
            accepted_by=operator,
            accepted_at=now,
            items=items,
        )
        self.handovers.save(updated)
        self.shifts.open(to_shift)
        if from_shift is not None and from_shift.state != HANDING_OVER:
            self.shifts.save(replace(from_shift, state=HANDING_OVER))
        suspended = len([item for item in items if item.state == SUSPENDED])
        self._record("shift_accepted", shift=to_shift, handover=updated, suspended=suspended)
        self._emit(
            "shift_accepted",
            shift_id=to_shift.id,
            handover_id=updated.id,
            operator=operator,
            suspended=suspended,
        )
        updated = self._maybe_finalize(updated)
        return {
            "ok": True,
            "shift": to_shift.as_dict(),
            "handover": updated.as_dict(),
            "violations": [],
        }

    # ---- 交接流程 -------------------------------------------------------

    def begin_handover(self, operator: str) -> dict:
        """交班发起：快照在办事项，班次转入交接中。"""

        shift = self.shifts.current()
        if shift is None or shift.state == CLOSED:
            raise RailError("当前没有当班班次", component="shift")
        if shift.state != ACTIVE:
            raise RailError("当前班次已在交接中", component="shift")
        if operator != shift.operator and not self.roster.has(operator, SUPERVISOR):
            raise RailError("只有当班值班员或班组长能发起交接", component="shift")
        if self.handovers.active() is not None:
            raise RailError("已有进行中的交接", component="shift")
        now = self._clock.now()
        items = tuple(
            HandoverItem(
                key=item.key,
                kind=item.kind,
                ref=item.ref,
                summary=item.summary,
                state=PENDING,
                since=item.since,
                detail=dict(item.detail),
            )
            for item in self._collect(now)
        )
        handover = Handover(
            id=self._ids.labeled("handover"),
            from_shift=shift.id,
            to_shift="",
            state=OFFERED,
            items=items,
            created_by=operator,
            created_at=now,
        )
        self.handovers.save(handover)
        self.shifts.save(replace(shift, state=HANDING_OVER))
        self._record("handover_begin", shift=shift, handover=handover, items=len(items))
        self._emit(
            "handover_begin",
            handover_id=handover.id,
            shift_id=shift.id,
            items=len(items),
        )
        return {"ok": True, "handover": handover.as_dict(), "violations": []}

    def ack(self, item_key: str, operator: str, resume: bool = False) -> dict:
        """接班人逐项确认事项；``resume`` 为真时同时接着办。"""

        handover = self._accepting_handover()
        self._require_incoming(handover, operator)
        item = handover.item(item_key)
        if item is None:
            raise RailError(f"交接单里没有事项 {item_key}", component="shift")
        if item.state == DONE:
            raise RailError(f"事项 {item_key} 已办结", component="shift")
        now = self._clock.now()
        if item.state in (ACCEPTED, RESUMED):
            if resume and item.state == ACCEPTED:
                item = replace(item, state=RESUMED, resumed_at=now)
                handover = self._save_item(handover, item)
                self._record_item("handover_item_resumed", handover, item)
            return {"ok": True, "item": item.as_dict(), "handover": handover.as_dict(), "violations": []}
        item = replace(
            item,
            state=RESUMED if resume else ACCEPTED,
            acked_by=operator,
            acked_at=now,
            resumed_at=now if resume else 0.0,
        )
        handover = self._save_item(handover, item)
        self._record_item("handover_item_acked", handover, item, operator=operator)
        self._emit(
            "handover_item_acked",
            handover_id=handover.id,
            item_key=item.key,
            operator=operator,
        )
        handover = self._maybe_finalize(handover)
        return {"ok": True, "item": item.as_dict(), "handover": handover.as_dict(), "violations": []}

    def resume(self, item_key: str, operator: str) -> dict:
        """把已确认的事项转成接着办。"""

        return self.ack(item_key, operator, resume=True)

    def approve(self, supervisor: str) -> dict:
        """班组长复核：全部事项确认后封存交接单与交班班次。"""

        self.roster.require(supervisor, SUPERVISOR)
        if not self._require_supervisor:
            raise RailError("本线交接不需要班组长复核", component="shift")
        handover = self._accepting_handover()
        if supervisor == handover.accepted_by:
            raise RailError("复核人不能是接班本人", component="shift")
        blocking = handover.blocking()
        if blocking:
            raise RailError(
                f"还有 {len(blocking)} 项未确认，不能复核: "
                + ", ".join(item.key for item in blocking),
                component="shift",
            )
        handover = self._finalize(handover, approved_by=supervisor)
        return {"ok": True, "handover": handover.as_dict(), "violations": []}

    def abort(self, operator: str) -> dict:
        """作废进行中的交接：交班人回到当班，半成品接班一并回退。"""

        handover = self.handovers.active()
        if handover is None:
            raise RailError("没有进行中的交接", component="shift")
        from_shift = self.shifts.get(handover.from_shift)
        if handover.state == OFFERED:
            allowed = (from_shift is not None and operator == from_shift.operator) or (
                self.roster.has(operator, SUPERVISOR)
            )
        else:
            # 接班已开始，收回属于管理动作，只能由班组长执行。
            allowed = self.roster.has(operator, SUPERVISOR)
        if not allowed:
            raise RailError("只有交班值班员或班组长能作废交接", component="shift")
        now = self._clock.now()
        self.handovers.save(replace(handover, state=ABORTED, completed_at=now))
        if handover.to_shift:
            to_shift = self.shifts.get(handover.to_shift)
            if to_shift is not None and to_shift.state != CLOSED:
                self.shifts.save(replace(to_shift, state=CLOSED, closed_at=now))
        if from_shift is not None and from_shift.state != CLOSED:
            self.shifts.save(replace(from_shift, state=ACTIVE))
            self.shifts.set_current(from_shift.id)
        self._record("handover_aborted", shift=from_shift, handover=handover, by=operator)
        self._emit("handover_aborted", handover_id=handover.id, by=operator)
        return {"ok": True, "handover_id": handover.id, "violations": []}

    # ---- 挂起查询（门控／联锁／放行共用） ---------------------------------

    def departure_hold(self) -> dict:
        """发车放行冻结：有未确认事项时不放行。"""

        handover = self.handovers.active()
        blocking = handover.blocking() if handover else []
        return {
            "held": bool(blocking),
            "count": len(blocking),
            "items": [item.key for item in blocking],
            "handover_id": handover.id if handover else "",
        }

    def action_hold(self, kind: str, ref: str) -> dict | None:
        """某类动作是否被交接挂起卡住；卡住时返回事项，否则返回 ``None``。"""

        handover = self.handovers.active()
        if handover is None:
            return None
        item = handover.item(f"{kind}:{ref}")
        if item is not None and item.state in BLOCKING:
            return item.as_dict()
        return None

    def reconcile(self, kind: str, ref: str) -> bool:
        """现场条件消失时把对应事项自动办结（故障清除、批量开完等）。"""

        handover = self.handovers.active()
        if handover is None:
            return False
        item = handover.item(f"{kind}:{ref}")
        if item is None or item.state == DONE:
            return False
        item = replace(item, state=DONE, done_at=self._clock.now())
        handover = self._save_item(handover, item)
        self._record_item("handover_item_done", handover, item)
        return True

    def note_batch(self, station_id: str, deferred: list[str], skipped: dict) -> None:
        """把批量开门的剩余门区落盘，供下次交接扫描。"""

        note_batch_remainder(self._store, station_id, deferred, skipped, self._clock.now())

    # ---- 时序与恢复 -----------------------------------------------------

    def check_timeouts(self, now: float) -> dict | None:
        """交接超时时记审计并发告警；每份交接单只报一次。"""

        handover = self.handovers.active()
        if handover is None or handover.timeout_noted:
            return None
        if handover.state == OFFERED:
            age = now - handover.created_at
        elif handover.state == ACCEPTING:
            age = now - handover.accepted_at
        else:
            return None
        if age <= self._timeout_s:
            return None
        handover = self.handovers.save(replace(handover, timeout_noted=True))
        blocking = len(handover.blocking())
        self._record("handover_timeout", handover=handover, blocking=blocking)
        self._emit(
            "handover_timeout",
            severity="alarm",
            handover_id=handover.id,
            blocking=blocking,
        )
        return {"handover_id": handover.id, "blocking": blocking}

    def recover(self) -> dict:
        """重启后把半成品迁移修复成确定状态，每处修复都落审计。"""

        repairs: list[str] = []
        now = self._clock.now()
        shift = self.shifts.current()
        if shift is None:
            candidates = [item for item in self.shifts.list() if item.state != CLOSED]
            if len(candidates) == 1:
                self.shifts.set_current(candidates[0].id)
                shift = candidates[0]
                repairs.append(f"恢复当班指针 → {shift.id}")
        handover = self.handovers.active()
        if handover is not None and handover.state == ACCEPTING:
            to_shift = self.shifts.get(handover.to_shift) if handover.to_shift else None
            if to_shift is None:
                # 接班写到一半掉电：交接单作废，交班人回到当班。
                self.handovers.save(replace(handover, state=ABORTED, completed_at=now))
                from_shift = self.shifts.get(handover.from_shift)
                if from_shift is not None and from_shift.state != CLOSED:
                    self.shifts.save(replace(from_shift, state=ACTIVE))
                    self.shifts.set_current(from_shift.id)
                repairs.append("接班不完整，交接单作废回退")
            elif shift is None or shift.id != to_shift.id:
                self.shifts.set_current(to_shift.id)
                repairs.append(f"当班指针校正 → {to_shift.id}")
        if self.handovers.active() is None and shift is not None and shift.state == HANDING_OVER:
            self.shifts.save(replace(shift, state=ACTIVE))
            repairs.append("交接单缺失，班次回到当班")
        for message in repairs:
            self._record("shift_recovered", message=message)
            self._emit("shift_recovered", message=message)
        return {"ok": True, "repairs": repairs, "violations": []}

    # ---- 查询 -----------------------------------------------------------

    def status(self) -> dict:
        """班次与交接的状态快照。"""

        current = self.shifts.current()
        handover = self.handovers.active()
        from_shift = self.shifts.get(handover.from_shift) if handover else None
        return {
            "current_shift": current.as_dict() if current else None,
            "handover_from_shift": from_shift.as_dict() if from_shift else None,
            "handover": handover.as_dict() if handover else None,
            "departure_hold": self.departure_hold(),
            "require_supervisor": self._require_supervisor,
            "handover_timeout_s": self._timeout_s,
            "roster": {
                "operators": self.roster.members(OPERATOR),
                "supervisors": self.roster.members(SUPERVISOR),
            },
            "shifts_total": len(self.shifts.list()),
            "handovers_completed": len(
                [item for item in self.handovers.list() if item.state == COMPLETED]
            ),
        }

    def history(self) -> dict:
        """全部班次与交接单（含已封存），供巡检核对。"""

        return {
            "shifts": [shift.as_dict() for shift in self.shifts.list()],
            "handovers": [handover.as_dict() for handover in self.handovers.list()],
        }

    # ---- 内部 -----------------------------------------------------------

    def _accepting_handover(self) -> Handover:
        handover = self.handovers.active()
        if handover is None:
            raise RailError("当前没有进行中的交接", component="shift")
        if handover.state != ACCEPTING:
            raise RailError("交接还未接班，不能确认事项", component="shift")
        return handover

    def _require_incoming(self, handover: Handover, operator: str) -> None:
        current = self.shifts.current()
        if (
            current is None
            or current.id != handover.to_shift
            or current.operator != operator
        ):
            raise RailError("只有接班值班员本人能确认交接事项", component="shift")

    def _save_item(self, handover: Handover, item: HandoverItem) -> Handover:
        items = tuple(existing if existing.key != item.key else item for existing in handover.items)
        return self.handovers.save(replace(handover, items=items))

    def _maybe_finalize(self, handover: Handover) -> Handover:
        """免复核时，末项确认后自动完成交接。"""

        if self._require_supervisor:
            return handover
        if handover.state != ACCEPTING or handover.blocking():
            return handover
        return self._finalize(handover, approved_by="")

    def _finalize(self, handover: Handover, approved_by: str) -> Handover:
        """封存交接单与交班班次。"""

        now = self._clock.now()
        handover = self.handovers.save(
            replace(handover, state=COMPLETED, completed_at=now, approved_by=approved_by)
        )
        from_shift = self.shifts.get(handover.from_shift)
        if from_shift is not None and from_shift.state != CLOSED:
            self.shifts.save(replace(from_shift, state=CLOSED, closed_at=now))
            self._record("shift_closed", shift=from_shift, handover=handover)
        self._record("handover_completed", handover=handover, approved_by=approved_by)
        self._emit(
            "handover_completed", handover_id=handover.id, approved_by=approved_by or "auto"
        )
        return handover

    def _record(
        self,
        kind: str,
        shift: Shift | None = None,
        handover: Handover | None = None,
        **detail: object,
    ) -> None:
        self._audit.record(
            kind,
            "",
            "",
            shift_id=shift.id if shift else None,
            handover_id=handover.id if handover else None,
            **detail,
        )

    def _record_item(
        self, kind: str, handover: Handover, item: HandoverItem, **detail: object
    ) -> None:
        train_id = item.ref if item.kind in TRAIN_KINDS else ""
        station_id = item.ref if item.kind in STATION_KINDS else ""
        self._audit.record(
            kind,
            train_id,
            station_id,
            handover_id=handover.id,
            item_key=item.key,
            item_kind=item.kind,
            **detail,
        )

    def _emit(self, action: str, severity: str = "notice", **payload: object) -> None:
        self._bus.publish(
            Event.of("shift", "shift", action, severity=severity, **payload)
        )
