"""班次交接的不可变哈希链台账。

每一条台账记录都带：

* ``seq``        本值班台内单调递增的序号（也是权威代际的来源）；
* ``kind``       事件类型；
* ``at``         事件时刻；
* ``actor``      操作人标识；
* ``payload``    事件内容（只含可 JSON 化数据）；
* ``prev_hash``  上一条记录的哈希；
* ``hash``       本条记录对以上字段做规范 JSON 后计算的 SHA-256。

台账**只追加**：没有任何更新、删除接口。任何对历史文件的篡改（改内容、删行、
插行、换序）都会让 :meth:`ShiftLedger.verify` 在第一条失配处失败。当前态投影
（``registries``）可以丢、可以重建，台账不能。

部分失败恢复语义：业务流程是"先追加台账（fsync 落盘）→ 再更新投影 → 再写
审计"。若在投影更新前崩溃，重放台账即可补齐；若在审计前崩溃，恢复时补一条
``recovered`` 审计并以台账为准，不允许以投影反推台账。
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any

from ..errors import RailError, StorageError
from ..store import JsonlJournal

GENESIS_HASH = "0" * 64
LEDGER_TOPIC = "shift-ledger"


def canonical(payload: Any) -> str:
    """生成排序键、无空白的规范 JSON，保证同内容哈希稳定。"""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_entry(seq: int, kind: str, at: float, actor: str, payload: Any, prev_hash: str) -> str:
    """计算一条台账记录的哈希。"""

    body = canonical(
        {
            "seq": seq,
            "kind": kind,
            "at": at,
            "actor": actor,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerEntry:
    """台账中的一条不可变记录。"""

    seq: int
    kind: str
    at: float
    actor: str
    payload: dict = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    hash: str = ""

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "at": self.at,
            "actor": self.actor,
            "payload": copy.deepcopy(self.payload),
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "LedgerEntry":
        payload = raw.get("payload", {})
        return cls(
            seq=int(raw.get("seq", 0)),
            kind=str(raw.get("kind", "")),
            at=float(raw.get("at", 0.0)),
            actor=str(raw.get("actor", "")),
            payload=copy.deepcopy(payload) if isinstance(payload, dict) else {},
            prev_hash=str(raw.get("prev_hash", GENESIS_HASH)),
            hash=str(raw.get("hash", "")),
        )


@dataclass(frozen=True)
class ChainReport:
    """哈希链校验结果。"""

    ok: bool
    entries: int
    first_bad_seq: int = 0
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "entries": self.entries,
            "first_bad_seq": self.first_bad_seq,
            "reason": self.reason,
        }


class ShiftLedger:
    """只追加的班次台账（内存索引 + JSONL 分段落盘）。"""

    def __init__(self, journal: JsonlJournal) -> None:
        self._journal = journal
        self._lock = threading.RLock()
        self._entries: list[LedgerEntry] = []
        self._load()

    # ---- 读取 ---------------------------------------------------------

    def _load(self) -> None:
        records = self._journal.read_all()
        entries = [LedgerEntry.from_dict(raw) for raw in records if isinstance(raw, dict)]
        report = _check_chain(entries)
        if not report.ok:
            raise StorageError(
                f"班次台账哈希链在第 {report.first_bad_seq} 条失配: {report.reason}",
                component="shift",
            )
        self._entries = entries

    def entries(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries)

    def tail(self, limit: int = 50) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries[-limit:])

    def length(self) -> int:
        with self._lock:
            return len(self._entries)

    def last_seq(self) -> int:
        with self._lock:
            return self._entries[-1].seq if self._entries else 0

    def last_hash(self) -> str:
        with self._lock:
            return self._entries[-1].hash if self._entries else GENESIS_HASH

    # ---- 写入 ---------------------------------------------------------

    def append(self, kind: str, at: float, actor: str, payload: dict | None = None) -> LedgerEntry:
        """追加一条记录；序号与哈希由台账统一签发。

        序号在锁内分配并立即 fsync 落盘，保证跨班并发提交不会拿到同一个序号。
        """

        if not kind:
            raise RailError("台账事件类型为空", component="shift")
        body = copy.deepcopy(payload) if isinstance(payload, dict) else {}
        with self._lock:
            seq = self.last_seq() + 1
            prev_hash = self.last_hash()
            digest = hash_entry(seq, kind, at, actor, body, prev_hash)
            entry = LedgerEntry(
                seq=seq,
                kind=kind,
                at=at,
                actor=actor,
                payload=body,
                prev_hash=prev_hash,
                hash=digest,
            )
            self._journal.append(entry.as_dict())
            self._entries.append(entry)
            return entry

    # ---- 校验 ---------------------------------------------------------

    def verify(self) -> ChainReport:
        """重算整条哈希链。"""

        with self._lock:
            return _check_chain(list(self._entries))


def _check_chain(entries: list[LedgerEntry]) -> ChainReport:
    """逐链重算哈希，并检查序号连续。"""

    previous = GENESIS_HASH
    for index, entry in enumerate(entries, start=1):
        if entry.seq != index:
            return ChainReport(False, len(entries), entry.seq, "seq_not_contiguous")
        if entry.prev_hash != previous:
            return ChainReport(False, len(entries), entry.seq, "prev_hash_mismatch")
        expected = hash_entry(entry.seq, entry.kind, entry.at, entry.actor, entry.payload, previous)
        if entry.hash != expected:
            return ChainReport(False, len(entries), entry.seq, "hash_mismatch")
        previous = entry.hash
    return ChainReport(True, len(entries))
