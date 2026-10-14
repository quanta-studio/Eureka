"""AgentBackend: the same tools, observed and driven through droid-agent instead of uiautomator and `input`."""

import io
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage

import droid_mcp
from tests.fakes import FakeAgent, hierarchy, node

HOME = hierarchy(
    node(text="Network", clickable=True, bounds="[0,200][1080,400]"),
    node(cls="android.widget.EditText", resource_id="com.android.settings:id/search", bounds="[0,0][1080,100]"),
)
DETAIL = hierarchy(node(text="Wi-Fi", clickable=True, bounds="[0,200][1080,400]"))


def test_observe_comes_entirely_from_the_agent(agent):
    agent.show(HOME)

    text, image = droid_mcp.observe()

    assert text.splitlines() == [
        "activity: com.android.settings/.Settings",
        '[0] TextView "Network" tap',
        '[1] EditText "" id=search',
    ]
    assert PILImage.open(io.BytesIO(image.data)).size == (540, 1200)
    assert agent.device.calls == []  # no uiautomator, screencap or dumpsys


def test_the_agent_is_asked_to_wait_for_the_ui_to_go_quiet(agent):
    droid_mcp.observe(screenshot=False)

    (dump,) = [r for r in agent.requests if r["cmd"] == "dump"]
    assert dump["quiet_ms"] == droid_mcp.AGENT_QUIET_MS
    assert dump["timeout_ms"] == droid_mcp.AGENT_SETTLE_MS


def test_screenshots_are_capped_at_the_configured_width(agent, monkeypatch):
    monkeypatch.setattr(droid_mcp, "MAX_W", 360)

    droid_mcp.observe()

    (shot,) = [r for r in agent.requests if r["cmd"] == "screenshot"]
    assert shot["max_w"] == 360


def test_foreground_activity_falls_back_to_dumpsys_when_the_agent_cannot_tell(agent):
    agent.activity = None
    agent.device.activity = "com.example/.Main"

    text = droid_mcp.observe(screenshot=False)[0]

    assert text.splitlines()[0] == "activity: com.example/.Main"


def test_tap_sends_the_elements_tap_point_in_device_pixels(agent):
    agent.show(HOME)
    droid_mcp.observe()
    agent.show(DETAIL)

    text, _ = droid_mcp.tap(0)

    assert agent.actions == [("tap", {"x": 540, "y": 300})]
    assert '[0] TextView "Wi-Fi" tap' in text


def test_tap_xy_scales_by_what_the_agents_screenshot_reported(agent):
    agent.shot = (agent.shot[0], 1.5)
    droid_mcp.observe()

    droid_mcp.tap_xy(100, 250)

    assert agent.actions == [("tap", {"x": 150, "y": 375})]


def test_type_text_sends_any_text_verbatim(agent):
    agent.show(HOME)
    droid_mcp.observe()
    text = 'café 你好 😀 "q" $(id) 100%s'

    droid_mcp.type_text(text, element=1, submit=True)

    assert agent.actions == [("tap", {"x": 540, "y": 50}), ("text", {"text": text}), ("key", {"code": 66})]


def test_empty_text_sends_nothing(agent):
    agent.backend.text("")

    assert agent.actions == []


def test_swipe_is_one_gesture_across_the_middle_of_the_screen(agent):
    agent.device.size = (1080, 2400)

    droid_mcp.swipe("down")

    assert agent.actions == [("swipe", {"x1": 540, "y1": 1600, "x2": 540, "y2": 800, "ms": 300})]


def test_launch_goes_through_the_agent_so_it_can_wait_for_the_app_to_draw(agent):
    droid_mcp.launch_app("com.android.settings")

    assert agent.actions == [("launch", {"package": "com.android.settings"})]
    assert not any(c[:2] == ("shell", "monkey") for c in agent.device.calls)


def test_unknown_key_is_refused_before_anything_is_sent(agent):
    with pytest.raises(ValueError, match="volume_up"):
        droid_mcp.press("volume_up")

    assert agent.actions == []


def test_an_error_on_the_device_is_raised_with_the_agents_message(agent):
    agent.errors["text"] = "no focused text field to type into; tap one first"

    with pytest.raises(RuntimeError, match="no focused text field"):
        droid_mcp.type_text("hello")


def test_the_agent_is_started_once_and_reused(agent):
    droid_mcp.observe()
    droid_mcp.press("back")

    assert agent.opened == 1


def test_an_observation_survives_a_dropped_connection_by_starting_a_new_agent(agent):
    agent.show(HOME)
    agent.drops.append("dump")

    text = droid_mcp.observe(screenshot=False)[0]

    assert '[0] TextView "Network" tap' in text
    assert agent.opened == 2


def test_an_action_is_not_repeated_after_a_dropped_connection(agent):
    agent.drops.append("key")

    with pytest.raises(RuntimeError, match="Lost the connection.*key"):
        droid_mcp.press("back")

    assert agent.actions == [("key", {"code": 4})]  # it may have happened; sending it again could do it twice
    droid_mcp.observe()
    assert agent.opened == 2


def test_snapshot_load_replaces_the_agent(agent):
    droid_mcp.observe()

    droid_mcp.snapshot("load", "clean")
    droid_mcp.observe()

    assert ("emu", "avd", "snapshot", "load", "clean") in agent.device.calls
    assert agent.opened == 2


def test_snapshot_save_keeps_the_agent(agent):
    droid_mcp.observe()

    droid_mcp.snapshot("save", "clean")
    droid_mcp.observe()

    assert agent.opened == 1


# --- starting the agent ---------------------------------------------------------------------------------------------


class FakeShell:
    """Stands in for the `adb shell` the agent runs under."""

    def __init__(self, cmd, stdin, stdout, stderr, exit_with=None, output=b""):
        self.cmd, self.stdin, self.returncode = cmd, stdin, exit_with
        stdout.write(output)
        stdout.flush()

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self):
        return self.returncode


@pytest.fixture
def starting(device, monkeypatch, tmp_path):
    """An AgentBackend whose _open runs for real, against the fake device's adb, a fake `adb shell`, and a FakeAgent
    behind each connection in `connections`: an agent's token, or an exception. Once they run out, nothing is
    listening."""
    jar = tmp_path / "droid-agent.jar"
    jar.write_bytes(b"dex")
    monkeypatch.setattr(droid_mcp, "AGENT_JAR", jar)
    monkeypatch.setattr(droid_mcp.secrets, "token_hex", lambda n: "ours")
    s = SimpleNamespace(
        backend=droid_mcp.AgentBackend("emulator-5554"), device=device, agent=FakeAgent(), shells=[], shell={}
    )
    s.connections = []
    monkeypatch.setattr(s.backend, "adb", device.adb)

    def popen(cmd, **kwargs):
        s.shells.append(FakeShell(cmd, **kwargs, **s.shell))
        return s.shells[-1]

    def connect(*args, **kwargs):
        answer = s.connections.pop(0) if s.connections else ConnectionRefusedError()
        if isinstance(answer, Exception):
            raise answer
        return s.agent.open(answer)[0]

    monkeypatch.setattr(droid_mcp.subprocess, "Popen", popen)
    monkeypatch.setattr(droid_mcp.socket, "create_connection", connect)
    yield s
    s.backend._close()


def test_start_pushes_the_jar_and_runs_it_under_adb_shell_with_a_token(starting):
    starting.connections = ["ours"]

    starting.backend._call("hello")

    assert ("push", str(droid_mcp.AGENT_JAR), droid_mcp.AGENT_REMOTE) in starting.device.calls
    assert ("forward", "tcp:0", "localabstract:droid-agent") in starting.device.calls
    (shell,) = starting.shells
    assert shell.cmd == [
        "adb",
        "-s",
        "emulator-5554",
        "shell",
        f"CLASSPATH={droid_mcp.AGENT_REMOTE} exec app_process / droid.agent.Server ours",
    ]
    assert shell.stdin == subprocess.PIPE  # the agent exits when this closes


def test_start_waits_out_an_older_agent_still_holding_the_socket(starting):
    starting.connections = [ConnectionRefusedError(), "older", "ours"]
    starting.agent.show(HOME)

    xml = starting.backend.ui_xml()

    assert starting.connections == []
    assert "Network" in xml


def test_start_reports_what_an_agent_that_died_printed(starting):
    starting.shell.update(exit_with=1, output=b"java.lang.IllegalStateException: UiAutomation already registered")

    with pytest.raises(RuntimeError, match="exited on start.*already registered"):
        starting.backend.ui_xml()


def test_start_gives_up_on_an_agent_that_never_answers_and_stops_it(starting):
    with pytest.raises(RuntimeError, match="did not start"):
        starting.backend.ui_xml()

    assert starting.shells[0].returncode == -9


def test_start_without_a_built_jar_says_how_to_build_it(starting, monkeypatch, tmp_path):
    monkeypatch.setattr(droid_mcp, "AGENT_JAR", tmp_path / "missing.jar")

    with pytest.raises(RuntimeError, match="agent/build.sh"):
        starting.backend.ui_xml()

    assert starting.shells == []
