"""交接权限名册。

谁能交班、谁能接班、谁能复核，都按名册判定：值班员开班与接班，班组长复核并
维护名册。名册落盘保存，重启不丢；装配时由配置播种，空名册允许直接登记第一
批人（否则永远没有人能做第一次登记）。名册只增不删：在审计里出现过的人必须
能一直被认出，离职停用属于线下管理，不改写历史。
"""

from __future__ import annotations

from ..errors import RailError
from ..store import JsonStore

OPERATOR = "operator"
SUPERVISOR = "supervisor"
ROLES = (OPERATOR, SUPERVISOR)

ROLE_LABELS = {OPERATOR: "值班员", SUPERVISOR: "班组长"}


def _key(role: str) -> str:
    return f"{role}s"


class Roster:
    """人员名册读写，落在 ``shift/roster`` 文档上。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id() -> str:
        return "shift/roster"

    def _load(self) -> dict:
        raw = self._store.read(self._doc_id())
        if not isinstance(raw, dict):
            return {_key(OPERATOR): [], _key(SUPERVISOR): []}
        return {
            key: sorted(str(name) for name in raw.get(key, []) if str(name))
            for key in (_key(OPERATOR), _key(SUPERVISOR))
        }

    def empty(self) -> bool:
        """名册是否一个人都没有（装配期播种用）。"""

        data = self._load()
        return not data[_key(OPERATOR)] and not data[_key(SUPERVISOR)]

    def members(self, role: str) -> list[str]:
        """列出某角色的全部人员（排序）。"""

        return list(self._load().get(_key(role), []))

    def has(self, name: str, role: str) -> bool:
        """判断某人是否登记为某角色。"""

        return bool(name) and name in self._load().get(_key(role), [])

    def require(self, name: str, role: str) -> None:
        """要求某人必须登记为某角色，否则拒绝。"""

        if not name:
            raise RailError("人员姓名不能为空", component="shift")
        if not self.has(name, role):
            raise RailError(
                f"{name} 不是登记的{ROLE_LABELS.get(role, role)}", component="shift"
            )

    def register(self, name: str, role: str) -> dict:
        """把某人登记进名册；已登记时直接返回现状。"""

        if role not in ROLES:
            raise RailError(f"未知角色: {role}", component="shift")
        if not name:
            raise RailError("人员姓名不能为空", component="shift")
        data = self._load()
        key = _key(role)
        if name not in data[key]:
            data[key] = sorted(data[key] + [name])
            self._store.write(self._doc_id(), data)
        return {"name": name, "role": role, "members": list(data[key])}
