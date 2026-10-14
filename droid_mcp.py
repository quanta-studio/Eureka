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
       DROID_AGENT=1 drives the device through droid-agent (agent/build.sh) instead of uiautomator and input
"""

import contextlib
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import socket
import struct
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage

SERIAL = os.environ.get("ANDROID_SERIAL")
MAX_W = int(os.environ.get("SHOT_MAX_W", "540"))
TRAJ_LOG = os.environ.get("TRAJ_LOG")
AGENT = os.environ.get("DROID_AGENT") == "1"
AGENT_JAR = Path(__file__).with_name("agent") / "build" / "droid-agent.jar"  # built by agent/build.sh
AGENT_REMOTE = "/data/local/tmp/droid-agent.jar"
AGENT_SOCKET = "droid-agent"
AGENT_QUIET_MS = 500  # the UI counts as settled once no accessibility event has arrived for this long
AGENT_SETTLE_MS = 3000  # observe anyway after this long: a spinner or a video never goes quiet
BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
KEYS = {"back": 4, "home": 3, "enter": 66, "recents": 187, "delete": 67}
SWIPES = {"up": (0, 1), "down": (0, -1), "left": (1, 0), "right": (-1, 0)}  # finger travel, in thirds of the viewport
KEYBOARD = "the keyboard"
MIN_TAP = 16  # px; an exposed strip thinner than this is too close to whatever covers the rest
TYPE_CHUNK = 6  # chars per `input text` call; a slow device drops whatever one call has not typed within ~10 s


def _keycode(name):
    if name not in KEYS:
        raise ValueError(f"Unknown key {name!r}; use one of: {', '.join(KEYS)}")
    return KEYS[name]


def _rect(bounds):
    m = BOUNDS.match(bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None


def _centre(rect):
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


def _exposed(rect, covers):
    """The roomiest rectangle inside rect that none of the covers overlap, or None."""
    free = [rect]
    for cx1, cy1, cx2, cy2 in covers:
        pieces = []
        for x1, y1, x2, y2 in free:
            ix1, iy1, ix2, iy2 = max(x1, cx1), max(y1, cy1), min(x2, cx2), min(y2, cy2)
            if ix1 >= ix2 or iy1 >= iy2:
                pieces.append((x1, y1, x2, y2))
            else:
                pieces += [(x1, y1, x2, iy1), (x1, iy2, x2, y2), (x1, iy1, ix1, iy2), (ix2, iy1, x2, iy2)]
        free = [f for f in pieces if f[2] - f[0] >= MIN_TAP and f[3] - f[1] >= MIN_TAP]
    return max(free, key=lambda f: (min(f[2] - f[0], f[3] - f[1]), (f[2] - f[0]) * (f[3] - f[1])), default=None)


def _tap_point(rect, covers):
    """Where to tap rect so nothing on top of it takes the touch: (point, None), or (None, what covers it)."""
    x, y = _centre(rect)
    hit = next((what for (x1, y1, x2, y2), what in covers if x1 <= x < x2 and y1 <= y < y2), None)
    if hit is None:
        return (x, y), None
    free = _exposed(rect, [c for c, _ in covers])
    return (_centre(free), None) if free else (None, hit)


def _active_window(xml):
    """Split a dump into the active window's hierarchy, that window's bounds, and whatever windows sit above it."""
    root = ET.fromstring(xml)
    if root.tag == "hierarchy":  # uiautomator too old for --windows: it dumped the active window alone
        return root, None, []
    displays = root.findall("display")
    windows = next((d.findall("window") for d in displays if d.find("window[@active='true']") is not None), [])
    active = next((w for w in windows if w.get("active") == "true"), None)
    if active is None:
        return None, None, []
    above = [
        (_rect(w.get("bounds")), KEYBOARD if w.get("type") == "TYPE_INPUT_METHOD" else "another window")
        for w in windows
        if int(w.get("layer", 0)) > int(active.get("layer", 0))
    ]
    hierarchy = active.find("hierarchy")
    return (
        hierarchy if hierarchy is not None else ET.Element("hierarchy"),
        _rect(active.get("bounds")),
        [(r, what) for r, what in above if r],
    )


def _walk(hierarchy):
    """Nodes in drawing order, each as [node, rect, index of its last descendant, index of its clickable ancestor]."""
    order = []

    def visit(n, row):
        i = len(order)
        rect = _rect(n.get("bounds"))
        order.append([n, rect, i, row])
        inner = i if rect and n.get("clickable") == "true" else row
        for child in n.findall("node"):
            visit(child, inner)
        order[i][2] = len(order) - 1

    for top in hierarchy.findall("node"):
        visit(top, None)
    return order


class AndroidBackend:
    def __init__(self, serial=None):
        self.serial = serial
        self.elements: dict[int, tuple[int, int]] = {}  # index -> where to tap it (device px)
        self.covered: dict[int, str] = {}  # index -> what hides it, for listed elements with nowhere to tap
        self.viewport = None  # part of the active window no other window sits over (device px)
        self.keyboard = False
        self.scale = 1.0  # device px per screenshot px
        self._size = None

    # ---------- low level ----------
    def adb(self, *args, binary=False, timeout=20):
        cmd = ["adb"] + (["-s", self.serial] if self.serial else []) + list(args)
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout).decode(errors="ignore").strip()
            raise RuntimeError(detail or f"adb {' '.join(args)} failed with exit code {r.returncode} and no output")
        return r.stdout if binary else r.stdout.decode(errors="ignore")

    def size(self):
        if not self._size:
            m = re.search(r"(\d+)x(\d+)", self.adb("shell", "wm", "size"))
            self._size = (int(m[1]), int(m[2]))
        return self._size

    # ---------- observation ----------
    def ui_xml(self):
        """One UI dump, or "" if the device produced none."""
        # --windows adds the keyboard and system bars, which is how we know what sits on top of the app.
        # uiautomator can report success without writing anything (it does when the screen is off), so the
        # previous file is removed first: an empty answer is recoverable, a stale tree is not.
        f = "/sdcard/ui.xml"
        return self.adb(
            "exec-out", f"rm -f {f}; uiautomator dump --windows {f} >/dev/null 2>&1; cat {f} 2>/dev/null; rm -f {f}"
        )

    def settle(self, timeout=5.0):
        """One dump of the settled UI, retried while the device produces none."""
        # uiautomator waits for the UI to go idle before it dumps, so there is no second dump to compare against:
        # that only doubled the cost of every step.
        end = time.time() + timeout
        xml = self.ui_xml()
        while not xml and time.time() < end:
            time.sleep(0.3)
            xml = self.ui_xml()
        if not xml:
            raise RuntimeError("The device returned no UI tree; its screen is probably off or locked.")
        return xml

    def compact_tree(self, xml):
        self.elements.clear()
        self.covered.clear()
        hierarchy, bounds, above = _active_window(xml)
        self.keyboard = any(what == KEYBOARD for _, what in above)
        self.viewport = _exposed(bounds, [r for r, _ in above]) if bounds else None
        if hierarchy is None:
            return "(no active window — the screen may be off, locked or between apps; observe again)"
        order = _walk(hierarchy)
        clickable = [(i, rect) for i, (n, rect, _, _) in enumerate(order) if rect and n.get("clickable") == "true"]

        def tap_point(i):
            n, rect, last, _ = order[i]
            own = n.get("clickable") == "true"
            # Anything clickable drawn later takes the touch. A container that is not itself clickable has no
            # touch of its own to lose, so its children do not count against it.
            covers = above + [(r, "another control") for j, r in clickable if j > i and (own or j > last)]
            return _tap_point(rect, covers)

        lines = []
        for i, (n, rect, _, row) in enumerate(order):
            text = (n.get("text") or n.get("content-desc") or "").replace("\n", " ")
            cls = n.get("class", "").split(".")[-1]
            actionable = (
                any(n.get(k) == "true" for k in ("clickable", "long-clickable", "scrollable")) or cls == "EditText"
            )
            if not (actionable or text) or not rect:
                continue
            point, hidden_by = tap_point(i)
            if point is None and row is not None and n.get("clickable") != "true":
                point, hidden_by = tap_point(row)  # a covered label still belongs to its row: tap the row instead
            idx = len(lines)
            if point:
                self.elements[idx] = point
            else:
                self.covered[idx] = hidden_by
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
            ] + ([] if point else ["covered"])
            lines.append(
                f'[{idx}] {cls} "{text[:60]}"'
                + (f" id={rid}" if rid else "")
                + (f" {','.join(flags)}" if flags else "")
            )
        return "\n".join(lines) or "(empty tree — maybe a game/canvas; use screenshot + tap_xy)"

    def point(self, element):
        """Where to tap an element listed by the last observation."""
        if element in self.elements:
            return self.elements[element]
        if element in self.covered:
            raise ValueError(
                f"Element [{element}] is hidden behind {self.covered[element]}. "
                "Dismiss or scroll past what covers it, or use tap_xy."
            )
        count = len(self.elements) + len(self.covered)
        if not count:
            raise ValueError(f"No element [{element}]: the last observation listed none. Call observe first.")
        raise ValueError(f"No element [{element}]: the last observation listed [0] to [{count - 1}].")

    def screenshot(self):
        # Raw pixels skip the PNG encode on the device, which is most of a capture's cost.
        raw = self.adb("exec-out", "screencap", binary=True)
        w, h, fmt = struct.unpack_from("<3I", raw) if len(raw) >= 12 else (0, 0, 0)
        header = len(raw) - w * h * 4  # 12 bytes, or 16 from Android 10 on (a colour-space field)
        if fmt in (1, 2) and header in (12, 16):  # RGBA_8888 / RGBX_8888
            im = PILImage.frombuffer("RGBA", (w, h), raw[header:], "raw", "RGBA", 0, 1).convert("RGB")
        else:
            im = PILImage.open(io.BytesIO(self.adb("exec-out", "screencap", "-p", binary=True))).convert("RGB")
        self.scale = 1.0
        if im.width > MAX_W:
            self.scale = im.width / MAX_W
            im = im.resize((MAX_W, round(im.height / self.scale)))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)  # a fraction of the bytes of a PNG, and far cheaper to encode
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
        # `input text` types through the virtual keyboard's key map, which only covers ASCII;
        # anything else needs an IME or an on-device agent.
        untypable = "".join(sorted({c for c in s if not " " <= c <= "~"}))
        if untypable:
            raise ValueError(f"adb can only type printable ASCII, not {untypable!r}")
        # `input text` turns %s into a space, so a literal %s has to be split across two calls.
        for part in re.split(r"(?<=%)(?=s)", s):
            for chunk in (part[i : i + TYPE_CHUNK] for i in range(0, len(part), TYPE_CHUNK)):
                # adb hands the command line to the device's shell: quote it, or the text runs as shell code.
                self.adb("shell", "input", "text", shlex.quote(chunk.replace(" ", "%s")))

    def key(self, name):
        self.adb("shell", "input", "keyevent", str(_keycode(name)))

    def launch(self, package):
        self.adb("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")

    def snapshot(self, op, name):
        if op not in ("save", "load"):
            raise ValueError(f"Unknown snapshot op {op!r}; use one of: save, load")
        try:
            out = self.adb("emu", "avd", "snapshot", op, name, timeout=120)
        except RuntimeError as e:
            raise RuntimeError(f"Snapshots need an emulator, and adb could not reach one here: {e}") from None
        # The emulator console reports failure in its output, not its exit code.
        failures = [line[3:].strip() for line in out.splitlines() if line.startswith("KO:")]
        if failures:
            raise RuntimeError(f"snapshot {op} '{name}' failed: {failures[-1]}")
        if op == "load":
            self._wait_until_reachable()
            self.elements.clear()
            self.covered.clear()
            self.viewport = None

    def _wait_until_reachable(self, timeout=60):
        """Restoring a snapshot drops the adb connection for a moment; block until the device answers again."""
        end = time.time() + timeout
        while True:
            self.adb("wait-for-device", timeout=timeout)
            try:
                return self.adb("shell", "true")
            except RuntimeError:
                if time.time() >= end:
                    raise
                time.sleep(0.2)


class AgentBackend(AndroidBackend):
    """Observes and acts through droid-agent (agent/), a process kept running on the device, instead of starting a
    uiautomator or input process per call and waiting a fixed second for the UI to idle. Snapshots and the screen
    size still go through adb."""

    def __init__(self, serial=None):
        super().__init__(serial)
        self._conn = None  # (socket, reader) to the agent, opened on first use
        self._shell = None  # the `adb shell` the agent runs under; the agent exits when it does
        self._port = None  # host port adb forwards to the agent's socket
        self._activity = None  # foreground activity the agent reported with the last dump

    # ---------- connection ----------
    def _open(self):
        """Start a fresh agent and connect to it."""
        if not AGENT_JAR.exists():
            raise RuntimeError(f"{AGENT_JAR} is missing; build it with agent/build.sh")
        self.adb("push", str(AGENT_JAR), AGENT_REMOTE)
        # The token tells our agent apart from an older one that answers until ours takes the socket over.
        token = secrets.token_hex(8)
        with tempfile.TemporaryFile() as log:
            self._shell = subprocess.Popen(
                ["adb"]
                + (["-s", self.serial] if self.serial else [])
                + ["shell", f"CLASSPATH={AGENT_REMOTE} exec app_process / droid.agent.Server {token}"],
                stdin=subprocess.PIPE,  # held open: the agent exits when it closes
                stdout=log,
                stderr=log,
            )
            # A server that exited without closing leaves its forward; ours replaces its agent, so drop them.
            for line in self.adb("forward", "--list").splitlines():
                serial, local, remote = (line.split() + ["", "", ""])[:3]
                if remote == f"localabstract:{AGENT_SOCKET}" and serial == (self.serial or serial):
                    self.adb("forward", "--remove", local)
            port = self._port = int(self.adb("forward", "tcp:0", f"localabstract:{AGENT_SOCKET}"))
            end = time.time() + 15
            while True:
                try:
                    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
                    conn = (sock, sock.makefile("rb"))
                    if self._exchange(*conn, {"cmd": "hello"})[0].get("token") == token:
                        return conn
                    sock.close()
                except (OSError, ValueError):
                    pass  # not listening yet: adb accepts the connection and then drops it
                if self._shell.poll() is not None:
                    log.seek(0)
                    output = log.read().decode(errors="ignore").strip()
                    self._close()
                    raise RuntimeError(f"droid-agent exited on start: {output}")
                if time.time() >= end:
                    self._close()
                    raise RuntimeError("droid-agent did not start within 15 seconds")
                time.sleep(0.1)

    def _close(self):
        if self._conn:
            self._conn[0].close()
            self._conn = None
        if self._shell:
            self._shell.kill()
            self._shell.wait()
            self._shell = None
        if self._port:
            # If the device is gone, adb has already dropped its forwards.
            with contextlib.suppress(RuntimeError, subprocess.TimeoutExpired):
                self.adb("forward", "--remove", f"tcp:{self._port}")
            self._port = None

    @staticmethod
    def _exchange(sock, reader, request):
        sock.sendall(json.dumps(request).encode() + b"\n")
        line = reader.readline()
        if not line:
            raise ConnectionError("droid-agent closed the connection")
        reply = json.loads(line)
        payload = reader.read(reply["len"]) if "len" in reply else b""
        if len(payload) != reply.get("len", 0):
            raise ConnectionError("droid-agent closed the connection mid-reply")
        return reply, payload

    def _call(self, cmd, retry=False, **args):
        """One request to the agent, starting it if need be. With retry, a dropped connection is retried once on a
        fresh agent; actions are not retried, because the agent may have carried one out before the drop."""
        for attempt in (1, 2):
            if self._conn is None:
                self._conn = self._open()
            try:
                reply, payload = self._exchange(*self._conn, {"cmd": cmd, **args})
                break
            except (OSError, ValueError) as e:
                self._close()
                if not retry or attempt == 2:
                    raise RuntimeError(f"Lost the connection to droid-agent during {cmd}: {e}") from None
        if not reply.get("ok"):
            raise RuntimeError(f"droid-agent could not {cmd}: {reply.get('error')}")
        return reply, payload

    # ---------- observation ----------
    def ui_xml(self):
        reply, xml = self._call("dump", retry=True, quiet_ms=AGENT_QUIET_MS, timeout_ms=AGENT_SETTLE_MS)
        self._activity = reply.get("activity")
        return xml.decode()

    def current_activity(self):
        activity, self._activity = self._activity, None
        return activity or super().current_activity()

    def screenshot(self):
        reply, jpeg = self._call("screenshot", retry=True, max_w=MAX_W)
        self.scale = reply["scale"]
        return jpeg

    # ---------- actions ----------
    def tap(self, x, y):
        self._call("tap", x=x, y=y)

    def swipe(self, x1, y1, x2, y2, ms=300):
        self._call("swipe", x1=x1, y1=y1, x2=x2, y2=y2, ms=ms)

    def text(self, s):
        # The agent types keyboard characters as keys and inserts anything else, so there is no ASCII limit here.
        if s:
            self._call("text", text=s)

    def key(self, name):
        self._call("key", code=_keycode(name))

    def launch(self, package):
        # Through the agent, so it knows to wait for the app to draw before calling the screen settled.
        self._call("launch", package=package)

    def snapshot(self, op, name):
        if op == "load":
            self._close()  # the restore replaces every process on the device, the agent included
        super().snapshot(op, name)


dev = AgentBackend(SERIAL) if AGENT else AndroidBackend(SERIAL)
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
    header = f"activity: {dev.current_activity()}\n" + ("keyboard: showing\n" if dev.keyboard else "")
    out = [header + tree]
    if screenshot:
        out.append(Image(data=dev.screenshot(), format="jpeg"))
    return out


@mcp.tool()
def observe(screenshot: bool = True):
    """Current screen: foreground activity, indexed UI elements, optional screenshot."""
    return _observe(screenshot=screenshot)


@mcp.tool()
def tap(element: int, screenshot: bool = True):
    """Tap UI element by its [index] from the last observation. Returns the new screen."""
    dev.tap(*dev.point(element))
    return _observe("tap", {"element": element}, screenshot)


@mcp.tool()
def tap_xy(x: int, y: int, screenshot: bool = True):
    """Tap at coordinates in SCREENSHOT pixels (for canvas/games with no UI tree)."""
    dev.tap(round(x * dev.scale), round(y * dev.scale))
    return _observe("tap_xy", {"x": x, "y": y}, screenshot)


@mcp.tool(
    description=f"Optionally focus an element, type text ({'any characters' if AGENT else 'printable ASCII only'}), "
    "optionally press enter."
)
def type_text(text: str, element: int | None = None, submit: bool = False, screenshot: bool = True):
    if element is not None:
        dev.tap(*dev.point(element))
    dev.text(text)
    if submit:
        dev.key("enter")
    return _observe("type_text", {"text": text, "element": element, "submit": submit}, screenshot)


@mcp.tool()
def swipe(direction: str, screenshot: bool = True):
    """Scroll content: direction is up | down | left | right (content moves that way's opposite)."""
    if direction not in SWIPES:
        raise ValueError(f"Unknown direction {direction!r}; use one of: {', '.join(SWIPES)}")
    # Stay inside the part of the screen the app owns: a swipe that starts on the keyboard types instead.
    x1, y1, x2, y2 = dev.viewport or (0, 0, *dev.size())
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    dx, dy = (step * span // 3 for step, span in zip(SWIPES[direction], (x2 - x1, y2 - y1), strict=True))
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
