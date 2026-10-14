import pytest

import droid_mcp
from tests.fakes import FakeClock, FakeDevice


@pytest.fixture
def device(monkeypatch):
    """A fake device wired in as the server's backend, isolated from the developer's environment."""
    fake = FakeDevice()
    fake.backend = droid_mcp.AndroidBackend()
    monkeypatch.setattr(fake.backend, "adb", fake.adb)
    monkeypatch.setattr(droid_mcp, "dev", fake.backend)
    # Pin what the module read from SHOT_MAX_W / TRAJ_LOG at import, so a shell that sets them can't change results
    # or have the suite append to a real trajectory log.
    monkeypatch.setattr(droid_mcp, "MAX_W", 540)
    monkeypatch.setattr(droid_mcp, "TRAJ_LOG", None)
    # settle() polls against a deadline; a fake clock keeps that instant and deterministic.
    clock = FakeClock()
    monkeypatch.setattr(droid_mcp.time, "time", clock.time)
    monkeypatch.setattr(droid_mcp.time, "sleep", clock.sleep)
    return fake
