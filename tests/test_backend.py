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


def test_adb_failure_without_any_output_still_says_what_failed(run):
    _, result = run
    result.returncode = 1
    result.stdout = result.stderr = b""

    with pytest.raises(RuntimeError, match="adb shell wm size.*exit code 1"):
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


def test_settle_keeps_polling_while_the_device_produces_no_tree(device):
    loaded = hierarchy(node(text="Loaded"))
    device.show("", "", loaded)

    assert device.backend.settle() == loaded


def test_settle_reports_a_device_that_never_produces_a_tree(device):
    device.show("")

    with pytest.raises(RuntimeError, match="no UI tree.*screen"):
        device.backend.settle()


def test_dump_cannot_return_a_tree_left_over_from_an_earlier_dump(device):
    device.backend.ui_xml()

    script = device.calls[-1][1]
    assert script.index("rm -f") < script.index("uiautomator dump") < script.index("cat ")


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
    device.backend.text("a b c")

    assert device.inputs == [("text", "a%sb%sc")]
    assert device.typed == "a b c"


@pytest.mark.parametrize(
    "text",
    [
        "x;>injected",
        "a && touch injected",
        "a | tee injected",
        "$(touch injected)",
        "`touch injected`",
        'it\'s 50% & "ok" (a|b) <c> $HOME',
        "back\\slash and 'single' quotes",
        "glob * ? [a-z] ~ #hash !bang",
        "two  spaces  and a trailing one ",
        "-n --flag",
        "100%sure",
        "%s",
        "a%%sb % s",
    ],
)
def test_text_reaches_the_field_verbatim_and_never_runs_as_a_command(device, text):
    device.backend.text(text)

    assert device.typed == text
    assert device.side_effects == []


def test_long_text_is_typed_in_pieces_short_enough_for_a_slow_device_to_keep_up(device):
    text = "the quick brown fox jumps over the lazy dog 0123456789"

    device.backend.text(text)

    assert device.typed == text
    assert max(map(len, device.pieces)) <= droid_mcp.TYPE_CHUNK
    assert len(device.pieces) == -(-len(text) // droid_mcp.TYPE_CHUNK)


def test_text_that_adb_cannot_type_is_refused_before_anything_is_sent(device):
    with pytest.raises(ValueError, match="printable ASCII.*'é你'"):
        device.backend.text("café 你")

    assert device.inputs == []


def test_empty_text_sends_nothing(device):
    device.backend.text("")

    assert device.inputs == []


@pytest.mark.parametrize(
    ("name", "keycode"), [("back", "4"), ("home", "3"), ("enter", "66"), ("recents", "187"), ("delete", "67")]
)
def test_key_names_map_to_android_keycodes(device, name, keycode):
    device.backend.key(name)

    assert device.inputs == [("keyevent", keycode)]


def test_unknown_key_is_refused_with_the_valid_names(device):
    with pytest.raises(ValueError, match="volume_up.*back, home, enter, recents, delete"):
        device.backend.key("volume_up")

    assert device.inputs == []


def test_snapshot_load_waits_for_the_device_to_reconnect(device):
    device.backend.snapshot("load", "clean")

    assert device.calls[:3] == [("emu", "avd", "snapshot", "load", "clean"), ("wait-for-device",), ("shell", "true")]


def test_snapshot_load_keeps_waiting_while_the_device_drops_out_again(device, monkeypatch):
    answers = iter([RuntimeError("adb: device 'emulator-5554' not found"), ""])

    def flaky(*args, **kwargs):
        device.calls.append(args)
        if args == ("shell", "true"):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
        return "OK\n" if args[:1] == ("emu",) else ""

    monkeypatch.setattr(device.backend, "adb", flaky)

    device.backend.snapshot("load", "clean")

    assert device.calls.count(("wait-for-device",)) == 2


def test_snapshot_load_invalidates_the_previous_observation(device):
    device.backend.compact_tree(hierarchy(node(text="A")))

    device.backend.snapshot("load", "clean")

    assert device.backend.elements == {}


def test_snapshot_save_does_not_wait(device):
    device.backend.snapshot("save", "clean")

    assert device.calls == [("emu", "avd", "snapshot", "save", "clean")]


def test_snapshot_reports_the_emulator_consoles_failure(device):
    device.emu_reply = (
        "KO: Device 'encrypt' does not have the requested snapshot 'x'\n"
        "KO: Snapshot load failure: snapshot doesn't exist\n"
    )

    with pytest.raises(RuntimeError, match="snapshot doesn't exist"):
        device.backend.snapshot("load", "x")

    assert ("wait-for-device",) not in device.calls


def test_snapshot_on_a_device_adb_cannot_snapshot_explains_that_it_needs_an_emulator(run):
    _, result = run
    result.returncode = 1
    result.stdout = result.stderr = b""

    with pytest.raises(RuntimeError, match="emulator"):
        droid_mcp.AndroidBackend("R8YY80SJYZF").snapshot("save", "clean")


def test_snapshot_refuses_an_unknown_operation(device):
    with pytest.raises(ValueError, match="delete.*save, load"):
        device.backend.snapshot("delete", "clean")

    assert device.calls == []
