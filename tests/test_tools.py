import asyncio
import hashlib
import io
import json

import pytest
from PIL import Image as PILImage

import droid_mcp
from tests.fakes import hierarchy, node, window, windows

HOME = hierarchy(
    node(text="Network", clickable=True, bounds="[0,200][1080,400]"),
    node(cls="android.widget.EditText", resource_id="com.android.settings:id/search", bounds="[0,0][1080,100]"),
)
DETAIL = hierarchy(node(text="Wi-Fi", clickable=True, bounds="[0,200][1080,400]"))


def test_server_exposes_the_expected_tools():
    tools = asyncio.run(droid_mcp.mcp.list_tools())

    assert {t.name for t in tools} == {
        "observe",
        "tap",
        "tap_xy",
        "type_text",
        "swipe",
        "press",
        "launch_app",
        "snapshot",
    }


def test_observe_returns_activity_tree_and_a_downscaled_screenshot(device):
    device.show(HOME)

    text, image = droid_mcp.observe()

    assert text.splitlines() == [
        "activity: com.android.settings/.Settings",
        '[0] TextView "Network" tap',
        '[1] EditText "" id=search',
    ]
    assert isinstance(image, droid_mcp.Image)
    assert image.to_image_content().mimeType == "image/jpeg"
    assert PILImage.open(io.BytesIO(image.data)).size == (540, 1200)


def test_screenshot_can_be_turned_off_per_call(device):
    device.show(HOME)

    out = droid_mcp.observe(screenshot=False)

    assert len(out) == 1
    assert not any(c[:2] == ("exec-out", "screencap") for c in device.calls)


def test_tap_hits_the_indexed_element_and_returns_the_resulting_screen(device):
    device.show(HOME)
    droid_mcp.observe()
    device.show(DETAIL)

    text, _ = droid_mcp.tap(0)

    assert device.inputs == [("tap", "540", "300")]
    assert '[0] TextView "Wi-Fi" tap' in text
    assert "Network" not in text


def test_tap_xy_converts_screenshot_pixels_to_device_pixels(device):
    device.size = (1080, 2400)
    droid_mcp.observe()

    droid_mcp.tap_xy(100, 250)

    assert device.inputs == [("tap", "200", "500")]


def test_type_text_focuses_the_element_types_then_submits(device):
    device.show(HOME)
    droid_mcp.observe()

    droid_mcp.type_text("wi fi", element=1, submit=True)

    assert device.inputs == [("tap", "540", "50"), ("text", "wi%sfi"), ("keyevent", "66")]


def test_type_text_alone_only_types(device):
    droid_mcp.type_text("hello")

    assert device.inputs == [("text", "hello")]


@pytest.mark.parametrize(
    ("direction", "finger_moves"),
    [("down", (0, -1)), ("up", (0, 1)), ("right", (-1, 0)), ("left", (1, 0))],
)
def test_swipe_drags_the_finger_opposite_to_the_scroll_direction(device, direction, finger_moves):
    device.size = (1080, 2400)

    droid_mcp.swipe(direction)

    (_, x1, y1, x2, y2, _), *rest = device.inputs
    dx, dy = int(x2) - int(x1), int(y2) - int(y1)
    assert rest == []
    assert ((dx > 0) - (dx < 0), (dy > 0) - (dy < 0)) == finger_moves
    # The gesture is centred on the screen.
    assert (int(x1) + int(x2), int(y1) + int(y2)) == (1080, 2400)


def test_press_sends_the_key_and_returns_the_new_screen(device):
    device.show(DETAIL)

    text, _ = droid_mcp.press("back")

    assert device.inputs == [("keyevent", "4")]
    assert '[0] TextView "Wi-Fi" tap' in text


def test_launch_app_starts_the_package_and_returns_its_screen(device):
    device.show(DETAIL)

    text, _ = droid_mcp.launch_app("com.android.settings")

    launches = [c for c in device.calls if c[:2] == ("shell", "monkey")]
    assert launches == [
        ("shell", "monkey", "-p", "com.android.settings", "-c", "android.intent.category.LAUNCHER", "1")
    ]
    assert '[0] TextView "Wi-Fi" tap' in text


def test_snapshot_reports_the_operation_without_observing(device):
    out = droid_mcp.snapshot("save", "clean")

    assert device.calls == [("emu", "avd", "snapshot", "save", "clean")]
    assert "save" in out and "clean" in out


def test_every_step_appends_one_line_to_the_trajectory_log(device, monkeypatch, tmp_path):
    log = tmp_path / "traj.jsonl"
    monkeypatch.setattr(droid_mcp, "TRAJ_LOG", str(log))
    device.show(HOME)
    droid_mcp.observe()
    device.show(DETAIL)

    text, _ = droid_mcp.tap(0)

    first, second = (json.loads(line) for line in log.read_text().splitlines())
    assert (first["action"], first["args"]) == ("observe", {})
    assert (second["action"], second["args"]) == ("tap", {"element": 0})
    tree = text.split("\n", 1)[1]
    assert second["tree_hash"] == hashlib.md5(tree.encode()).hexdigest()
    assert first["tree_hash"] != second["tree_hash"]


def test_nothing_is_logged_when_no_trajectory_log_is_configured(device, tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    droid_mcp.observe()

    assert list(cwd.iterdir()) == []


# --- acting on what is really reachable -----------------------------------------------------------------------------

STATUS_BAR = window(node(text="10:04", bounds="[20,10][120,60]"), bounds="[0,0][1080,142]", layer=2, kind="TYPE_SYSTEM")
KEYBOARD = window(
    node(text="q", clickable=True, bounds="[0,1600][108,1750]"),
    bounds="[0,1541][1080,2400]",
    layer=1,
    kind="TYPE_INPUT_METHOD",
)
SEARCH = windows(
    STATUS_BAR,
    KEYBOARD,
    window(
        node(text="Results", bounds="[0,200][1080,300]"),
        node(cls="android.widget.Button", text="Send", clickable=True, bounds="[800,1800][1000,1900]"),
        bounds="[0,0][1080,2400]",
        active=True,
    ),
)


def test_observation_says_when_the_keyboard_is_showing(device):
    device.show(SEARCH)
    with_keyboard = droid_mcp.observe(screenshot=False)[0]
    device.show(HOME)
    without = droid_mcp.observe(screenshot=False)[0]

    assert with_keyboard.splitlines()[:2] == ["activity: com.android.settings/.Settings", "keyboard: showing"]
    assert "keyboard" not in without


@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
def test_swipe_stays_clear_of_the_keyboard_and_status_bar(device, direction):
    device.show(SEARCH)
    droid_mcp.observe(screenshot=False)

    droid_mcp.swipe(direction, screenshot=False)

    _, x1, y1, x2, y2, _ = device.inputs[0]
    assert all(142 <= int(y) < 1541 for y in (y1, y2))
    assert all(0 <= int(x) < 1080 for x in (x1, x2))
    assert (x1, y1) != (x2, y2)


def test_tapping_an_element_behind_the_keyboard_is_refused_with_the_reason(device):
    device.show(SEARCH)
    droid_mcp.observe(screenshot=False)

    with pytest.raises(ValueError, match=r"\[1\].*the keyboard"):
        droid_mcp.tap(1)

    assert device.inputs == []


def test_tapping_an_index_that_is_not_on_screen_names_the_valid_range(device):
    device.show(HOME)
    droid_mcp.observe(screenshot=False)

    with pytest.raises(ValueError, match=r"\[999\].*\[0\].*\[1\]"):
        droid_mcp.tap(999)

    assert device.inputs == []


def test_tapping_before_anything_was_observed_says_to_observe_first(device):
    with pytest.raises(ValueError, match="observe"):
        droid_mcp.tap(0)

    assert device.inputs == []


def test_type_text_into_an_unknown_element_types_nothing(device):
    device.show(HOME)
    droid_mcp.observe(screenshot=False)

    with pytest.raises(ValueError, match=r"\[7\]"):
        droid_mcp.type_text("hello", element=7)

    assert device.inputs == []


def test_unknown_swipe_direction_is_refused_with_the_valid_ones(device):
    with pytest.raises(ValueError, match="diagonal.*up, down, left, right"):
        droid_mcp.swipe("diagonal")

    assert device.inputs == []
