import io
import subprocess

import pytest
from PIL import Image as PILImage

import droid_mcp
from tests.fakes import hierarchy, node


@pytest.fixture
def run(monkeypatch):
    """Replace subprocess.run. Yields the commands it was asked to execute and the result it hands back."""
    commands = []
    result = subprocess.CompletedProcess([], 0, stdout=b"ok\n", stderr=b"")

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return result

    monkeypatch.setattr(droid_mcp.subprocess, "run", fake_run)
    return commands, result


def test_adb_targets_the_configured_serial(run):
    commands, _ = run

    droid_mcp.AndroidBackend("emulator-5554").adb("shell", "wm", "size")

    assert commands == [["adb", "-s", "emulator-5554", "shell", "wm", "size"]]


def test_adb_leaves_device_selection_to_adb_when_no_serial_is_given(run):
    commands, _ = run

    droid_mcp.AndroidBackend().adb("shell", "wm", "size")

    assert commands == [["adb", "shell", "wm", "size"]]


def test_adb_returns_text_unless_binary_is_requested(run):
    backend = droid_mcp.AndroidBackend()

    assert backend.adb("shell", "echo") == "ok\n"
    assert backend.adb("exec-out", "screencap", "-p", binary=True) == b"ok\n"


def test_adb_failure_raises_with_the_stderr_message(run):
    _, result = run
    result.returncode = 1
    result.stderr = b"error: device offline\n"

    with pytest.raises(RuntimeError, match="device offline"):
        droid_mcp.AndroidBackend().adb("shell", "wm", "size")


def test_size_is_parsed_once_and_cached(device):
    device.size = (720, 1600)

    assert device.backend.size() == (720, 1600)
    assert device.backend.size() == (720, 1600)
    assert device.calls.count(("shell", "wm", "size")) == 1


def test_settle_returns_as_soon_as_two_consecutive_dumps_match(device):
    loading, loaded = hierarchy(node(text="Loading")), hierarchy(node(text="Loaded"))
    device.show(loading, loaded, loaded, hierarchy(node(text="Later")))

    assert device.backend.settle() == loaded
    assert device.dumps == 3


def test_settle_gives_up_at_the_timeout_and_returns_the_latest_dump(device):
    frames = [hierarchy(node(text=f"frame {i}")) for i in range(100)]
    device.show(*frames)

    xml = device.backend.settle(timeout=5.0)

    assert xml == frames[device.dumps - 1]
    assert 1 < device.dumps < len(frames)


def test_screenshot_is_downscaled_to_the_width_cap_and_records_the_scale(device):
    device.size = (1080, 2400)

    shot = PILImage.open(io.BytesIO(device.backend.screenshot()))

    assert shot.format == "PNG"
    assert shot.size == (540, 1200)
    assert device.backend.scale == 2.0


def test_screenshot_narrower_than_the_cap_is_left_alone(device):
    device.size = (1080, 2400)
    device.backend.screenshot()
    device.size = (480, 800)

    shot = PILImage.open(io.BytesIO(device.backend.screenshot()))

    assert shot.size == (480, 800)
    assert device.backend.scale == 1.0


@pytest.mark.parametrize(
    "dumpsys",
    [
        "    mResumedActivity: ActivityRecord{c4b2e1f u0 com.android.settings/.Settings t12}\n",
        "  topResumedActivity=ActivityRecord{9d1a7b0 u0 com.android.settings/.Settings t12}\n",
    ],
)
def test_current_activity_is_read_from_dumpsys(device, monkeypatch, dumpsys):
    monkeypatch.setattr(device.backend, "adb", lambda *args, **kwargs: f"ACTIVITY MANAGER ACTIVITIES\n{dumpsys}")

    assert device.backend.current_activity() == "com.android.settings/.Settings"


def test_current_activity_is_a_placeholder_when_nothing_is_resumed(device, monkeypatch):
    monkeypatch.setattr(device.backend, "adb", lambda *args, **kwargs: "ACTIVITY MANAGER ACTIVITIES\n")

    assert device.backend.current_activity() == "?"


def test_text_encodes_spaces_the_way_adb_input_expects(device):
    device.backend.text("hello big world")

    assert device.inputs == [("text", "hello%sbig%sworld")]


@pytest.mark.parametrize(
    ("name", "keycode"), [("back", "4"), ("home", "3"), ("enter", "66"), ("recents", "187"), ("delete", "67")]
)
def test_key_names_map_to_android_keycodes(device, name, keycode):
    device.backend.key(name)

    assert device.inputs == [("keyevent", keycode)]
