"""Stand-ins for a device, so the suite never needs adb or a running emulator."""

import functools
import io
import json
import os
import socket
import struct
import subprocess
import threading
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


COLOUR = (200, 30, 60)  # every fake screen is filled with this, so a decoder that mixes up channels shows


@functools.lru_cache
def png(width, height):
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), COLOUR).save(buf, "PNG")
    return buf.getvalue()


@functools.lru_cache
def raw_frame(width, height, pixel_format=1, header=16):
    """What `screencap` without -p writes: width, height, pixel format (1 = RGBA_8888), then on Android 10+ a
    colour space, then the pixels."""
    fields = (width, height, pixel_format, 0)[: header // 4]
    bpp = 4 if pixel_format in (1, 2) else 2
    pixel = bytes([*COLOUR, 255]) if bpp == 4 else b"\x00\x00"
    return struct.pack(f"<{len(fields)}I", *fields) + pixel * (width * height)


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
        self.pixel_format = 1
        self.raw_header = 16
        self.activity = "com.android.settings/.Settings"
        self.emu_reply = "OK\n"
        self.forwards = ""  # what `adb forward --list` prints
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
        if args == ("exec-out", "screencap"):
            return raw_frame(*self.size, self.pixel_format, self.raw_header)
        if args == ("shell", "wm", "size"):
            return f"Physical size: {self.size[0]}x{self.size[1]}\n"
        if args[:2] == ("shell", "dumpsys"):
            return f"  mResumedActivity: ActivityRecord{{1a2b3c u0 {self.activity} t42}}\n"
        if args[:3] == ("shell", "input", "text"):
            self.pieces.append(input_text_types(self._device_shell(args[1:])[1]))
        if args[:1] == ("emu",):
            return self.emu_reply
        if args == ("forward", "--list"):
            return self.forwards
        if args[:2] == ("forward", "tcp:0"):
            return "40123\n"  # adb picks a free host port and prints it
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


@functools.lru_cache
def jpeg(width, height):
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), COLOUR).save(buf, "JPEG")
    return buf.getvalue()


class FakeAgent:
    """Speaks droid-agent's protocol from the far end of a socket pair, answering from canned state and recording
    each request in order. `open` stands in for AgentBackend._open, so every call starts a fresh agent."""

    READS = {"hello", "dump", "screenshot", "activity"}

    def __init__(self):
        self.requests = []
        self.opened = 0
        self.activity = "com.android.settings/.Settings"
        self.shot = (jpeg(540, 1200), 2.0)  # what a 1080 px wide screen comes back as
        self.errors = {}  # cmd -> the error the agent reports for it
        self.drops = []  # cmds on which the connection drops instead of answering, once each
        self.show(hierarchy())

    def show(self, *screens):
        """Serve these UI dumps in order; the last one stays on screen."""
        self._screens = list(screens)

    def open(self, token=""):
        self.opened += 1
        ours, theirs = socket.socketpair()
        threading.Thread(target=self._serve, args=(theirs, token), daemon=True).start()
        return ours, ours.makefile("rb")

    @property
    def actions(self):
        """Every request that is not a read, as (cmd, args)."""
        return [
            (r["cmd"], {k: v for k, v in r.items() if k != "cmd"}) for r in self.requests if r["cmd"] not in self.READS
        ]

    def _serve(self, sock, token):
        with sock, sock.makefile("rb") as requests:
            for line in requests:
                request = json.loads(line)
                self.requests.append(request)
                cmd = request["cmd"]
                if cmd in self.drops:
                    self.drops.remove(cmd)
                    return
                reply, payload = self._answer(request, token)
                if payload:
                    reply["len"] = len(payload)
                data = json.dumps(reply).encode() + b"\n" + payload
                for i in range(0, len(data), 4096):  # in pieces, the way a socket delivers a large reply
                    sock.sendall(data[i : i + 4096])

    def _answer(self, request, token):
        cmd = request["cmd"]
        if cmd in self.errors:
            return {"ok": False, "error": self.errors[cmd]}, b""
        if cmd == "hello":
            return {"ok": True, "token": token}, b""
        if cmd == "dump":
            xml = self._screens.pop(0) if len(self._screens) > 1 else self._screens[0]
            return {"ok": True, "settled": True, "activity": self.activity}, xml.encode()
        if cmd == "screenshot":
            return {"ok": True, "scale": self.shot[1]}, self.shot[0]
        return {"ok": True}, b""
