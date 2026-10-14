"""Stand-ins for a device, so the suite never needs adb or a running emulator."""

import functools
import io
from xml.sax.saxutils import quoteattr

from PIL import Image as PILImage


def node(*children, text="", cls="android.widget.TextView", bounds="[0,0][100,100]", **attrs):
    """One uiautomator <node>. Keyword names use _ where the XML attribute uses -; booleans become true/false."""
    attrs = {"text": text, "class": cls, "bounds": bounds, **{k.replace("_", "-"): v for k, v in attrs.items()}}
    rendered = " ".join(f"{k}={quoteattr(str(v).lower() if isinstance(v, bool) else str(v))}" for k, v in attrs.items())
    return f"<node {rendered}>{''.join(children)}</node>"


def hierarchy(*nodes):
    """A full `uiautomator dump` document, including the XML declaration real dumps start with."""
    declaration = "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
    return f'{declaration}<hierarchy rotation="0">{"".join(nodes)}</hierarchy>'


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

    def __init__(self):
        self.calls = []
        self.size = (1080, 2400)
        self.activity = "com.android.settings/.Settings"
        self.show(hierarchy())

    def show(self, *screens):
        """Serve these UI dumps in order; the last one stays on screen."""
        self._screens = list(screens)

    def adb(self, *args, binary=False, timeout=20):
        self.calls.append(args)
        if args == ("exec-out", "cat", "/sdcard/ui.xml"):
            return self._screens.pop(0) if len(self._screens) > 1 else self._screens[0]
        if args == ("exec-out", "screencap", "-p"):
            return png(*self.size)
        if args == ("shell", "wm", "size"):
            return f"Physical size: {self.size[0]}x{self.size[1]}\n"
        if args[:2] == ("shell", "dumpsys"):
            return f"  mResumedActivity: ActivityRecord{{1a2b3c u0 {self.activity} t42}}\n"
        return b"" if binary else ""

    @property
    def inputs(self):
        """Every `adb shell input ...` sent so far, without the `shell input` prefix."""
        return [c[2:] for c in self.calls if c[:2] == ("shell", "input")]

    @property
    def dumps(self):
        return self.calls.count(("shell", "uiautomator", "dump", "/sdcard/ui.xml"))
