from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_SEARCH_PARAMS = {"ef": 64}


@dataclass(frozen=True)
class VectorStoreSettings:
    search_params: dict = field(default_factory=lambda: dict(DEFAULT_SEARCH_PARAMS))

    @classmethod
    def from_config(cls, raw: dict | None) -> VectorStoreSettings:
        raw = raw or {}
        return cls(search_params=dict(raw.get("searchParams") or DEFAULT_SEARCH_PARAMS))
