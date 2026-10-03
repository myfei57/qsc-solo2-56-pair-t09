"""班次交接：冻结、逐项确认、自动挂起、放行联动、权威代际、强收口与不可变台账。"""

from __future__ import annotations

import glob
import json
import os

import pytest

from psdline.app import build_platform
from psdline.config import default_config
from psdline.errors import RailError, StorageError
from psdline.shift import ShiftService
from psdline.shift.model import (
    HO_DONE,
    HO_REVIEW,
    SHIFT_ACTIVE,
    SHIFT_FROZEN,
    SHIFT_HANDED,
    SHIFT_FORCED,
    WI_ACKNOWLEDGED,
    WI_BATCH,
    WI_DONE,
    WI_FAULT,
    WI_PRESENTED,
    WI_RESUMED,
    WI_SUSPENDED,
)


def bootstrap(platform, deadline_s: float = 300.0):
    """登记两名班组长、两班各一名值班员，甲班开班；返回 (服务, 甲班 actor)。"""

    svc = platform.shift_service
    svc.register_operator(None, "L1", role="leader")
    svc.register_operator(None, "L2", role="leader")
    svc.register_operator(None, "A")
    svc.register_operator(None, "B")
    svc.plan_shift(None, "S1")
    svc.plan_shift(None, "S2")
    svc.roster_incoming({"operator_id": "L1"}, "S1", ["A"], "L1")
    svc.roster_incoming({"operator_id": "L1"}, "S2", ["B"], "L2")
    svc.open_shift(svc.check_in(None, "A", "S1"), "S1")
    actor_a = {"token": svc.check_in(None, "A", "S1")["token"]}
    return svc, actor_a


def handover_with_two_faults(platform, clock, svc, actor_a, *, deadline_s=10.0):
    """在 G02 门区挂两个故障（不影响 G01 主门区到站），发起交接并返回句柄。"""

    gates = [gate.id for gate in platform.gates.by_station("ST1") if gate.group == "G02"][:2]
    for gate_id in gates:
        platform.set_fault(gate_id, actor_a)
    ho = svc.begin_handover(actor_a, "S2", deadline_s=deadline_s)
    return ho, gates


# ---- 无班模式与权限 ----------------------------------------------------------

def test_unmanaged_mode_keeps_legacy_behaviour(platform):
    assert platform.shift_service.guard(None)["allowed"] is True
    assert platform.arrive("T01", "ST1", 0)["ok"] is True
    assert platform.shift_service.check_release("T01", "ST1")["allowed"] is True


def test_bootstrap_and_registration_permissions(platform):
    svc = platform.shift_service
    svc.register_operator(None, "L1", role="leader")
    svc.register_operator(None, "A")
    svc.plan_shift(None, "S1")
    # 已启用后：普通值班员不能登记人员 / 计划班次；必须班组长
    svc.roster_incoming({"operator_id": "L1"}, "S1", ["A"], "L1")
    svc.open_shift(svc.check_in(None, "A", "S1"), "S1")
    actor_a = {"token": svc.check_in(None, "A", "S1")["token"]}
    with pytest.raises(RailError):
        svc.register_operator(actor_a, "X")
    with pytest.raises(RailError):
        svc.plan_shift(actor_a, "S9")
    # 非班组长不能升级班组长
    with pytest.raises(RailError):
        svc.register_operator(actor_a, "X", role="leader")


def test_guard_rejects_unknown_operator_and_off_roster(platform):
    svc, actor_a = bootstrap(platform)
    assert svc.guard({"token": "nope"})["reason"] == "bad_session"
    svc.register_operator({"operator_id": "L1"}, "C")
    svc.plan_shift({"operator_id": "L1"}, "S3")
    svc.roster_incoming({"operator_id": "L1"}, "S3", ["C"], "L1")
    actor_c = {"token": svc.check_in(None, "C", "S3")["token"]}
    # C 属于计划班次 S3，不是当前在值的 S1
    assert svc.guard(actor_c)["reason"] == "not_on_duty"
    assert svc.guard(actor_a)["allowed"] is True


# ---- 冻结窗口 ----------------------------------------------------------------

def test_begin_handover_freezes_all_safety_actions(platform):
    svc, actor_a = bootstrap(platform)
    gates = [gate.id for gate in platform.gates.by_station("ST1") if gate.group == "G02"][:1]
    platform.set_fault(gates[0], actor_a)
    ho = svc.begin_handover(actor_a, "S2")
    assert svc.shifts.require("S1").state == SHIFT_FROZEN
    for action in (
        lambda: platform.arrive("T09", "ST1", 0, actor_a),
        lambda: platform.set_fault("ST1-G02-03", actor_a),
        lambda: platform.batch_open("ST1", actor_a),
        lambda: platform.sweep("ST1", actor_a),
    ):
        with pytest.raises(RailError) as info:
            action()
        assert "冻结" in info.value.message
    # 交接本身不受冻结影响
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    assert svc.acknowledge_item(actor_b, ho["handover_id"], ho["items"][0]["id"])["ok"] is True


def test_cannot_open_second_handover(platform):
    svc, actor_a = bootstrap(platform)
    svc.begin_handover(actor_a, "S2")
    with pytest.raises(RailError):
        svc.begin_handover(actor_a, "S2")


# ---- 逐项确认 ---------------------------------------------------------------

def test_items_presented_and_only_incoming_can_ack(platform):
    svc, actor_a = bootstrap(platform)
    ho, gates = handover_with_two_faults(platform, platform.clock if hasattr(platform, "clock") else None, svc, actor_a)
    items = ho["items"]
    assert {item["state"] for item in items} == {WI_PRESENTED}
    # 交班人不能替接班人确认
    with pytest.raises(RailError):
        svc.acknowledge_item(actor_a, ho["handover_id"], items[0]["id"])
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    svc.acknowledge_item(actor_b, ho["handover_id"], items[0]["id"])
    assert svc.items.require(items[0]["id"]).state == WI_ACKNOWLEDGED
    # 重复确认拒绝
    with pytest.raises(RailError):
        svc.acknowledge_item(actor_b, ho["handover_id"], items[0]["id"])
    # 有未确认项不能完成
    with pytest.raises(RailError):
        svc.complete_handover(actor_b, ho["handover_id"])


def test_deadline_auto_suspends_pending_items(platform, clock):
    svc, actor_a = bootstrap(platform)
    ho, gates = handover_with_two_faults(platform, clock, svc, actor_a, deadline_s=10.0)
    items = ho["items"]
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    svc.acknowledge_item(actor_b, ho["handover_id"], items[0]["id"])
    # 未到点：普通值班员不能提前挂起
    with pytest.raises(RailError):
        svc.suspend_pending(actor_b, ho["handover_id"])
    # 班组长可以提前挂起
    assert svc.suspend_pending({"operator_id": "L1"}, ho["handover_id"])["suspended"] == [items[1]["id"]]
    assert svc.handovers.require(ho["handover_id"]).state == HO_REVIEW
    assert svc.items.require(items[1]["id"]).state == WI_SUSPENDED


def test_tick_auto_suspend_at_deadline(platform, clock):
    svc, actor_a = bootstrap(platform)
    ho, gates = handover_with_two_faults(platform, clock, svc, actor_a, deadline_s=10.0)
    pending = ho["items"][1]["id"]
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    svc.acknowledge_item(actor_b, ho["handover_id"], ho["items"][0]["id"])
    clock.advance(10)
    result = svc.tick()
    assert result["timeouts"][0]["suspended"] == [pending]
    # 周期再推一次不会重复挂起
    clock.advance(1)
    assert svc.tick()["timeouts"] == []
    assert svc.items.require(pending).suspend_reason == "deadline"


# ---- 班组长会签与完成 --------------------------------------------------------

def test_suspended_handover_requires_countersign(platform, clock):
    svc, actor_a = bootstrap(platform)
    ho, gates = handover_with_two_faults(platform, clock, svc, actor_a, deadline_s=10.0)
    items = ho["items"]
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    svc.acknowledge_item(actor_b, ho["handover_id"], items[0]["id"])
    clock.advance(10)
    svc.tick()
    # 无会签不能完成
    with pytest.raises(RailError) as info:
        svc.complete_handover(actor_b, ho["handover_id"])
    assert "班组长" in info.value.message
    # 交班班次班组长不能会签本班组的交接（必须独立第三方班组长）
    with pytest.raises(RailError):
        svc.countersign({"operator_id": "L1"}, ho["handover_id"])
    svc.countersign({"operator_id": "L2"}, ho["handover_id"])
    done = svc.complete_handover(actor_b, ho["handover_id"])
    assert done["state"] == HO_DONE and done["active_shift"] == "S2"
    assert done["release_blocked"] is True and done["suspended"] == [items[1]["id"]]
    assert svc.shifts.require("S1").state == SHIFT_HANDED
    assert svc.shifts.require("S2").state == SHIFT_ACTIVE


def test_clean_handover_completes_without_leader(platform):
    svc, actor_a = bootstrap(platform)
    gate = [g.id for g in platform.gates.by_station("ST1") if g.group == "G02"][0]
    platform.set_fault(gate, actor_a)
    ho = svc.begin_handover(actor_a, "S2")
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    svc.acknowledge_item(actor_b, ho["handover_id"], ho["items"][0]["id"])
    done = svc.complete_handover(actor_b, ho["handover_id"])
    assert done["state"] == HO_DONE and done["suspended"] == []
    assert done["release_blocked"] is False


# ---- 权威代际：跨班迟到命令作废 ----------------------------------------------

def test_epoch_advance_invalidates_outgoing_session(platform, clock):
    svc, actor_a = bootstrap(platform)
    ho, gates = handover_with_two_faults(platform, clock, svc, actor_a, deadline_s=10.0)
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    svc.acknowledge_item(actor_b, ho["handover_id"], ho["items"][0]["id"])
    clock.advance(10)
    svc.tick()
    svc.countersign({"operator_id": "L2"}, ho["handover_id"])
    old_epoch = svc.desk()["epoch"]
    svc.complete_handover(actor_b, ho["handover_id"])
    assert svc.desk()["epoch"] == old_epoch + 1
    # 交班人令牌跨班作废；接班人交接前签的会话自动提升到新代际，无需重签
    verdict = svc.guard(actor_a)
    assert verdict["allowed"] is False
    assert svc.guard(actor_b)["allowed"] is True
    platform.arrive("T07", "ST1", 0, actor_b)


# ---- 发车放行联动 ------------------------------------------------------------

def test_suspended_item_blocks_departure_until_resumed(platform, clock):
    svc, actor_a = bootstrap(platform)
    ho, gates = handover_with_two_faults(platform, clock, svc, actor_a, deadline_s=10.0)
    items = ho["items"]
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    svc.acknowledge_item(actor_b, ho["handover_id"], items[0]["id"])
    clock.advance(10)
    svc.tick()
    svc.countersign({"operator_id": "L2"}, ho["handover_id"])
    svc.complete_handover(actor_b, ho["handover_id"])
    actor_b2 = {"token": svc.check_in(None, "B", "S2")["token"]}
    platform.arrive("T07", "ST1", 0, actor_b2)
    platform.check_gap("T07", True, actor_b2)
    blocked = platform.depart("T07", "ST1", actor_b2)
    assert blocked["ok"] is False
    assert blocked["shift_blockers"][0]["state"] == WI_SUSPENDED
    # 恢复必须班组长会签，且不能自签
    suspended = svc.items.require(items[1]["id"])
    with pytest.raises(RailError):
        svc.resume_item(actor_b2, suspended.id)
    resumed = svc.resume_item(actor_b2, suspended.id, leader_id="L2")
    assert resumed["state"] == WI_RESUMED
    # RESUMED 不再拦截放行
    assert platform.depart("T07", "ST1", actor_b2)["ok"] is True
    # 台账里同时留了拒绝与放行两种记录
    kinds = [entry.kind for entry in svc.ledger.entries()]
    assert "release_denied" in kinds and "release_granted" in kinds


def test_resume_acknowledged_fault_also_needs_leader(platform):
    svc, actor_a = bootstrap(platform)
    gate = [g.id for g in platform.gates.by_station("ST1") if g.group == "G02"][0]
    platform.set_fault(gate, actor_a)
    ho = svc.begin_handover(actor_a, "S2")
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    svc.acknowledge_item(actor_b, ho["handover_id"], ho["items"][0]["id"])
    svc.complete_handover(actor_b, ho["handover_id"])
    actor_b2 = {"token": svc.check_in(None, "B", "S2")["token"]}
    item = svc.items.list()[0]
    with pytest.raises(RailError):
        svc.resume_item(actor_b2, item.id)
    assert svc.resume_item(actor_b2, item.id, leader_id="L2")["state"] == WI_RESUMED


# ---- 故障自动挂账 / 办结 ------------------------------------------------------

def test_fault_lifecycle_auto_items(platform):
    svc, actor_a = bootstrap(platform)
    gate = [g.id for g in platform.gates.by_station("ST1") if g.group == "G02"][0]
    platform.set_fault(gate, actor_a)
    items = svc.items.open_items()
    assert len(items) == 1 and items[0].kind == WI_FAULT and items[0].ref == gate
    # 重复置位不重复挂账
    platform.set_fault(gate, actor_a)
    assert len(svc.items.open_items()) == 1
    platform.clear_fault(gate, actor_a)
    assert svc.items.open_items() == []
    assert svc.items.list()[0].state == WI_DONE
    # 故障不允许终止
    platform.set_fault(gate, actor_a)
    item = svc.items.open_items()[0]
    with pytest.raises(RailError):
        svc.abort_item(actor_a, item.id, "取消")


# ---- 批量开门半截 ------------------------------------------------------------

def test_half_batch_registered_resumed_and_secured(platform):
    svc, actor_a = bootstrap(platform)
    platform.doors.set_batch_limit(2)
    outcome = platform.batch_open("ST1", actor_a)
    assert outcome["deferred"] == ["G03", "G04"]
    item = svc.items.open_items()[0]
    assert item.kind == WI_BATCH and item.ref == "ST1"

    # 交接：接班人确认，零挂起无需会签
    ho = svc.begin_handover(actor_a, "S2")
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    svc.acknowledge_item(actor_b, ho["handover_id"], ho["items"][0]["id"])
    svc.complete_handover(actor_b, ho["handover_id"])
    actor_b2 = {"token": svc.check_in(None, "B", "S2")["token"]}

    # 恢复批量开门：班组长会签后按当前联锁把剩余门区开完
    result = svc.resume_item(actor_b2, item.id, leader_id="L2")
    assert result["state"] == WI_RESUMED
    assert all(gate.open for gate in platform.gates.by_station("ST1"))
    svc.complete_item(actor_b2, item.id)
    assert svc.items.require(item.id).state == WI_DONE


def test_abort_batch_secures_open_gates(platform):
    svc, actor_a = bootstrap(platform)
    platform.doors.set_batch_limit(2)
    platform.batch_open("ST1", actor_a)
    item = svc.items.open_items()[0]
    assert any(gate.open for gate in platform.gates.by_station("ST1"))
    svc.abort_item(actor_a, item.id, "取消本批")
    assert not any(gate.open for gate in platform.gates.by_station("ST1"))
    assert svc.items.require(item.id).state == "aborted"


# ---- 强制收口与孤儿认领 -------------------------------------------------------

def test_force_close_orphans_then_adopt(platform, clock):
    svc, actor_a = bootstrap(platform)
    gate = [g.id for g in platform.gates.by_station("ST1") if g.group == "G02"][0]
    platform.set_fault(gate, actor_a)
    ho = svc.begin_handover(actor_a, "", deadline_s=10.0)
    with pytest.raises(RailError):
        svc.force_close({"operator_id": "L1"}, ho["handover_id"], "")
    clock.advance(10)
    svc.tick()
    forced = svc.force_close({"operator_id": "L1"}, ho["handover_id"], "接班人未到")
    assert forced["state"] == "forced" and forced["orphans"]
    assert svc.shifts.require("S1").state == SHIFT_FORCED
    desk = svc.desk()
    assert desk["active_shift"] == "" and desk["orphans"]
    assert svc.guard(actor_a)["reason"] == "desk_inactive"

    svc.plan_shift({"operator_id": "L1"}, "S3")
    svc.roster_incoming({"operator_id": "L1"}, "S3", ["B"], "L2")
    svc.open_shift(svc.check_in(None, "B", "S3"), "S3")
    # 普通值班员不能认领孤儿
    actor_b = {"token": svc.check_in(None, "B", "S3")["token"]}
    with pytest.raises(RailError):
        svc.adopt_orphans(actor_b)
    leader_b = {"token": svc.check_in(None, "L2", "S3")["token"]}
    adopted = svc.adopt_orphans(leader_b)
    item = svc.items.require(adopted["adopted"][0])
    assert item.owner_shift_id == "S3" and item.state == WI_ACKNOWLEDGED
    platform.clear_fault(gate, actor_b)
    assert svc.items.open_items() == []


# ---- 不可变台账 ---------------------------------------------------------------

def test_ledger_is_append_only_and_detects_tampering(platform, tmp_path):
    svc, actor_a = bootstrap(platform)
    gate = [g.id for g in platform.gates.by_station("ST1") if g.group == "G02"][0]
    platform.set_fault(gate, actor_a)
    platform.clear_fault(gate, actor_a)
    length = svc.ledger.length()
    assert length >= 4
    report = svc.verify_ledger()
    assert report["ok"] and report["entries"] == length

    ledger_file = sorted(glob.glob(os.path.join(platform.store.root, "shift-ledger-*.jsonl")))[0]
    lines = open(ledger_file, encoding="utf-8").read().splitlines()
    row = json.loads(lines[2])
    row["payload"]["tampered"] = True
    lines[2] = json.dumps(row, ensure_ascii=False)
    open(ledger_file, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    with pytest.raises(StorageError):
        ShiftService(platform.store, platform.clock, platform.audit)


def test_recovery_rebuilds_projection_from_ledger(platform, clock):
    """投影文档被删/损坏时，重放台账必须能把当前态完整重建。"""

    svc, actor_a = bootstrap(platform)
    ho, gates = handover_with_two_faults(platform, clock, svc, actor_a, deadline_s=10.0)
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    svc.acknowledge_item(actor_b, ho["handover_id"], ho["items"][0]["id"])
    clock.advance(10)
    svc.tick()
    svc.countersign({"operator_id": "L2"}, ho["handover_id"])
    svc.complete_handover(actor_b, ho["handover_id"])
    ledger_entries = svc.ledger.length()

    # 删掉全部投影文档（只留哈希链台账与现场文档）。
    for doc_id in platform.store.list_ids():
        if doc_id.startswith("shift/operator/") or doc_id.startswith("shift/shift/") \
                or doc_id.startswith("shift/handover/") or doc_id.startswith("shift/item/"):
            assert platform.store.remove(doc_id) is True

    rebuilt = build_platform(default_config(), clock=clock, storage_root=platform.store.root)
    recovered = rebuilt.shift_service
    assert recovered.verify_ledger()["entries"] == ledger_entries
    assert recovered.desk()["active_shift"] == "S2"
    assert {item.ref: item.state for item in recovered.items.open_items()} == {
        gates[0]: WI_ACKNOWLEDGED,
        gates[1]: WI_SUSPENDED,
    }
    handover = recovered.handovers.require(ho["handover_id"])
    assert handover.state == HO_DONE and handover.leader_id == "L2"
    assert recovered.operators.require("A").id == "A"


def test_recovery_replays_ledger(platform, clock):
    svc, actor_a = bootstrap(platform)
    ho, gates = handover_with_two_faults(platform, clock, svc, actor_a, deadline_s=10.0)
    actor_b = {"token": svc.check_in(None, "B", "S2")["token"]}
    svc.acknowledge_item(actor_b, ho["handover_id"], ho["items"][0]["id"])
    clock.advance(10)
    svc.tick()
    svc.countersign({"operator_id": "L2"}, ho["handover_id"])
    svc.complete_handover(actor_b, ho["handover_id"])
    epoch = svc.desk()["epoch"]
    entries = svc.ledger.length()

    rebuilt = build_platform(default_config(), clock=clock, storage_root=platform.store.root)
    recovered = rebuilt.shift_service
    assert recovered.verify_ledger()["entries"] == entries
    desk = recovered.desk()
    assert desk["active_shift"] == "S2" and desk["epoch"] == epoch
    assert desk["release_blocked"] is True
    states = {item.ref: item.state for item in recovered.items.open_items()}
    assert states[gates[0]] == WI_ACKNOWLEDGED and states[gates[1]] == WI_SUSPENDED
    # 会话不持久化，恢复后必须重新签到
    assert recovered.guard(actor_b)["reason"] == "bad_session"


# ---- 控制台端到端 -------------------------------------------------------------

def test_console_shift_flow(platform):
    from psdline.console.api import Api

    api = Api(platform)

    def call(method, path, body=None, params=None):
        return api.handle(method, path, params or {}, body or {})

    status, _ = call("POST", "/api/shift/operator", {"operator_id": "L1", "role": "leader"})
    assert status == 200
    call("POST", "/api/shift/operator", {"operator_id": "L2", "role": "leader"})
    call("POST", "/api/shift/operator", {"operator_id": "A"})
    call("POST", "/api/shift/operator", {"operator_id": "B"})
    call("POST", "/api/shift/plan", {"shift_id": "S1"})
    call("POST", "/api/shift/plan", {"shift_id": "S2"})
    call("POST", "/api/shift/roster", {"operator_id": "L1", "shift_id": "S1", "roster": ["A"], "leader_id": "L1"})
    call("POST", "/api/shift/roster", {"operator_id": "L1", "shift_id": "S2", "roster": ["B"], "leader_id": "L2"})
    status, checkin = call("POST", "/api/shift/checkin", {"operator_id": "A", "shift_id": "S1"})
    token_a = checkin["token"]
    assert status == 200
    call("POST", "/api/shift/open", {"token": token_a, "shift_id": "S1"})

    status, fault = call("POST", "/api/fault", {"token": token_a, "psd_id": "ST1-G02-01"})
    assert status == 200 and fault["locked"] is True
    status, ho = call("POST", "/api/shift/begin-handover", {"token": token_a, "incoming_shift_id": "S2"})
    assert status == 200 and ho["frozen"] is True

    # 冻结期间走 API 开门被 409 拒绝
    status, denied = call("POST", "/api/arrive", {"token": token_a, "train_id": "T9", "station_id": "ST1", "position_m": 0})
    assert status == 409 and "冻结" in denied["message"]

    status, checkin_b = call("POST", "/api/shift/checkin", {"operator_id": "B", "shift_id": "S2"})
    token_b = checkin_b["token"]
    call("POST", "/api/shift/acknowledge", {"token": token_b, "handover_id": ho["handover_id"], "item_id": ho["items"][0]["id"]})
    status, done = call("POST", "/api/shift/complete-handover", {"token": token_b, "handover_id": ho["handover_id"]})
    assert status == 200 and done["state"] == HO_DONE

    status, desk = call("GET", "/api/shift/desk", {})
    assert status == 200 and desk["desk"]["active_shift"] == "S2"
    assert desk["ledger"]["ok"] is True

    status, overview = call("GET", "/api/status", {})
    assert status == 200 and overview["shift"]["managed"] is True

    status, ledger = call("GET", "/api/shift/ledger", params={"limit": "5"})
    assert status == 200 and len(ledger["tail"]) == 5
