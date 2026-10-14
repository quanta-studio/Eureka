"""Stand-ins for a device, so the suite never needs adb or a running emulator."""

import functools
import io
import os
import subprocess
from xml.sax.saxutils import quoteattr

from PIL import Image as PILImage


def node(*children, text="", cls="android.widget.TextView", bounds="[0,0][100,100]", **attrs):
    """One uiautomator <node>. Keyword names use _ where the XML attribute uses -; booleans become true/false."""
    attrs = {"text": text, "class": cls, "bounds": bounds, **{k.replace("_", "-"): v for k, v in attrs.items()}}
    rendered = " ".join(f"{k}={quoteattr(str(v).lower() if isinstance(v, bool) else str(v))}" for k, v in attrs.items())
    return f"<node {rendered}>{''.join(children)}</node>"


DECLARATION = "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"  # real dumps start with this


def hierarchy(*nodes):
    """A single-window dump, which is what a uiautomator too old for --windows produces."""
    return f'{DECLARATION}<hierarchy rotation="0">{"".join(nodes)}</hierarchy>'


def window(*nodes, bounds, layer=0, kind="TYPE_APPLICATION", active=False):
    """One window of a `--windows` dump: `bounds` is its touchable area, and higher layers sit on top."""
    attrs = f'bounds="{bounds}" active="{str(active).lower()}" layer="{layer}" type="{kind}"'
    return f'<window index="0" title="" {attrs}><hierarchy rotation="0">{"".join(nodes)}</hierarchy></window>'


def windows(*stack):
    return f'{DECLARATION}<displays><display id="0">{"".join(stack)}</display></displays>'


def input_text_types(arg):
    """What `input text <arg>` types. Ported from Android's InputShellCommand: %s becomes a space."""
    buff, escape, i = list(arg), False, 0
    while i < len(buff):
        if escape:
            escape = False
            if buff[i] == "s":
                buff[i] = " "
                i -= 1
                del buff[i]
        if buff[i] == "%":
            escape = True
        i += 1
    return "".join(buff)


@functools.lru_cache
def png(width, height):
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height)).save(buf, "PNG")
    return buf.getvalue()


class FakeClock:
    """Replaces time.time and time.sleep: sleeping moves the clock forward instead of waiting."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeDevice:
    """Answers the adb commands AndroidBackend issues and records them in order."""

    def __init__(self, scratch):
        self.calls = []
        self.size = (1080, 2400)
        self.activity = "com.android.settings/.Settings"
        self.emu_reply = "OK\n"
        self.pieces = []  # what each `input text` call typed, as the focused field would receive it
        self._argv = scratch / "argv"
        self._cwd = scratch / "device"
        self._cwd.mkdir()
        self.show(hierarchy())

    def show(self, *screens):
        """Serve these UI dumps in order; the last one stays on screen. "" is a dump that produced nothing."""
        self._screens = list(screens)

    def adb(self, *args, binary=False, timeout=20):
        self.calls.append(args)
        if self._is_dump(args):
            return self._screens.pop(0) if len(self._screens) > 1 else self._screens[0]
        if args == ("exec-out", "screencap", "-p"):
            return png(*self.size)
        if args == ("shell", "wm", "size"):
            return f"Physical size: {self.size[0]}x{self.size[1]}\n"
        if args[:2] == ("shell", "dumpsys"):
            return f"  mResumedActivity: ActivityRecord{{1a2b3c u0 {self.activity} t42}}\n"
        if args[:3] == ("shell", "input", "text"):
            self.pieces.append(input_text_types(self._device_shell(args[1:])[1]))
        if args[:1] == ("emu",):
            return self.emu_reply
        return b"" if binary else ""

    def _device_shell(self, words):
        """Run an `adb shell` command the way a device does: adb joins the words with spaces and a POSIX shell
        parses the result. Returns the arguments `input` ended up with."""
        script = 'input() { printf \'%s\\0\' "$@" > "$ARGV"; }\n' + " ".join(words)
        self._argv.unlink(missing_ok=True)
        env = {"ARGV": str(self._argv), "PATH": os.defpath}
        r = subprocess.run(["sh", "-c", script], cwd=self._cwd, env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        return self._argv.read_text().split("\0")[:-1]

    @property
    def typed(self):
        return "".join(self.pieces)

    @property
    def side_effects(self):
        """Files the device shell created while running our commands; typing text must never produce any."""
        return sorted(p.name for p in self._cwd.iterdir())

    @property
    def inputs(self):
        """Every `adb shell input ...` sent so far, without the `shell input` prefix."""
        return [c[2:] for c in self.calls if c[:2] == ("shell", "input")]

    @staticmethod
    def _is_dump(args):
        return args[0] == "exec-out" and "uiautomator dump" in args[1]

    @property
    def dumps(self):
        return sum(self._is_dump(c) for c in self.calls)
