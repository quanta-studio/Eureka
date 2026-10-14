import pytest

import droid_mcp
from tests.fakes import FakeAgent, FakeClock, FakeDevice


@pytest.fixture
def device(monkeypatch, tmp_path):
    """A fake device wired in as the server's backend, isolated from the developer's environment."""
    fake = FakeDevice(tmp_path)
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


@pytest.fixture
def agent(device, monkeypatch):
    """An AgentBackend wired in as the server's backend, talking to a FakeAgent. What still goes through adb
    (snapshots, the screen size, dumpsys) is answered by the fake device."""
    fake = FakeAgent()
    fake.device = device
    fake.backend = droid_mcp.AgentBackend()
    monkeypatch.setattr(fake.backend, "adb", device.adb)
    monkeypatch.setattr(fake.backend, "_open", fake.open)
    monkeypatch.setattr(droid_mcp, "dev", fake.backend)
    yield fake
    fake.backend._close()
