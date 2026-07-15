from __future__ import annotations

from app.core.config import PoolEntry


class PoolResolver:
    """Maps a pool name to its ordered list of entries.

    M2 uses only the first entry; the full ordered list is exposed so M3's
    fallback chain can iterate it without changing this interface.
    """

    def __init__(self, pools: dict[str, list[PoolEntry]], default_pool: str) -> None:
        self._pools = pools
        self._default_pool = default_pool

    @property
    def default_pool(self) -> str:
        return self._default_pool

    def has_pool(self, name: str) -> bool:
        return name in self._pools

    def entries(self, name: str) -> list[PoolEntry]:
        return self._pools[name]

    def first_entry(self, name: str) -> PoolEntry:
        return self._pools[name][0]
