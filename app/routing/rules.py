from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import RuleConfig, RuleWhen
from app.routing.features import RequestFeatures

_TEMPLATE = re.compile(r"^\s*\{\{\s*header\.([A-Za-z0-9_-]+)\s*\}\}\s*$")


@dataclass
class RuleMatch:
    pool: str
    rule_name: str


class RuleEngine:
    def __init__(self, rules: list[RuleConfig]) -> None:
        self._rules = rules
        # Precompile system_regex per rule (index-aligned; None where unset).
        self._regex: list[re.Pattern[str] | None] = [
            re.compile(r.when.system_regex) if r.when.system_regex is not None else None
            for r in rules
        ]

    def match(self, features: RequestFeatures) -> RuleMatch | None:
        for rule, pattern in zip(self._rules, self._regex):
            if not self._when_matches(rule.when, pattern, features):
                continue
            pool = self._resolve_pool(rule.pool, features)
            if pool is None:
                continue  # templated header absent/empty → try later rules
            return RuleMatch(pool=pool, rule_name=rule.name)
        return None

    @staticmethod
    def _when_matches(
        when: RuleWhen, pattern: re.Pattern[str] | None, f: RequestFeatures
    ) -> bool:
        if when.max_input_tokens is not None and f.input_tokens > when.max_input_tokens:
            return False
        if when.min_input_tokens is not None and f.input_tokens < when.min_input_tokens:
            return False
        if when.has_tools is not None and f.has_tools != when.has_tools:
            return False
        if when.header is not None and not f.headers.get(when.header.lower()):
            return False
        if pattern is not None and not pattern.search(f.system_prompt):
            return False
        return True

    @staticmethod
    def _resolve_pool(pool: str, f: RequestFeatures) -> str | None:
        m = _TEMPLATE.match(pool)
        if m is None:
            return pool  # literal pool name
        value = f.headers.get(m.group(1).lower())
        return value or None
