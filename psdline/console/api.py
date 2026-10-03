"""控制台接口处理器。

每个接口都是对平台公开方法的一次调用；动作类接口把请求体里的参数透传给平台，
缺少必填字段或取值非法时返回 ``400``，联锁异常统一转成 ``409`` 并在响应里带上
组件名与原因。页面不走这里，见 :mod:`psdline.console.pages`。
"""

from __future__ import annotations

from ..app import Platform
from ..audit import build_timeline
from ..errors import RailError
from .router import Router


def _require(body: dict, key: str) -> str:
    """取出必填的字符串字段。"""

    value = body.get(key)
    if value is None or str(value) == "":
        raise RailError(f"缺少必填字段 {key!r}", component="console")
    return str(value)


def _as_int(body: dict, key: str, default: int) -> int:
    """取出整数字段。"""

    try:
        return int(body.get(key, default))
    except (TypeError, ValueError) as error:
        raise RailError(f"参数 {key!r} 必须是整数", component="console") from error


def _as_float(body: dict, key: str, default: float) -> float:
    """取出浮点字段。"""

    try:
        return float(body.get(key, default))
    except (TypeError, ValueError) as error:
        raise RailError(f"参数 {key!r} 必须是数值", component="console") from error


def _actor(body: dict) -> dict:
    """从请求里取值班身份：会话令牌优先，其次在册身份自证（开班前台帐动作）。"""

    return {
        "token": str(body.get("token", "")),
        "operator_id": str(body.get("operator_id", "")),
    }


class Api:
    """把 HTTP 请求翻译成平台调用。"""

    def __init__(self, platform: Platform) -> None:
        self._platform = platform
        self._router = Router()
        self._register()

    @property
    def platform(self) -> Platform:
        return self._platform

    def _register(self) -> None:
        add = self._router.add
        add("GET", "/api/status", lambda params, body: self._platform.status())
        add("GET", "/api/snapshot", lambda params, body: self._platform.snapshot())
        add("GET", "/api/namespace", lambda params, body: self._platform.namespace.as_dict())
        add("GET", "/api/namespaces", lambda params, body: self._platform.namespaces.describe())
        add("GET", "/api/stations", lambda params, body: {"stations": self._platform.status()["stations"]})
        add("GET", "/api/stations/{stationID}", self._station)
        add("GET", "/api/trains", lambda params, body: {"trains": self._platform.status()["trains"]})
        add("GET", "/api/trains/{trainID}", self._train)
        add("GET", "/api/doors", lambda params, body: {"doors": self._platform.status()["doors"]})
        add("GET", "/api/doors/{psdID}", self._gate)
        add("GET", "/api/interlock", lambda params, body: self._platform.status()["interlock"])
        add("GET", "/api/gaps", lambda params, body: self._platform.status()["gaps"])
        add("GET", "/api/quota", lambda params, body: self._platform.status()["quota"])
        add("GET", "/api/shift/desk", self._shift_desk)
        add("GET", "/api/shift/handovers", self._shift_handovers)
        add("GET", "/api/shift/items", self._shift_items)
        add("GET", "/api/shift/ledger", self._shift_ledger)
        add("GET", "/api/shift/handovers/{handoverID}", self._shift_handover_view)
        add("POST", "/api/shift/operator", self._shift_operator)
        add("POST", "/api/shift/plan", self._shift_plan)
        add("POST", "/api/shift/roster", self._shift_roster)
        add("POST", "/api/shift/checkin", self._shift_checkin)
        add("POST", "/api/shift/signout", self._shift_signout)
        add("POST", "/api/shift/open", self._shift_open)
        add("POST", "/api/shift/begin-handover", self._shift_begin)
        add("POST", "/api/shift/acknowledge", self._shift_ack)
        add("POST", "/api/shift/suspend", self._shift_suspend)
        add("POST", "/api/shift/countersign", self._shift_countersign)
        add("POST", "/api/shift/complete-handover", self._shift_complete)
        add("POST", "/api/shift/force-close", self._shift_force)
        add("POST", "/api/shift/adopt-orphans", self._shift_adopt)
        add("POST", "/api/shift/items", self._shift_item_register)
        add("POST", "/api/shift/items/{itemID}/resume", self._shift_item_resume)
        add("POST", "/api/shift/items/{itemID}/complete", self._shift_item_complete)
        add("POST", "/api/shift/items/{itemID}/abort", self._shift_item_abort)
        add("GET", "/api/audit", self._audit)
        add("GET", "/api/timeline", self._timeline)
        add("GET", "/api/trend", self._trend)
        add("GET", "/api/store", self._store)
        add("GET", "/api/mapping/{stationID}", self._mapping)
        add("GET", "/api/resolve", self._resolve)
        add("GET", "/api/readiness", self._readiness)
        add("POST", "/api/arrive", self._arrive)
        add("POST", "/api/depart", self._depart)
        add("POST", "/api/gap", self._gap)
        add("POST", "/api/enqueue", self._enqueue)
        add("POST", "/api/process", self._process)
        add("POST", "/api/renumber", self._renumber)
        add("POST", "/api/batch-open", self._batch_open)
        add("POST", "/api/fault", self._fault)
        add("POST", "/api/clear-fault", self._clear_fault)
        add("POST", "/api/arm", self._arm)
        add("POST", "/api/position", self._position)
        add("POST", "/api/sequence", self._sequence)
        add("POST", "/api/cycle", self._cycle)
        add("POST", "/api/sweep", self._sweep)
        add("POST", "/api/dwell", self._dwell)
        add("POST", "/api/route", self._route)
        add("POST", "/api/reset-state", self._reset_state)

    def routes(self) -> list[dict]:
        """返回接口清单，供自检与文档核对。"""

        return [route.as_dict() for route in self._router.routes()]

    def handle(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        """处理一次请求，返回状态码与响应体。"""

        matched = self._router.match(method, path)
        if matched is None:
            return 404, {"error": "not_found", "path": path}
        handler, params = matched
        merged = {**query, **params}
        try:
            payload = handler(merged, body)
        except RailError as error:
            self._platform.note_error(error)
            status = 409 if error.component != "console" else 400
            return status, {"error": type(error).__name__, **error.as_record()}
        except (TypeError, ValueError) as error:
            return 400, {"error": "bad_request", "message": str(error)}
        return 200, payload

    # ---- 查询 ---------------------------------------------------------

    def _station(self, params: dict, body: dict) -> dict:
        return self._platform.station_view(str(params.get("stationID", "")))

    def _train(self, params: dict, body: dict) -> dict:
        train_id = str(params.get("trainID", ""))
        view = self._platform.train_view(train_id)
        if view["train"] is None:
            raise RailError(f"列车不存在: {train_id}", component="console")
        return view

    def _gate(self, params: dict, body: dict) -> dict:
        psd_id = str(params.get("psdID", ""))
        gate = self._platform.gates.get(psd_id)
        if gate is None:
            raise RailError(f"站台门不存在: {psd_id}", component="console")
        return {"gate": gate.as_dict(), "pressure": self._platform.doors.batch_limit()}

    def _audit(self, params: dict, body: dict) -> dict:
        entries = self._platform.audit.list()
        return {
            "entries": [entry.as_dict() for entry in entries[-int(params.get("limit", 50)) :]],
            "summary": self._platform.audit.summary(),
        }

    def _timeline(self, params: dict, body: dict) -> dict:
        train_id = str(params.get("train_id", ""))
        return {"timeline": build_timeline(self._platform.audit.list(), train_id)}

    def _trend(self, params: dict, body: dict) -> dict:
        signal = str(params.get("signal", "locked_count"))
        samples = self._platform.trend.series(signal, int(params.get("limit", 60)))
        filled = self._platform.trend.fill_gaps(signal, samples)
        return {
            "signal": signal,
            "period_s": self._platform.trend.period_s,
            "signals": self._platform.trend.signals(),
            "samples": [sample.as_dict() for sample in samples],
            "filled": [sample.as_dict() for sample in filled],
        }

    def _store(self, params: dict, body: dict) -> dict:
        return {
            "summary": self._platform.metrics.summary(),
            "documents": self._platform.store.list_ids(),
        }

    def _mapping(self, params: dict, body: dict) -> dict:
        station_id = str(params.get("stationID", ""))
        mapping = self._platform.mappings.current(station_id)
        if mapping is None:
            raise RailError(f"站台 {station_id} 没有门区映射", component="console")
        return mapping.as_dict()

    def _resolve(self, params: dict, body: dict) -> dict:
        return self._platform.resolve_group(
            str(params.get("station_id", "")), str(params.get("group_key", ""))
        )

    def _readiness(self, params: dict, body: dict) -> dict:
        checks = {
            "stations": bool(self._platform.stations.list()),
            "gates": bool(self._platform.gates.list()),
            "quota_arrival": self._platform.quotas.status("arrival")["configured"],
            "quota_depart": self._platform.quotas.status("depart")["configured"],
            "audit_log": self._platform.audit.journal.count() >= 0,
        }
        return {"ready": all(checks.values()), "checks": checks}

    # ---- 动作 ---------------------------------------------------------

    def _arrive(self, params: dict, body: dict) -> dict:
        return self._platform.arrive(
            _require(body, "train_id"),
            _require(body, "station_id"),
            _as_int(body, "position_m", 0),
            _actor(body),
        )

    def _depart(self, params: dict, body: dict) -> dict:
        return self._platform.depart(
            _require(body, "train_id"), _require(body, "station_id"), _actor(body)
        )

    def _gap(self, params: dict, body: dict) -> dict:
        return self._platform.check_gap(
            _require(body, "train_id"), bool(body.get("clear", True)), _actor(body)
        )

    def _enqueue(self, params: dict, body: dict) -> dict:
        return self._platform.enqueue(
            _require(body, "train_id"),
            _require(body, "id"),
            str(body.get("kind", "open")),
            str(body.get("station_id", "")),
            _actor(body),
        )

    def _process(self, params: dict, body: dict) -> dict:
        return self._platform.process_queue(_require(body, "train_id"))

    def _renumber(self, params: dict, body: dict) -> dict:
        groups = body.get("groups")
        if not isinstance(groups, dict) or not groups:
            raise RailError("映射必须是非空对象", component="console")
        return self._platform.renumber(
            _require(body, "station_id"),
            {str(k): str(v) for k, v in groups.items()},
            _actor(body),
        )

    def _batch_open(self, params: dict, body: dict) -> dict:
        return self._platform.batch_open(_require(body, "station_id"), _actor(body))

    def _fault(self, params: dict, body: dict) -> dict:
        return self._platform.set_fault(_require(body, "psd_id"), _actor(body))

    def _clear_fault(self, params: dict, body: dict) -> dict:
        return self._platform.clear_fault(_require(body, "psd_id"), _actor(body))

    def _arm(self, params: dict, body: dict) -> dict:
        return self._platform.arm(_require(body, "train_id"), bool(body.get("armed", True)), _actor(body))

    def _position(self, params: dict, body: dict) -> dict:
        return self._platform.refresh_position(
            _require(body, "train_id"), _as_int(body, "position_m", 0), _actor(body)
        )

    def _sequence(self, params: dict, body: dict) -> dict:
        return self._platform.sequence_verdict(
            _as_int(body, "previous", 0), _as_int(body, "step", 1)
        )

    def _cycle(self, params: dict, body: dict) -> dict:
        return self._platform.cycle(_as_float(body, "dt", 1.0))

    def _sweep(self, params: dict, body: dict) -> dict:
        return self._platform.sweep(_require(body, "station_id"), _actor(body))

    def _dwell(self, params: dict, body: dict) -> dict:
        return self._platform.register_dwell(
            _require(body, "train_id"),
            _as_float(body, "seconds", 0.0),
            str(body.get("station_id", "")),
        )

    def _route(self, params: dict, body: dict) -> dict:
        return self._platform.assign_route(
            _require(body, "train_id"),
            _require(body, "origin"),
            _require(body, "destination"),
            _as_int(body, "distance_m", 0),
        )

    def _reset_state(self, params: dict, body: dict) -> dict:
        return {"removed": self._platform.reset_state()}

    # ---- 班次交接 -----------------------------------------------------

    @property
    def _shifts(self):
        return self._platform.shift_service

    def _shift_desk(self, params: dict, body: dict) -> dict:
        return {"desk": self._shifts.desk(), "ledger": self._shifts.verify_ledger()}

    def _shift_handovers(self, params: dict, body: dict) -> dict:
        return {"handovers": self._shifts.list_handovers()}

    def _shift_handover_view(self, params: dict, body: dict) -> dict:
        return self._shifts.handover_view(str(params.get("handoverID", "")))

    def _shift_items(self, params: dict, body: dict) -> dict:
        include_done = str(params.get("include_done", "")).lower() in ("1", "true", "yes")
        return {"items": self._shifts.list_items(include_done=include_done)}

    def _shift_ledger(self, params: dict, body: dict) -> dict:
        limit = int(params.get("limit", 30))
        return {
            "verify": self._shifts.verify_ledger(),
            "tail": self._shifts.ledger_tail(limit),
        }

    def _shift_operator(self, params: dict, body: dict) -> dict:
        return self._shifts.register_operator(
            _actor(body),
            _require(body, "operator_id"),
            str(body.get("name", "")),
            str(body.get("role", "operator")),
        )

    def _shift_plan(self, params: dict, body: dict) -> dict:
        return self._shifts.plan_shift(_actor(body), _require(body, "shift_id"))

    def _shift_roster(self, params: dict, body: dict) -> dict:
        roster = body.get("roster")
        if not isinstance(roster, list) or not roster:
            raise RailError("roster 必须是非空数组", component="console")
        return self._shifts.roster_incoming(
            _actor(body),
            _require(body, "shift_id"),
            [str(item) for item in roster],
            str(body.get("leader_id", "")),
        )

    def _shift_checkin(self, params: dict, body: dict) -> dict:
        return self._shifts.check_in(
            _actor(body), _require(body, "operator_id"), _require(body, "shift_id")
        )

    def _shift_signout(self, params: dict, body: dict) -> dict:
        return self._shifts.sign_out(_actor(body))

    def _shift_open(self, params: dict, body: dict) -> dict:
        return self._shifts.open_shift(_actor(body), _require(body, "shift_id"))

    def _shift_begin(self, params: dict, body: dict) -> dict:
        deadline = body.get("deadline_s")
        return self._shifts.begin_handover(
            _actor(body),
            str(body.get("incoming_shift_id", "")),
            deadline_s=float(deadline) if deadline is not None else None,
        )

    def _shift_ack(self, params: dict, body: dict) -> dict:
        return self._shifts.acknowledge_item(
            _actor(body), _require(body, "handover_id"), _require(body, "item_id")
        )

    def _shift_suspend(self, params: dict, body: dict) -> dict:
        return self._shifts.suspend_pending(
            _actor(body), _require(body, "handover_id"), str(body.get("reason", ""))
        )

    def _shift_countersign(self, params: dict, body: dict) -> dict:
        return self._shifts.countersign(_actor(body), _require(body, "handover_id"))

    def _shift_complete(self, params: dict, body: dict) -> dict:
        return self._shifts.complete_handover(_actor(body), _require(body, "handover_id"))

    def _shift_force(self, params: dict, body: dict) -> dict:
        return self._shifts.force_close(
            _actor(body), _require(body, "handover_id"), _require(body, "reason")
        )

    def _shift_adopt(self, params: dict, body: dict) -> dict:
        raw = body.get("item_ids")
        item_ids = [str(item) for item in raw] if isinstance(raw, list) else None
        return self._shifts.adopt_orphans(_actor(body), item_ids)

    def _shift_item_register(self, params: dict, body: dict) -> dict:
        payload = body.get("payload")
        return self._shifts.register_item(
            _actor(body),
            str(body.get("kind", "generic")),
            _require(body, "title"),
            ref=str(body.get("ref", "")),
            station_id=str(body.get("station_id", "")),
            payload=payload if isinstance(payload, dict) else None,
        )

    def _shift_item_resume(self, params: dict, body: dict) -> dict:
        return self._shifts.resume_item(
            _actor(body), str(params.get("itemID", "")), str(body.get("leader_id", ""))
        )

    def _shift_item_complete(self, params: dict, body: dict) -> dict:
        return self._shifts.complete_item(_actor(body), str(params.get("itemID", "")))

    def _shift_item_abort(self, params: dict, body: dict) -> dict:
        return self._shifts.abort_item(
            _actor(body), str(params.get("itemID", "")), _require(body, "reason")
        )
