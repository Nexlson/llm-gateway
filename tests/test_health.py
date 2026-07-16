from app.routing.health import HealthTracker


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


def test_unknown_entry_is_healthy():
    h = HealthTracker(cooldown_seconds=60, clock=FakeClock().now)
    assert h.is_healthy("deepseek", "deepseek-chat") is True


def test_marked_entry_is_unhealthy_within_window():
    clock = FakeClock(1000.0)
    h = HealthTracker(cooldown_seconds=60, clock=clock.now)
    h.mark_unhealthy("deepseek", "deepseek-chat")
    assert h.is_healthy("deepseek", "deepseek-chat") is False
    clock.t = 1000.0 + 59.0
    assert h.is_healthy("deepseek", "deepseek-chat") is False


def test_entry_recovers_exactly_at_cooldown_boundary():
    clock = FakeClock(1000.0)
    h = HealthTracker(cooldown_seconds=60, clock=clock.now)
    h.mark_unhealthy("deepseek", "deepseek-chat")
    clock.t = 1000.0 + 60.0            # boundary: healthy again (the free attempt)
    assert h.is_healthy("deepseek", "deepseek-chat") is True
    clock.t = 1000.0 + 61.0
    assert h.is_healthy("deepseek", "deepseek-chat") is True


def test_remark_extends_window():
    clock = FakeClock(1000.0)
    h = HealthTracker(cooldown_seconds=60, clock=clock.now)
    h.mark_unhealthy("deepseek", "deepseek-chat")
    clock.t = 1060.0                    # would have recovered
    h.mark_unhealthy("deepseek", "deepseek-chat")   # fails its free attempt -> new window
    assert h.is_healthy("deepseek", "deepseek-chat") is False
    clock.t = 1119.0
    assert h.is_healthy("deepseek", "deepseek-chat") is False
    clock.t = 1120.0
    assert h.is_healthy("deepseek", "deepseek-chat") is True


def test_entries_are_independent():
    clock = FakeClock(1000.0)
    h = HealthTracker(cooldown_seconds=60, clock=clock.now)
    h.mark_unhealthy("deepseek", "deepseek-chat")
    assert h.is_healthy("anthropic", "claude-haiku-4-5") is True
    assert h.is_healthy("deepseek", "deepseek-chat") is False
