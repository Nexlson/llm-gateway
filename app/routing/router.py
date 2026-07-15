from __future__ import annotations

from dataclasses import dataclass

from app.routing.features import extract_features
from app.routing.pools import PoolResolver
from app.routing.rules import RuleEngine


@dataclass
class Resolution:
    pool: str
    provider: str
    model: str
    route_stage: str
    route_reason: str


class Router:
    def __init__(self, rule_engine: RuleEngine, pool_resolver: PoolResolver) -> None:
        self._rules = rule_engine
        self._pools = pool_resolver

    def route(self, body: dict, headers: dict[str, str]) -> Resolution:
        features = extract_features(body, headers)
        match = self._rules.match(features)

        if match is not None and self._pools.has_pool(match.pool):
            pool, stage, reason = match.pool, "rule", f"rule:{match.rule_name}"
        elif match is not None:
            pool, stage = self._pools.default_pool, "default"
            reason = f"rule:{match.rule_name}:unknown-pool:{match.pool}"
        else:
            pool, stage, reason = self._pools.default_pool, "default", "no-rule-matched"

        entry = self._pools.first_entry(pool)
        return Resolution(
            pool=pool, provider=entry.provider, model=entry.model,
            route_stage=stage, route_reason=reason,
        )
