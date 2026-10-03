"""在办事项采集。

交接班要过的"在办事项"不靠交班人回忆，而是从各登记表现扫出来：故障登记表
给未清除的故障，间隙登记表给未确认的可疑探测，指令队列给没执行的门控指令，
门控落盘文档给开了门没发车的列车，站台门登记表给没清扫的门区，批量余量文档
给开了一半的批量开门。扫描是纯查询，交接发起时把结果快照进交接单，扫描时刻
之后新出现的情况归下一班，不进本次交接。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..gap import GapRegistry
from ..interlock import FaultRegistry
from ..psd import PSDRegistry
from ..store import JsonStore

FAULT_ACTIVE = "fault_active"
BATCH_PARTIAL = "batch_open_partial"
QUEUED_COMMAND = "queued_command"
GAP_BLOCKED = "gap_blocked"
DOOR_OPEN = "door_open"
GATES_OPEN = "gates_open"
ITEM_KINDS = (
    FAULT_ACTIVE,
    BATCH_PARTIAL,
    QUEUED_COMMAND,
    GAP_BLOCKED,
    DOOR_OPEN,
    GATES_OPEN,
)

# 事项挂在列车侧还是站台侧，决定审计流水落到哪条资产时间线上。
TRAIN_KINDS = (QUEUED_COMMAND, GAP_BLOCKED, DOOR_OPEN)
STATION_KINDS = (BATCH_PARTIAL, GATES_OPEN)


@dataclass(frozen=True)
class OpenItem:
    """一条扫描出来的在办事项。"""

    kind: str
    ref: str
    summary: str
    since: float
    detail: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """事项在交接单里的稳定标识。"""

        return f"{self.kind}:{self.ref}"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "ref": self.ref,
            "summary": self.summary,
            "since": self.since,
            "detail": dict(self.detail),
        }


def batch_doc_id(station_id: str) -> str:
    return f"shift/batch/{station_id}"


def note_batch_remainder(
    store: JsonStore,
    station_id: str,
    deferred: list[str],
    skipped: dict,
    at: float,
) -> None:
    """批量开门没开完时把剩余门区落盘；开完了就清掉余量文档。

    被联锁跳过的门区不记进余量：那部分由故障事项覆盖，避免一件事在交接单
    里出现两次。
    """

    if deferred:
        store.write(
            batch_doc_id(station_id),
            {
                "station_id": station_id,
                "deferred": [str(group) for group in deferred],
                "skipped": dict(skipped),
                "at": at,
            },
        )
    else:
        store.remove(batch_doc_id(station_id))


def collect_open_items(
    store: JsonStore,
    gates: PSDRegistry,
    faults: FaultRegistry,
    gaps: GapRegistry,
    now: float,
) -> list[OpenItem]:
    """从各登记表扫描当前全部在办事项（按事项标识排序）。"""

    items: list[OpenItem] = []
    for psd_id in faults.active_ids():
        raw = store.read(f"interlock/{psd_id}/fault")
        since = float(raw.get("at", now)) if isinstance(raw, dict) else now
        items.append(OpenItem(FAULT_ACTIVE, psd_id, "门区故障置位未清除", since))
    for train_id in gaps.blocked_ids():
        raw = store.read(f"gap/{train_id}")
        since = float(raw.get("at", now)) if isinstance(raw, dict) else now
        items.append(OpenItem(GAP_BLOCKED, train_id, "间隙探测可疑未确认", since))
    for doc_id in store.list_ids():
        if doc_id.startswith("commands/"):
            raw = store.read(doc_id)
            if isinstance(raw, list) and raw:
                train_id = doc_id.split("/", 1)[1]
                ids = [str(item.get("id", "")) for item in raw if isinstance(item, dict)]
                items.append(
                    OpenItem(
                        QUEUED_COMMAND,
                        train_id,
                        f"门控指令未执行 {len(ids)} 条",
                        now,
                        {"commands": ids},
                    )
                )
        elif doc_id.startswith("door/") and doc_id.endswith("/open"):
            train_id = doc_id.split("/")[1]
            if not store.exists(f"door/{train_id}/departed"):
                raw = store.read(doc_id)
                since = float(raw.get("at", now)) if isinstance(raw, dict) else now
                items.append(OpenItem(DOOR_OPEN, train_id, "开门后未发车", since))
        elif doc_id.startswith("shift/batch/"):
            raw = store.read(doc_id)
            if isinstance(raw, dict) and raw.get("deferred"):
                deferred = [str(group) for group in raw.get("deferred", [])]
                items.append(
                    OpenItem(
                        BATCH_PARTIAL,
                        str(raw.get("station_id", "")),
                        f"批量开门剩余 {len(deferred)} 个门区",
                        float(raw.get("at", now)),
                        {"deferred": deferred},
                    )
                )
    open_by_station: dict[str, list[str]] = {}
    for gate in gates.list():
        if gate.open:
            open_by_station.setdefault(gate.station, []).append(gate.id)
    for station_id, gate_ids in sorted(open_by_station.items()):
        items.append(
            OpenItem(
                GATES_OPEN,
                station_id,
                f"站台门未清扫 {len(gate_ids)} 樘",
                now,
                {"gates": sorted(gate_ids)},
            )
        )
    return sorted(items, key=lambda item: item.key)
