"""
droid-mcp — sketch of an agent-oriented Android MCP server.

Design goals (vs. generic mobile MCPs):
  1. Every action returns the NEW observation in the same call
     (one round-trip per step instead of act -> screenshot -> dump).
  2. Compact, indexed UI tree: Claude taps `[12]`, not pixel guesses.
  3. Screenshots downscaled to cap image-token cost; optional per call.
  4. Waits for the UI to settle before observing (no stale frames).
  5. Emulator snapshot save/load for deterministic resets between runs.
  6. Every step appended to a JSONL trajectory log (replay / eval).
  7. Backend class so an iOS (simctl + idb) backend can slot in later.

Run:   uv sync
       claude mcp add droid -- uv run --directory /path/to/this/repo droid-mcp
Env:   ANDROID_SERIAL=emulator-5554  SHOT_MAX_W=540  TRAJ_LOG=traj.jsonl
"""

import hashlib
import io
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage

SERIAL = os.environ.get("ANDROID_SERIAL")
MAX_W = int(os.environ.get("SHOT_MAX_W", "540"))
TRAJ_LOG = os.environ.get("TRAJ_LOG")
BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


class AndroidBackend:
    def __init__(self, serial=None):
        self.serial = serial
        self.elements: dict[int, tuple[int, int]] = {}  # index -> center (device px)
        self.scale = 1.0  # device px per screenshot px
        self._size = None

    # ---------- low level ----------
    def adb(self, *args, binary=False, timeout=20):
        cmd = ["adb"] + (["-s", self.serial] if self.serial else []) + list(args)
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="ignore").strip())
        return r.stdout if binary else r.stdout.decode(errors="ignore")

    def size(self):
        if not self._size:
            m = re.search(r"(\d+)x(\d+)", self.adb("shell", "wm", "size"))
            self._size = (int(m[1]), int(m[2]))
        return self._size

    # ---------- observation ----------
    def ui_xml(self):
        self.adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
        return self.adb("exec-out", "cat", "/sdcard/ui.xml")

    def settle(self, timeout=5.0):
        """Dump until two consecutive trees match (crude idle detection)."""
        prev, xml, end = None, "", time.time() + timeout
        while time.time() < end:
            xml = self.ui_xml()
            h = hashlib.md5(xml.encode()).hexdigest()
            if h == prev:
                break
            prev = h
            time.sleep(0.3)
        return xml

    def compact_tree(self, xml):
        self.elements.clear()
        lines = []
        for n in ET.fromstring(xml).iter("node"):
            text = (n.get("text") or n.get("content-desc") or "").replace("\n", " ")
            cls = n.get("class", "").split(".")[-1]
            actionable = (
                any(n.get(k) == "true" for k in ("clickable", "long-clickable", "scrollable")) or cls == "EditText"
            )
            if not (actionable or text):
                continue
            m = BOUNDS.match(n.get("bounds", ""))
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            if x2 <= x1 or y2 <= y1:
                continue
            i = len(self.elements)
            self.elements[i] = ((x1 + x2) // 2, (y1 + y2) // 2)
            rid = (n.get("resource-id") or "").split("/")[-1]
            flags = [
                f
                for f, k in (
                    ("tap", "clickable"),
                    ("scroll", "scrollable"),
                    ("checked", "checked"),
                    ("focused", "focused"),
                )
                if n.get(k) == "true"
            ]
            lines.append(
                f'[{i}] {cls} "{text[:60]}"' + (f" id={rid}" if rid else "") + (f" {','.join(flags)}" if flags else "")
            )
        return "\n".join(lines) or "(empty tree — maybe a game/canvas; use screenshot + tap_xy)"

    def screenshot(self):
        png = self.adb("exec-out", "screencap", "-p", binary=True)
        im = PILImage.open(io.BytesIO(png))
        self.scale = 1.0
        if im.width > MAX_W:
            self.scale = im.width / MAX_W
            im = im.resize((MAX_W, round(im.height / self.scale)))
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    def current_activity(self):
        out = self.adb("shell", "dumpsys", "activity", "activities")
        m = re.search(r"mResumedActivity.*? (\S+/\S+)", out) or re.search(r"topResumedActivity.*? (\S+/\S+)", out)
        return m[1] if m else "?"

    # ---------- actions ----------
    def tap(self, x, y):
        self.adb("shell", "input", "tap", str(x), str(y))

    def swipe(self, x1, y1, x2, y2, ms=300):
        self.adb("shell", "input", "swipe", *map(str, (x1, y1, x2, y2, ms)))

    def text(self, s):
        # `input text` can't handle many special chars; swap for an IME/ADBKeyboard later.
        self.adb("shell", "input", "text", s.replace(" ", "%s").replace("'", "\\'"))

    def key(self, name):
        codes = {"back": 4, "home": 3, "enter": 66, "recents": 187, "delete": 67}
        self.adb("shell", "input", "keyevent", str(codes[name]))

    def launch(self, package):
        self.adb("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")

    def snapshot(self, op, name):
        self.adb("emu", "avd", "snapshot", op, name, timeout=120)


dev = AndroidBackend(SERIAL)
mcp = FastMCP("droid")


def _log(action, args, tree):
    if TRAJ_LOG:
        with open(TRAJ_LOG, "a") as f:
            f.write(
                json.dumps(
                    {
                        "t": time.time(),
                        "action": action,
                        "args": args,
                        "tree_hash": hashlib.md5(tree.encode()).hexdigest(),
                    }
                )
                + "\n"
            )


def _observe(action="observe", args=None, screenshot=True):
    tree = dev.compact_tree(dev.settle())
    _log(action, args or {}, tree)
    header = f"activity: {dev.current_activity()}\n"
    out = [header + tree]
    if screenshot:
        out.append(Image(data=dev.screenshot(), format="png"))
    return out


@mcp.tool()
def observe(screenshot: bool = True):
    """Current screen: foreground activity, indexed UI elements, optional screenshot."""
    return _observe(screenshot=screenshot)


@mcp.tool()
def tap(element: int, screenshot: bool = True):
    """Tap UI element by its [index] from the last observation. Returns the new screen."""
    dev.tap(*dev.elements[element])
    return _observe("tap", {"element": element}, screenshot)


@mcp.tool()
def tap_xy(x: int, y: int, screenshot: bool = True):
    """Tap at coordinates in SCREENSHOT pixels (for canvas/games with no UI tree)."""
    dev.tap(round(x * dev.scale), round(y * dev.scale))
    return _observe("tap_xy", {"x": x, "y": y}, screenshot)


@mcp.tool()
def type_text(text: str, element: int | None = None, submit: bool = False, screenshot: bool = True):
    """Optionally focus an element, type text, optionally press enter."""
    if element is not None:
        dev.tap(*dev.elements[element])
    dev.text(text)
    if submit:
        dev.key("enter")
    return _observe("type_text", {"text": text, "element": element, "submit": submit}, screenshot)


@mcp.tool()
def swipe(direction: str, screenshot: bool = True):
    """Scroll content: direction is up | down | left | right (content moves that way's opposite)."""
    w, h = dev.size()
    cx, cy = w // 2, h // 2
    dx, dy = {"up": (0, h // 3), "down": (0, -h // 3), "left": (w // 3, 0), "right": (-w // 3, 0)}[direction]
    dev.swipe(cx - dx // 2, cy - dy // 2, cx + dx // 2, cy + dy // 2)
    return _observe("swipe", {"direction": direction}, screenshot)


@mcp.tool()
def press(key: str, screenshot: bool = True):
    """System key: back | home | enter | recents | delete."""
    dev.key(key)
    return _observe("press", {"key": key}, screenshot)


@mcp.tool()
def launch_app(package: str, screenshot: bool = True):
    """Launch an app by package name, e.g. com.android.settings."""
    dev.launch(package)
    return _observe("launch_app", {"package": package}, screenshot)


@mcp.tool()
def snapshot(op: str, name: str):
    """Emulator snapshot: op = save | load. Use load to reset to a known state between tasks."""
    dev.snapshot(op, name)
    return f"snapshot {op} '{name}' ok"


if __name__ == "__main__":
    mcp.run()
