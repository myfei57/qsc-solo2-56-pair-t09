"""班次交接：名册、交接流程、挂起放行、时序与恢复。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from psdline.app import build_platform
from psdline.clock import ManualClock
from psdline.config import default_config
from psdline.errors import RailError
from psdline.shift import (
    ACCEPTING,
    COMPLETED,
    DONE,
    OFFERED,
    SUSPENDED,
    HandoverRegistry,
    ShiftRegistry,
    collect_open_items,
)


def _second_operator(platform, name="operator2"):
    platform.shift_register(name, "operator", by="supervisor")
    return name


def _begin_with_items(platform, gate_station="ST1"):
    """开班并制造两类典型在办事项：挂起的故障 + 未执行的指令。"""

    platform.shift_open("operator", "day")
    gate = platform.gates.by_station(gate_station)[0].id
    platform.set_fault(gate)
    platform.enqueue("T01", "cmd-h", "open", gate_station)
    return gate


def test_roster_seeded_and_register_requires_supervisor(platform):
    status = platform.shift_status()
    assert status["roster"]["operators"] == ["operator"]
    assert status["roster"]["supervisors"] == ["supervisor"]
    with pytest.raises(RailError):
        platform.shift_register("operator2", "operator", by="operator")
    with pytest.raises(RailError):
        platform.shift_register("operator2", "ghost", by="supervisor")
    assert platform.shift_register("operator2", "operator", by="supervisor")["ok"] is True
    assert "operator2" in platform.shift_status()["roster"]["operators"]


def test_shift_open_rules(platform):
    assert platform.shift_open("operator", "day")["ok"] is True
    with pytest.raises(RailError):
        platform.shift_open("operator", "double")
    with pytest.raises(RailError):
        platform.shift_open("nobody", "day")
    with pytest.raises(RailError):
        platform.handover_begin("nobody")


def test_collect_open_items_from_registries(platform):
    platform.arrive("T01", "ST1", 0)
    gate = platform.gates.by_station("ST1")[0].id
    platform.set_fault(gate)
    platform.enqueue("T01", "c1", "open", "ST1")
    platform.gaps.probe("T02", False, platform.clock.now())
    platform.doors.set_batch_limit(1)
    platform.batch_open("ST2")
    items = collect_open_items(
        platform.store, platform.gates, platform.faults, platform.gaps, platform.clock.now()
    )
    kinds = {item.key for item in items}
    assert f"fault_active:{gate}" in kinds
    assert "queued_command:T01" in kinds
    assert "gap_blocked:T02" in kinds
    assert "door_open:T01" in kinds
    assert "gates_open:ST1" in kinds
    assert "batch_open_partial:ST2" in kinds
    batch = next(item for item in items if item.kind == "batch_open_partial")
    assert batch.detail["deferred"]


def test_handover_full_flow(platform):
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    gate = _begin_with_items(platform)
    begin = platform.handover_begin("operator")
    assert begin["ok"] is True
    assert begin["handover"]["state"] == OFFERED
    keys = {item["key"] for item in begin["handover"]["items"]}
    assert {f"fault_active:{gate}", "queued_command:T01", "door_open:T01"} <= keys

    accepted = platform.shift_accept(_second_operator(platform), "night")
    assert accepted["handover"]["state"] == ACCEPTING
    assert all(item["state"] == SUSPENDED for item in accepted["handover"]["items"])

    for item in accepted["handover"]["items"]:
        outcome = platform.handover_ack(item["key"], "operator2", resume=True)
        assert outcome["ok"] is True
    assert outcome["handover"]["blocking"] == 0

    approved = platform.handover_approve("supervisor")
    assert approved["handover"]["state"] == COMPLETED
    shifts = {shift["label"]: shift["state"] for shift in platform.handover_history()["shifts"]}
    assert shifts == {"day": "closed", "night": "active"}
    kinds = [entry.kind for entry in platform.audit.list()]
    assert "shift_opened" in kinds and "handover_completed" in kinds


def test_departure_held_until_items_acked(platform):
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    _begin_with_items(platform)
    platform.handover_begin("operator")
    held = platform.depart("T01", "ST1")
    assert held["ok"] is False
    assert "交接事项未确认" in held["violations"][0]

    platform.shift_accept(_second_operator(platform), "night")
    for item in platform.shift_status()["handover"]["items"]:
        platform.handover_ack(item["key"], "operator2")
    departure = platform.depart("T01", "ST1")
    assert departure["ok"] is True


def test_suspended_fault_and_batch_require_ack(platform):
    platform.arrive("T01", "ST1", 0)
    gate = _begin_with_items(platform)
    platform.doors.set_batch_limit(1)
    platform.batch_open("ST2")
    platform.handover_begin("operator")
    platform.shift_accept(_second_operator(platform), "night")

    blocked_fault = platform.clear_fault(gate)
    assert blocked_fault["ok"] is False
    assert "交接事项未确认" in blocked_fault["violations"][0]
    blocked_batch = platform.batch_open("ST2")
    assert blocked_batch["ok"] is False
    assert "交接事项未确认" in blocked_batch["violations"][0]

    platform.handover_ack(f"fault_active:{gate}", "operator2", resume=True)
    platform.handover_ack("batch_open_partial:ST2", "operator2", resume=True)
    platform.sweep("ST1")
    assert platform.clear_fault(gate)["ok"] is True
    platform.doors.set_batch_limit(64)
    finished = platform.batch_open("ST2")
    assert finished["ok"] is True
    assert finished["deferred"] == []
    items = platform.shift_status()["handover"]["items"]
    states = {item["key"]: item["state"] for item in items}
    assert states[f"fault_active:{gate}"] == DONE
    assert states["batch_open_partial:ST2"] == DONE


def test_ack_permission_and_self_review(platform):
    _begin_with_items(platform)
    platform.handover_begin("operator")
    platform.shift_accept(_second_operator(platform), "night")
    with pytest.raises(RailError):
        platform.handover_ack("fault_active:ST1-G01-01", "operator")
    with pytest.raises(RailError):
        platform.handover_ack("fault_active:ST9-99", "operator2")
    with pytest.raises(RailError):
        platform.handover_approve("operator2")
    with pytest.raises(RailError):
        platform.handover_approve("nobody")


def test_approve_requires_all_items_acked(platform):
    _begin_with_items(platform)
    platform.handover_begin("operator")
    platform.shift_accept(_second_operator(platform), "night")
    with pytest.raises(RailError):
        platform.handover_approve("supervisor")


def test_auto_finalize_without_supervisor(tmp_path):
    config = replace(
        default_config(), shift=replace(default_config().shift, require_supervisor=False)
    )
    platform = build_platform(config, clock=ManualClock(), storage_root=str(tmp_path / "s"))
    _begin_with_items(platform)
    platform.handover_begin("operator")
    accepted = platform.shift_accept(_second_operator(platform), "night")
    outcome = None
    for item in accepted["handover"]["items"]:
        outcome = platform.handover_ack(item["key"], "operator2")
    assert outcome["handover"]["state"] == COMPLETED
    assert outcome["handover"]["approved_by"] == ""
    with pytest.raises(RailError):
        platform.handover_approve("supervisor")


def test_abort_returns_shift_to_active(platform):
    _begin_with_items(platform)
    platform.handover_begin("operator")
    with pytest.raises(RailError):
        platform.handover_abort("nobody")
    assert platform.handover_abort("supervisor")["ok"] is True
    status = platform.shift_status()
    assert status["current_shift"]["state"] == "active"
    assert status["handover"] is None
    assert status["departure_hold"]["held"] is False


def test_abort_after_accept_requires_supervisor(platform):
    _begin_with_items(platform)
    platform.handover_begin("operator")
    platform.shift_accept(_second_operator(platform), "night")
    with pytest.raises(RailError):
        platform.handover_abort("operator")
    assert platform.handover_abort("supervisor")["ok"] is True
    status = platform.shift_status()
    assert status["current_shift"]["operator"] == "operator"
    shifts = {shift["operator"]: shift["state"] for shift in platform.handover_history()["shifts"]}
    assert shifts["operator2"] == "closed"


def test_handover_timeout_alarm(platform):
    _begin_with_items(platform)
    platform.handover_begin("operator")
    platform.clock.advance(2000)
    cycle = platform.cycle()
    assert cycle["handover_timeout"] is not None
    assert platform.cycle()["handover_timeout"] is None
    kinds = [entry.kind for entry in platform.audit.list()]
    assert "handover_timeout" in kinds


def test_restart_keeps_handover_state(tmp_path):
    clock = ManualClock()
    root = str(tmp_path / "state")
    platform = build_platform(default_config(), clock=clock, storage_root=root)
    platform.arrive("T01", "ST1", 0)
    _begin_with_items(platform)
    platform.handover_begin("operator")
    platform.shift_accept(_second_operator(platform), "night")

    restarted = build_platform(default_config(), clock=clock, storage_root=root)
    status = restarted.shift_status()
    assert status["handover"]["state"] == ACCEPTING
    assert status["current_shift"]["operator"] == "operator2"
    assert status["departure_hold"]["held"] is True
    assert restarted.depart("T01", "ST1")["ok"] is False
    for item in status["handover"]["items"]:
        restarted.handover_ack(item["key"], "operator2")
    assert restarted.handover_approve("supervisor")["ok"] is True


def test_recover_repairs_half_written_accept(tmp_path):
    clock = ManualClock()
    root = str(tmp_path / "state")
    platform = build_platform(default_config(), clock=clock, storage_root=root)
    _begin_with_items(platform)
    platform.handover_begin("operator")
    handover = platform.shifts.handovers.active()
    # 模拟接班写到一半掉电：交接单已转入 accepting，但新班次文档没落盘。
    broken = replace(handover, state=ACCEPTING, to_shift="shift-ghost", accepted_by="operator2")
    platform.shifts.handovers.save(broken)

    recovered = platform.shifts.recover()
    assert recovered["repairs"]
    status = platform.shift_status()
    assert status["handover"] is None
    assert status["current_shift"]["operator"] == "operator"
    assert status["current_shift"]["state"] == "active"


def test_closed_records_are_immutable(platform):
    _begin_with_items(platform)
    platform.handover_begin("operator")
    accepted = platform.shift_accept(_second_operator(platform), "night")
    for item in accepted["handover"]["items"]:
        platform.handover_ack(item["key"], "operator2")
    platform.handover_approve("supervisor")

    shifts = ShiftRegistry(platform.store)
    closed = [shift for shift in shifts.list() if shift.state == "closed"][0]
    with pytest.raises(RailError):
        shifts.save(replace(closed, operator="hacker"))
    handovers = HandoverRegistry(platform.store)
    done = handovers.list()[0]
    with pytest.raises(RailError):
        handovers.save(replace(done, items=()))
    history = platform.handover_history()
    assert history["handovers"][0]["state"] == COMPLETED


def test_depart_reconciles_open_items(platform):
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    _begin_with_items(platform)
    platform.handover_begin("operator")
    accepted = platform.shift_accept(_second_operator(platform), "night")
    for item in accepted["handover"]["items"]:
        platform.handover_ack(item["key"], "operator2", resume=True)
    assert platform.depart("T01", "ST1")["ok"] is True
    states = {
        item["key"]: item["state"]
        for item in platform.shift_status()["handover"]["items"]
    }
    assert states["door_open:T01"] == DONE
    assert states["queued_command:T01"] == DONE
