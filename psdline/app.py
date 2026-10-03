"""平台装配与控制循环。

``Platform`` 把命名空间、站台、列车、站台门、信号、联锁、门控、门区映射、间隙
探测、配额与审计按真实调用链装到一起，并暴露四件事：到发编排（到站、发车、
门区联动、批量开门、清扫）、控制循环、状态快照与手动检修动作。

控制链固定为「信号 → 停稳对位 → 开门 → 间隙探测 → 关门 → 发车 → 审计」，
任何一步错位都会被对应组件拒绝，并把拒绝原因落进审计。
"""

from __future__ import annotations

from .audit import AuditLog, build_timeline
from .clock import Clock, SystemClock
from .config import PlatformConfig, default_config, with_storage_root
from .door import (
    DoorController,
    active_group,
    batch_open,
    close_and_audit,
    depart,
    depart_policy,
    door_report,
    door_state,
    enqueue,
    open_pressure,
    open_doors,
    open_group,
    process_queue,
    queued_total,
)
from .errors import RailError
from .eventbus import Event, EventBus
from .gap import GapRegistry, gap_summary
from .ids import IdFactory
from .interlock import (
    FaultRegistry,
    InterlockArmer,
    interlock_status,
    next_sequence,
    releasable,
    restore_lockouts,
    verdict_of,
)
from .ns import Namespace, NamespaceRegistry
from .platform import MappingRegistry, adjacent, group_keys, physical_groups, resolve
from .psd import LockoutRegistry, PSD, PSDRegistry, sweep_station
from .quota import QuotaRegistry
from .sig import SignalController, aspect_of, depart_checked, refresh_position, signal_summary
from .shift import ShiftService, WI_BATCH, WI_FAULT
from .station import (
    DoorZone,
    Layout,
    LayoutRegistry,
    Schedule,
    ScheduleRegistry,
    Station,
    StationRegistry,
    nearest_zone,
    spread_m,
)
from .store import CommandLog, JsonStore, JsonlJournal, StoreMetrics
from .telemetry import TrendBuffer
from .train import (
    AlignRegistry,
    DoorStateRegistry,
    Dwell,
    DwellRegistry,
    PositionTracker,
    Route,
    RouteRegistry,
    Train,
    TrainRegistry,
    train_state,
)

ARRIVAL = "arrival"
DEPART = "depart"


class Platform:
    """站台门区顺序与联锁控制平台。"""

    def __init__(
        self,
        config: PlatformConfig | None = None,
        clock: Clock | None = None,
        unit: str | None = None,
    ) -> None:
        self.config = (config or default_config()).validate()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.unit = unit or self.config.platform.unit
        self.ids = IdFactory("op")

        store = JsonStore(self.config.storage_root)
        self.store = store
        self.metrics = StoreMetrics(store.root)
        self.audit = AuditLog(
            self.clock,
            self.bus,
            JsonlJournal(store.root, "audit", segment_limit=self.config.audit.journal_segment),
        )
        self.namespace = Namespace(
            self.config.namespace.line, list(self.config.namespace.stations)
        )
        self.namespaces = NamespaceRegistry(self.unit)
        self.trend = TrendBuffer(period_s=self.config.signal.refresh_period_s)

        self.stations = StationRegistry(store)
        self.layouts = LayoutRegistry(store)
        self.schedules = ScheduleRegistry(store)
        self.trains = TrainRegistry(store)
        self.align = AlignRegistry(store, tolerance_m=self.config.station.stop_tolerance_m)
        self.train_doors = DoorStateRegistry(store)
        self.dwells = DwellRegistry(store, max_seconds=self.config.train.dwell_max_s)
        self.positions = PositionTracker(
            store,
            max_position_m=self.config.train.max_position_m,
            stale_after_s=self.config.signal.stale_after_s,
        )
        self.routes = RouteRegistry(store)

        self.gates = PSDRegistry(store)
        self.lockouts = LockoutRegistry(self.gates)
        self.signals = SignalController(store)
        self.faults = FaultRegistry(store)
        self.armers = InterlockArmer(store)
        self.mappings = MappingRegistry(store)
        self.gaps = GapRegistry(store)
        self.quotas = QuotaRegistry(store)
        self.commands = CommandLog(store)
        self.doors = DoorController(
            store, self.align, self.commands, self.gates, self.lockouts
        )
        self.doors.set_batch_limit(self.config.door.batch_limit)

        # 班次交接值班台：装配晚于门控/联锁，构造时自动从哈希链台账重放恢复。
        self.shift_service = ShiftService(
            store,
            self.clock,
            self.audit,
            handover_deadline_s=self.config.shift.handover_deadline_s,
            journal_segment=self.config.shift.journal_segment,
        )
        self._wire_shift_hooks()

        self._cycles = 0
        self._operations = 0
        self._event_counts: dict[str, int] = {}
        self.bus.subscribe("*", self._on_event)
        self._register_namespaces()
        self._seed(store)
        self._restore()

    def _on_event(self, event: Event) -> None:
        """统计总线事件，供状态接口判断动作有没有真的流过组件。"""

        self._event_counts[event.topic] = self._event_counts.get(event.topic, 0) + 1

    # ---- 装配 ---------------------------------------------------------

    def _register_namespaces(self) -> None:
        """登记现场设备点位（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("line", "signal", "signal-controller"),
            ("line", "sequence", "sequence-source"),
            ("station", "platform", "platform-screen"),
            ("station", "gap", "gap-detector"),
            ("station", "door", "platform-door"),
            ("station", "interlock", "door-interlock"),
            ("station", "quota", "quota-counter"),
            ("station", "audit", "audit-sink"),
        ):
            self.namespaces.register(area, device, kind)

    def _seed(self, store: JsonStore) -> None:
        """按线路命名空间布置站台、布局、映射与站台门。"""

        seed_doc = "platform/seed"
        if store.exists(seed_doc):
            return
        groups_per_station = self.config.station.groups_per_station
        doors_per_group = self.config.station.doors_per_group
        span = self.config.station.group_span_m
        for station_index, station_id in enumerate(self.namespace.station_ids(), start=1):
            groups = [f"G{index:02d}" for index in range(1, groups_per_station + 1)]
            self.stations.upsert(
                Station(
                    id=station_id,
                    name=f"站台 {station_index}",
                    line=self.namespace.line_id(),
                    door_groups=groups,
                )
            )
            base = (station_index - 1) * span
            zones = [
                DoorZone(id=group, distance_m=int(base + (index - 1) * span / groups_per_station))
                for index, group in enumerate(groups, start=1)
            ]
            self.layouts.save(Layout(station_id=station_id, zones=zones))
            self.mappings.renumber(station_id, {group: group for group in groups})
            self.schedules.save(
                Schedule(station_id=station_id, headways=[120, 150, 180, 240])
            )
            for group in groups:
                for door_index in range(1, doors_per_group + 1):
                    self.gates.upsert(
                        PSD(
                            id=f"{station_id}-{group}-{door_index:02d}",
                            station=station_id,
                            group=group,
                        )
                    )
        for name, limit in (
            (ARRIVAL, self.config.quota.arrival_limit),
            (DEPART, self.config.quota.depart_limit),
        ):
            self.quotas.set_limit(name, limit)
        store.write(
            seed_doc,
            {"line": self.namespace.line_id(), "stations": self.namespace.station_ids()},
        )

    def _restore(self) -> None:
        """从落盘状态恢复操作计数与锁定状态。"""

        state = self.store.read("platform/state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))
        restore_lockouts(self.faults, self.gates, self.lockouts, not self.gaps.blocked_ids())

    def _persist(self) -> None:
        """落盘平台计数，供重启后对照。"""

        self.store.write(
            "platform/state",
            {
                "operations": self._operations,
                "cycles": self._cycles,
                "unit": self.unit,
                "line": self.namespace.line_id(),
            },
        )

    # ---- 班次值班台接线 -------------------------------------------------

    def _wire_shift_hooks(self) -> None:
        """把现场事实回调注入班次服务，发现/续办/收口都重新读当前门态。"""

        self.shift_service.hooks.update(
            {
                "active_fault_ids": self.faults.active_ids,
                "discover_batches": self._discover_open_batches,
                "continue_batch": self._continue_batch_item,
                "secure_close_batch": self._secure_close_batch_item,
            }
        )

    def _guard(self, actor: dict | None) -> None:
        """安全动作闸门：冻结窗口 / 跨班旧代际 / 无效会话在此统一拒绝。"""

        self.shift_service.require_allowed(actor)

    def _discover_open_batches(self) -> dict[str, dict]:
        """现场扫描：有开启门且不是整站全开的站台视为存在半截批量开门。"""

        found: dict[str, dict] = {}
        for station_id in self.namespace.station_ids():
            gates = self.gates.by_station(station_id)
            open_groups = sorted({gate.group for gate in gates if gate.open})
            if not open_groups:
                continue
            all_groups = [zone.id for zone in (self.layouts.get(station_id).zones if self.layouts.get(station_id) else [])]
            deferred = [group for group in all_groups if group not in open_groups]
            if deferred:
                found[station_id] = {
                    "order": all_groups,
                    "opened": open_groups,
                    "deferred": deferred,
                    "skipped": {},
                }
        return found

    def _continue_batch_item(self, item) -> dict:
        """恢复批量开门：按当前布局逐门区复核联锁，把剩余门区全部开完。

        恢复已经过班组长会签，不再受单批上限约束（上限只约束班内例行批量）；
        逐门区重新检查锁定状态，任一被锁就立即停手并保持事项挂起，不盲信
        交接时的门态快照。
        """

        station_id = item.station_id
        layout = self.layouts.get(station_id)
        if layout is None:
            return {"ok": False, "violations": ["layout_missing"]}
        gates = self.gates.by_station(station_id)
        opened_now: list[str] = []
        blocked: dict[str, str] = {}
        for zone in layout.zones:
            group_gates = [gate for gate in gates if gate.group == zone.id]
            if not group_gates:
                blocked[zone.id] = "gate_missing"
                continue
            if all(gate.open for gate in group_gates):
                continue
            locked = [gate.id for gate in group_gates if gate.locked]
            if locked:
                blocked[zone.id] = f"locked:{','.join(locked)}"
                continue
            for gate in group_gates:
                self.gates.set_open(gate.id, True)
            opened_now.append(zone.id)
        if blocked:
            return {
                "ok": False,
                "violations": ["batch_not_complete"],
                "blocked": blocked,
                "opened_now": opened_now,
            }
        self.audit.record(
            "batch_open_resumed", "", station_id, item_id=item.id, opened=len(opened_now)
        )
        return {"ok": True, "opened_now": opened_now}

    def _secure_close_batch_item(self, item) -> None:
        """终止批量开门时的安全收口：把该站已开的门全部关回并清扫。"""

        for gate in self.gates.by_station(item.station_id):
            if gate.open:
                self.gates.set_open(gate.id, False)
        self.audit.record("batch_open_secured", "", item.station_id, item_id=item.id)

    def _record_operation(self, op_id: str, operation: str, report: dict) -> dict:
        """把编排步骤逐条写事件，并记一条汇总审计。"""

        train_id = str(report.get("train_id", ""))
        station_id = str(report.get("station_id", ""))
        reserved = {
            "train_id",
            "station_id",
            "op_id",
            "severity",
            "message",
            "component",
            "action",
            "detail",
        }
        for step in report.get("steps", []):
            detail = {
                key: value
                for key, value in dict(step.get("detail", {})).items()
                if not isinstance(value, (dict, list)) and key not in reserved
            }
            self.bus.publish(
                Event.of(
                    "door",
                    str(step.get("component", "door")),
                    str(step.get("action", "step")),
                    op_id=op_id,
                    train_id=train_id,
                    severity="notice",
                    **detail,
                )
            )
        for violation in report.get("violations", []):
            self.bus.publish(
                Event.of(
                    "audit",
                    "interlock",
                    "interlock_rejected",
                    op_id=op_id,
                    train_id=train_id,
                    station_id=station_id,
                    severity="alarm",
                    message=f"{operation} 被拒绝：{violation}",
                    violation=violation,
                )
            )
        entry = self.audit.record(
            f"{operation}_complete",
            train_id,
            station_id,
            op_id=op_id,
            ok=bool(report.get("ok")),
            violations=list(report.get("violations", [])),
        )
        return {
            "op_id": op_id,
            "operation": operation,
            "ok": bool(report.get("ok")),
            "violations": list(report.get("violations", [])),
            "audit_id": entry.id,
        }

    # ---- 生命周期 -----------------------------------------------------

    def start(self) -> dict:
        """启动平台：核对命名空间、站台、布局与映射是否一致。"""

        missing: dict[str, list[str]] = {}
        for station_id in self.namespace.station_ids():
            if self.stations.get(station_id) is None:
                missing.setdefault("station", []).append(station_id)
            if self.layouts.get(station_id) is None:
                missing.setdefault("layout", []).append(station_id)
            if self.mappings.current(station_id) is None:
                missing.setdefault("mapping", []).append(station_id)
        return {
            "unit": self.unit,
            "line": self.namespace.label(),
            "missing": missing,
            "stations": len(self.namespace.station_ids()),
            "gates": len(self.gates.list()),
            "operations": self._operations,
            "ready": not missing,
        }

    def stop(self) -> dict:
        """停运平台：整站清扫后收起全部灯位。"""

        op_id = self.ids.labeled("stop")
        steps: list[dict] = []
        closed = 0
        for station_id in self.namespace.station_ids():
            report = self.sweep(station_id)
            closed += report["closed"]
            steps.append(
                {
                    "component": "psd",
                    "action": "sweep",
                    "detail": {"station_id": station_id, "closed": report["closed"]},
                }
            )
        for train in self.trains.list():
            self.signals.clear(train.id, ARRIVAL)
            self.signals.clear(train.id, DEPART)
        report = {"ok": True, "train_id": "", "closed": closed, "steps": steps, "violations": []}
        summary = self._record_operation(op_id, "stop", report)
        self._operations += 1
        self._persist()
        return {**report, **summary}

    # ---- 到发编排 -----------------------------------------------------

    def arrive(self, train_id: str, station_id: str, position_m: int, actor: dict | None = None) -> dict:
        """到站到开门：信号到站 → 停稳对位 → 对位落盘 → 开门。"""

        self._guard(actor)
        op_id = self.ids.labeled("arrive")
        steps: list[dict] = []
        violations: list[str] = []

        def reject(reason: str) -> dict:
            violations.append(reason)
            self.bus.publish(
                Event.of(
                    "audit",
                    "interlock",
                    "arrive_rejected",
                    op_id=op_id,
                    train_id=train_id,
                    station_id=station_id,
                    severity="alarm",
                    message=reason,
                )
            )
            return {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": violations,
            }

        if not self.quotas.consume(ARRIVAL):
            return reject("到站配额已用完")
        try:
            self.namespace.require(station_id)
        except RailError as error:
            return reject(error.message)
        if self.stations.get(station_id) is None:
            return reject(f"站台未登记: {station_id}")

        self.signals.raise_signal(train_id, ARRIVAL, self.clock.now())
        steps.append(
            {"component": "sig", "action": "arrival_signal", "detail": {"train_id": train_id}}
        )
        if self.trains.get(train_id) is None:
            self.trains.upsert(Train(id=train_id, line=self.namespace.line_id()))
        self.trains.assign_station(train_id, station_id)

        stop_position = self._stop_position(station_id)
        try:
            alignment = self.align.align(train_id, position_m, stop_position, self.clock.now())
        except RailError as error:
            return reject(error.message)
        steps.append(
            {
                "component": "train",
                "action": "align",
                "detail": {"train_id": train_id, "position_m": alignment.position_m},
            }
        )
        try:
            self.doors_open(train_id, station_id)
        except RailError as error:
            return reject(error.message)
        entry = self.audit.record(ARRIVAL, train_id, station_id, position_m=position_m)
        report = {
            "ok": True,
            "train_id": train_id,
            "station_id": station_id,
            "position_m": alignment.position_m,
            "steps": steps,
            "violations": [],
        }
        summary = self._record_operation(op_id, "arrive", report)
        self._operations += 1
        self._persist()
        return {**report, **summary, "arrival_audit": entry.id}

    def doors_open(self, train_id: str, station_id: str) -> dict:
        """开门并确认车门：对位必须先落盘。"""

        report = open_doors(self.doors, train_id, self.clock.now())
        group = self._primary_group(station_id)
        if group:
            open_group(self.doors, self.mappings, station_id, group)
        self.train_doors.confirm_open(train_id, self.clock.now())
        self.bus.publish(
            Event.of(
                "door",
                "door",
                "opened",
                train_id=train_id,
                station_id=station_id,
                severity="notice",
                group=group,
            )
        )
        return {**report, "group": group}

    def depart(self, train_id: str, station_id: str, actor: dict | None = None) -> dict:
        """关门到发车：关门指令 → 间隙探测 → 关门落盘 → 发车信号。"""

        self._guard(actor)
        op_id = self.ids.labeled("depart")
        steps: list[dict] = []
        if not self.quotas.consume(DEPART):
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": ["发车配额已用完"],
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}

        gate = self._primary_gate(station_id)
        if gate is not None and not self.lockouts.is_locked(gate.id):
            self.lockouts.set(gate.id, True)
            steps.append(
                {"component": "interlock", "action": "lock", "detail": {"psd_id": gate.id}}
            )
        try:
            closed = close_and_audit(
                self.doors, self.audit, train_id, station_id, DEPART, self.clock.now()
            )
        except RailError as error:
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": [error.message],
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}
        steps.append(
            {"component": "door", "action": "close_durable", "detail": {"train_id": train_id}}
        )
        gap_state = self.gaps.state(train_id)
        steps.append(
            {
                "component": "gap",
                "action": "check",
                "detail": {"train_id": train_id, "state": gap_state},
            }
        )
        verdict = depart_checked(self.signals, self.store, self.gaps, train_id, self.clock.now())
        if not verdict["ok"]:
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": list(verdict["violations"]),
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}
        shift_release = self.shift_service.check_release(train_id, station_id)
        if not shift_release["allowed"]:
            violations = [
                f"班次放行被扣: {row['item_id']}({row['state']})"
                for row in shift_release["blockers"]
            ]
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": violations,
                "shift_blockers": shift_release["blockers"],
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}
        steps.append(
            {"component": "sig", "action": "depart_signal", "detail": {"train_id": train_id}}
        )
        departure = depart(self.doors, train_id, self.clock.now())
        if gate is not None:
            self.lockouts.clear(gate.id)
        report = {
            "ok": True,
            "train_id": train_id,
            "station_id": station_id,
            "close_durable": bool(closed["close_durable"]),
            "aspect": verdict["aspect"],
            "cleared_commands": departure["cleared_commands"],
            "steps": steps,
            "violations": [],
        }
        summary = self._record_operation(op_id, "depart", report)
        self._operations += 1
        self._persist()
        return {**report, **summary}

    def check_gap(self, train_id: str, clear: bool = True, actor: dict | None = None) -> dict:
        """确认间隙无异常，或登记一次可疑探测结果。"""

        self._guard(actor)
        if clear:
            result = self.gaps.check(train_id, self.clock.now())
        else:
            result = self.gaps.probe(train_id, False, self.clock.now())
        self.audit.record("gap_probe", train_id, "", clear=clear)
        return {"ok": True, "train_id": train_id, "clear": result.clear, "violations": []}

    def renumber(self, station_id: str, groups: dict[str, str], actor: dict | None = None) -> dict:
        """按最新布局重排门区映射，并按新映射联动一次。"""

        self._guard(actor)
        mapping = self.mappings.renumber(station_id, groups)
        first_key = sorted(groups)[0]
        linked = open_group(self.doors, self.mappings, station_id, first_key)
        self.audit.record("mapping_renumber", "", station_id, revision=mapping.revision)
        return {
            "ok": True,
            "station_id": station_id,
            "revision": mapping.revision,
            "mapping": mapping.as_dict(),
            "active_group": linked["group"],
            "violations": [],
        }

    def enqueue(self, train_id: str, command_id: str, kind: str, station_id: str = "", actor: dict | None = None) -> dict:
        """把一条门控指令排进队列。"""

        self._guard(actor)
        command = enqueue(self.doors, train_id, command_id, kind, station_id)
        self.audit.record(
            "door_enqueue", train_id, station_id, command_id=command.id, command_kind=kind
        )
        return {"ok": True, "train_id": train_id, "command": command.as_dict(), "violations": []}

    def process_queue(self, train_id: str) -> dict:
        """返回列车队列里待执行的门控指令，并带上发车自检结果。"""

        policy = depart_policy(self.doors, train_id)
        ids = process_queue(self.doors, train_id)
        self.audit.record("door_process", train_id, "", queued=len(ids))
        return {
            "ok": True,
            "train_id": train_id,
            "ids": ids,
            "ready": policy["allowed"],
            "violations": list(policy["failures"]),
        }

    def clear_fault(self, psd_id: str, actor: dict | None = None) -> dict:
        """清除门区故障并按条件解锁。"""

        self._guard(actor)
        gate = self.gates.require(psd_id)
        allowed, reason = releasable(self.gates, psd_id, True)
        if not allowed:
            return {"ok": False, "psd_id": psd_id, "locked": True, "violations": [reason]}
        self.faults.clear_fault(self.lockouts, psd_id, self.clock.now())
        self.shift_service.note_fault_cleared(psd_id)
        self.audit.record("interlock_clear", "", gate.station, psd_id=psd_id)
        return {"ok": True, "psd_id": psd_id, "locked": False, "violations": []}

    def set_fault(self, psd_id: str, actor: dict | None = None) -> dict:
        """置位门区故障并锁定。"""

        self._guard(actor)
        gate = self.gates.require(psd_id)
        self.faults.set_fault(self.lockouts, psd_id, self.clock.now())
        self.shift_service.note_fault_set(psd_id, gate.station)
        self.audit.record("interlock_fault", "", gate.station, psd_id=psd_id)
        return {"ok": True, "psd_id": psd_id, "locked": True, "violations": []}

    def arm(self, train_id: str, armed: bool, actor: dict | None = None) -> dict:
        """启用或退出一列车的联锁。"""

        self._guard(actor)
        state = self.armers.arm(train_id, armed)
        self.audit.record("interlock_arm", train_id, "", armed=state)
        return {"ok": True, "train_id": train_id, "armed": state, "violations": []}

    def refresh_position(self, train_id: str, position_m: int, actor: dict | None = None) -> dict:
        """按信号刷新列车位置。"""

        self._guard(actor)
        outcome = refresh_position(self.positions, train_id, position_m, self.clock.now())
        if outcome["ok"]:
            self.trend.append("train_position_m", self.clock.now(), position_m)
        else:
            self.audit.record("position_rejected", train_id, "", reason=outcome["reason"])
        return {**outcome, "violations": [] if outcome["ok"] else [outcome["reason"]]}

    def sequence_verdict(self, previous: int, step: int) -> dict:
        """推进门控事件序号并给出回绕判定。"""

        current = next_sequence(previous, step)
        verdict = verdict_of(previous, current)
        self.audit.record(
            "sequence_verdict",
            "",
            "",
            previous=verdict.previous,
            current=verdict.current,
            advanced=verdict.advanced,
        )
        return {"ok": verdict.advanced, "verdict": verdict.as_dict(), "violations": [] if verdict.advanced else [verdict.reason]}

    def batch_open(self, station_id: str, actor: dict | None = None) -> dict:
        """按布局里程顺序批量开门。"""

        self._guard(actor)
        layout = self.layouts.get(station_id)
        zones = list(layout.zones) if layout else []
        outcome = batch_open(self.doors, station_id, zones)
        # 半截批量（超上限延后 / 门区锁定跳过）自动挂成在办事项，交接逐项过。
        item = self.shift_service.note_batch_open(station_id, outcome)
        nearest = nearest_zone(zones)
        self.audit.record(
            "batch_open",
            "",
            station_id,
            opened=len(outcome["opened"]),
            skipped=len(outcome["skipped"]),
        )
        return {
            "ok": not outcome["skipped"],
            "station_id": station_id,
            "order": outcome["order"],
            "opened": outcome["opened"],
            "deferred": outcome["deferred"],
            "skipped": outcome["skipped"],
            "nearest": nearest.id if nearest else "",
            "work_item": item["id"] if item else "",
            "violations": [],
        }

    def sweep(self, station_id: str, actor: dict | None = None) -> dict:
        """整站清扫。"""

        self._guard(actor)
        return sweep_station(self.gates, station_id)

    def register_dwell(self, train_id: str, seconds: float, station_id: str = "") -> dict:
        """登记停站时长。"""

        dwell = self.dwells.set(Dwell(train_id=train_id, seconds=seconds, station_id=station_id))
        return {"ok": True, "dwell": dwell.as_dict(), "violations": []}

    def assign_route(self, train_id: str, origin: str, destination: str, distance_m: int) -> dict:
        """登记运行交路。"""

        route = self.routes.assign(
            Route(train_id=train_id, origin=origin, destination=destination, distance_m=distance_m)
        )
        return {"ok": True, "route": route.__dict__, "violations": []}

    # ---- 控制循环 -----------------------------------------------------

    def cycle(self, dt: float = 1.0) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        if dt <= 0:
            return {"ok": False, "violations": ["步长必须为正"], "cycle": self._cycles}
        self._cycles += 1
        shift_timeouts = self.shift_service.tick()
        moment = self.clock.now()
        stale = [
            train.id for train in self.trains.list() if self.positions.stale(train.id, moment)
        ]
        interlock = interlock_status(self.faults, self.gates, self.config.interlock.max_locked_ratio)
        holding = [
            train.id
            for train in self.trains.list()
            if not self.armers.armed(train.id) and depart_policy(self.doors, train.id)["allowed"]
        ]
        self.trend.append("locked_count", moment, interlock["locked_count"])
        self.trend.append("queued_commands", moment, queued_total(self.doors))
        self.trend.append("audit_entries", moment, len(self.audit.list()))
        self._persist()
        return {
            "ok": True,
            "cycle": self._cycles,
            "dt": dt,
            "shift_timeouts": shift_timeouts["timeouts"],
            "stale_positions": stale,
            "locked_count": interlock["locked_count"],
            "can_release_depart": interlock["can_release_depart"],
            "ready_trains": holding,
            "gaps": gap_summary(self.gaps, [train.id for train in self.trains.list()]),
            "violations": [],
        }

    # ---- 查询 ---------------------------------------------------------

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        trains = self.trains.list()
        ids = [train.id for train in trains]
        return {
            "platform": self.config.name,
            "unit": self.unit,
            "line": self.namespace.label(),
            "operations": self._operations,
            "cycles": self._cycles,
            "utc": self.clock.utc_iso(),
            "namespace": self.namespace.as_dict(),
            "namespaces": self.namespaces.describe(),
            "open_gates": self.gates.count_open(),
            "lockout_changes": self.lockouts.changes,
            "signals_raised": self.signals.raises,
            "signals_cleared": self.signals.clears,
            "interlock_counters": {
                "faults_set": self.faults.sets,
                "faults_cleared": self.faults.clears,
                "armed": self.armers.arms,
                "disarmed": self.armers.disarms,
                "armed_trains": self.armers.armed_trains(),
            },
            "gap_engine": {"checks": self.gaps.checks, "probes": self.gaps.probes},
            "event_counts": dict(sorted(self._event_counts.items())),
            "console_hits": len(self.bus.history("console")),
            "stations": [station.as_dict() for station in self.stations.list()],
            "trains": {train.id: train_state(train.id, self.align, self.train_doors, self.positions) for train in trains},
            "doors": [
                {**gate.as_dict(), "open": gate.open, "locked": gate.locked}
                for gate in self.gates.list()
            ],
            "door_report": door_report(self.doors, self.namespace.station_ids()),
            "signals": signal_summary(self.signals, ids),
            "interlock": interlock_status(
                self.faults, self.gates, self.config.interlock.max_locked_ratio
            ),
            "gaps": gap_summary(self.gaps, ids),
            "quota": {
                ARRIVAL: self.quotas.status(ARRIVAL),
                DEPART: self.quotas.status(DEPART),
            },
            "shift": self.shift_service.desk(),
            "store": self.metrics.summary(),
            "audit": self.audit.summary(),
            "trend": self.trend.snapshot(limit=4),
            "ids_issued": self.ids.issued(),
        }

    def snapshot(self) -> dict:
        """返回只读的整体快照（状态 + 命名空间 + 门区映射 + 审计时间线）。"""

        return {
            "status": self.status(),
            "groups": {
                station_id: group_keys(self.mappings, station_id)
                for station_id in self.namespace.station_ids()
            },
            "physical_groups": {
                station_id: physical_groups(self.mappings, station_id)
                for station_id in self.namespace.station_ids()
            },
            "mapping_revisions": {
                station_id: {
                    "latest": self.mappings.history(station_id).latest(),
                    "log": self.mappings.history(station_id).as_dict()["revisions"],
                }
                for station_id in self.namespace.station_ids()
            },
            "adjacent": self._adjacent_sample(),
            "timeline": build_timeline(self.audit.list(), limit=20),
        }

    def train_view(self, train_id: str) -> dict:
        """返回一列车的综合视图。"""

        return {
            "train": self.trains.get(train_id).as_dict() if self.trains.get(train_id) else None,
            "state": train_state(train_id, self.align, self.train_doors, self.positions),
            "doors": door_state(self.doors, train_id),
            "aspect": aspect_of(self.signals, train_id),
            "gap": self.gaps.state(train_id),
        }

    def station_view(self, station_id: str) -> dict:
        """返回一个站台的综合视图。"""

        layout = self.layouts.get(station_id)
        mapping = self.mappings.current(station_id)
        zones = list(layout.zones) if layout else []
        schedule = self.schedules.get(station_id)
        return {
            "station": self.stations.get(station_id).as_dict() if self.stations.get(station_id) else None,
            "group_count": self.stations.group_count(station_id),
            "zones": [zone.as_dict() for zone in zones],
            "order": [zone.id for zone in zones],
            "zone_distances": {
                zone.id: self.layouts.zone_distance(station_id, zone.id) for zone in zones
            },
            "span_m": spread_m(zones),
            "minimum_headway_s": self.schedules.minimum_headway(station_id) if schedule else 0,
            "revision": mapping.revision if mapping else 0,
            "groups": group_keys(self.mappings, station_id),
            "active_group": active_group(self.doors, station_id),
            "pressure": open_pressure(self.doors, station_id),
        }

    def resolve_group(self, station_id: str, group_key: str) -> dict:
        """解析一个逻辑门区。"""

        group = resolve(self.mappings, station_id, group_key)
        return {
            "station_id": station_id,
            "group_key": group_key,
            "group": group,
            "neighbours": adjacent(self.mappings, station_id, group_key),
        }

    def note_error(self, error: RailError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        entry = self.audit.record(
            "error",
            "",
            "",
            component=error.component,
            message=error.message,
            error_type=type(error).__name__,
        )
        return entry.as_dict()

    def reset_state(self) -> bool:
        """清掉落盘的平台计数（交付前重新标定用）。"""

        if not self.store.exists("platform/state"):
            return False
        return self.store.remove("platform/state")

    # ---- 内部 ---------------------------------------------------------

    def _stop_position(self, station_id: str) -> int:
        """返回站台的停稳位置（取最近门区里程）。"""

        layout = self.layouts.get(station_id)
        zones = list(layout.zones) if layout else []
        nearest = nearest_zone(zones)
        return nearest.distance_m if nearest else 0

    def _primary_group(self, station_id: str) -> str:
        """返回站台的首个门区。"""

        keys = group_keys(self.mappings, station_id)
        return keys[0] if keys else ""

    def _primary_gate(self, station_id: str) -> PSD | None:
        """返回站台的首樘站台门。"""

        gates = self.gates.by_station(station_id)
        return gates[0] if gates else None

    def _adjacent_sample(self) -> dict:
        """给快照提供一个相邻关系样例。"""

        station_ids = self.namespace.station_ids()
        if not station_ids:
            return {}
        station_id = station_ids[0]
        keys = group_keys(self.mappings, station_id)
        if not keys:
            return {}
        return {keys[0]: adjacent(self.mappings, station_id, keys[0])}


def build_platform(
    config: PlatformConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str | None = None,
) -> Platform:
    """构造一个平台；``storage_root`` 可覆盖配置里的存储目录。"""

    base = config or default_config()
    if storage_root is not None:
        base = with_storage_root(base, storage_root)
    return Platform(base, clock=clock, unit=unit)
