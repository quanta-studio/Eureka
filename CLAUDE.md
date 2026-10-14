# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Our own mobile MCP server: lets an agent drive an Android emulator or real device, and later an iOS simulator or phone. **Android comes first; iOS is not started.**

The server is one module, `droid_mcp.py`, packaged as `droid-mcp` and still describing itself as a sketch. The GitHub repository is `quanta-studio/Eureka`.

The module docstring lists the design goals that distinguish this from generic mobile MCPs. Keep them intact when changing behaviour:

- Every action returns the new observation in the same call (one round-trip per step).
- The UI tree is compact and indexed, so the agent taps `[12]` instead of guessing pixels.
- Screenshots are downscaled to cap image-token cost and can be turned off per call.
- The UI is allowed to settle before it is observed.
- Emulator snapshots give deterministic resets between runs.
- Every step is appended to a JSONL trajectory log.

## Commands

The project is managed with `uv`; `uv run` creates and syncs `.venv` on first use.

```bash
uv run pytest                                  # all tests (no device needed)
uv run pytest tests/test_tools.py::test_tap_hits_the_indexed_element_and_returns_the_resulting_screen
uv run pytest -k swipe                         # tests matching a keyword

uv run ruff check                              # lint (add --fix to apply safe fixes)
uv run ruff format                             # format (add --check to only verify)

uv run droid-mcp                               # run the server over stdio

# Register with Claude Code
claude mcp add droid -e ANDROID_SERIAL=emulator-5554 -- uv run --directory /abs/path/to/this/repo droid-mcp

# Find a device serial / start an emulator
adb devices -l
emulator -list-avds && emulator -avd <name>
```

CI (`.github/workflows/ci.yml`) runs the same lint, format and test commands on pushes to `main` and on pull requests, with tests on Python 3.10 through 3.14. It installs with `uv sync --locked`, so a dependency change must include the updated `uv.lock`.

Two pins in the packaging are deliberate:

- **`mcp<2`.** The code imports `mcp.server.fastmcp`, which mcp 2.x removed (FastMCP became `MCPServer`). Moving to 2.x is a port, not a version bump.
- **`.python-version` is 3.12.** Python 3.10+ is supported, but without the pin `uv` picks whichever ambient interpreter it finds first.

Environment variables, all read once at import:

| Variable | Effect |
| --- | --- |
| `ANDROID_SERIAL` | Device to target. Needed whenever more than one device is attached. |
| `SHOT_MAX_W` | Maximum screenshot width in pixels (default 540). |
| `TRAJ_LOG` | Path of the JSONL trajectory log. Logging is off when unset. |

## Architecture

`droid_mcp.py` has two layers:

- **`AndroidBackend`** does everything by shelling out to `adb`. There is no on-device agent or companion app: the tree comes from `uiautomator dump --windows`, images from `screencap`, input from `adb shell input`, app launch from `monkey`, and snapshots from `adb emu avd snapshot`.
- **The tool layer** is a set of `@mcp.tool()` functions on the FastMCP server `droid`. Each is a thin wrapper that performs one action on the module-global `dev` and then returns `_observe(...)`.

`_observe()` is the centre of the design. It runs `settle()`, then `compact_tree()`, then `_log()`, prepends the foreground activity (and `keyboard: showing` when it is), and optionally appends a screenshot. Every action tool returns it, and new action tools should too. `snapshot` is the only tool that doesn't.

### What `compact_tree()` works out

The dump covers every window, not just the app. `compact_tree()` lists the active window only and uses the others to decide what is reachable:

- **`dev.elements` maps an index to a tap point, which is not always the centre.** Anything clickable drawn later, and any window stacked above, takes the touch, so the point moves to the roomiest exposed part of the element. A covered label resolves to its clickable row.
- **`dev.covered` holds listed elements with nowhere left to tap**, with what hides them. They are flagged `covered` in the tree and `tap` refuses them. `tap_xy` is the escape hatch.
- **`dev.viewport` is the part of the active window no other window sits over.** `swipe` stays inside it, because a swipe that starts on the keyboard types a character instead of scrolling.
- **A container that is not itself clickable is never covered by its own children.** Without that rule every list and scroll view reads as covered.

All three are rebuilt on every call, so indices are only valid against the most recent observation, and changing the listing filter renumbers everything.

### Other things that couple the tools

- **There are two coordinate spaces.** Tap points, swipes, and `adb input` use device pixels. `tap_xy` takes screenshot pixels and multiplies by `dev.scale`.
- **`dev.scale` is only set by `screenshot()`.** Until a screenshot has been taken in the process, it is 1.0 and `tap_xy` coordinates are treated as device pixels.
- **A step costs one `uiautomator dump`, 2.5 to 3 seconds.** Over a second of that is starting a Java process; the rest is uiautomator's own wait for the UI to go idle, which is why `settle()` does not dump a second time to compare. If the device produces no tree at all (screen off), it retries for 5 seconds and then raises.
- **Screenshots are captured as raw pixels and encoded as JPEG on the host.** Encoding a PNG on the device was most of a capture's cost. `screencap -p` remains the fallback for pixel formats other than RGBA_8888 and RGBX_8888.
- **The dump is one `adb exec-out` round trip that deletes the file before and after.** `uiautomator` can report success without writing anything, and reading a leftover file would present a stale screen as current.
- **Typed text goes through the device's shell**, so `text()` shell-quotes it. Dropping the quoting turns `type_text` into remote command execution.
- **`input text` is the weak link for typing.** It handles printable ASCII only, turns `%s` into a space, and on a slow emulator silently drops whatever one call has not typed within about 10 seconds, hence the short `TYPE_CHUNK` pieces.
- **`swipe(direction)` names the scroll direction, not the finger direction.** `down` drags the finger up to reveal content further down.
- **`snapshot` is emulator-only**, since it goes through `adb emu`. The emulator console reports failure as `KO:` text with exit code 0, and a load drops the adb connection for about half a second, so `snapshot()` checks the text and waits for the device.
- **One device per server process.** `dev` is bound at import from `ANDROID_SERIAL`.
- **The trajectory log is not replayable on its own.** Each line holds the action, its arguments, and an MD5 of the resulting compact tree, but not the tree or the screenshot. Failed steps and `snapshot` calls are not logged.

The server speaks MCP over stdio, so stdout is the protocol channel. Send debug output to stderr.

### Tests

The suite never touches adb. The `device` fixture in `tests/conftest.py` replaces the module-global `dev` with a backend whose `adb` method is `FakeDevice.adb` (`tests/fakes.py`), which answers each command from canned state and records the calls. Tests then call the tool functions directly, since `@mcp.tool()` returns the original function, and assert on `device.inputs` and the returned observation.

- Build UI dumps with `node()` and `hierarchy()`, or `window()` and `windows()` when the keyboard or system bars matter, and change the screen between steps with `device.show(...)`.
- For `input text`, the fake runs the command line through a real `sh` and applies Android's `%s` decoding, so `device.typed` is what the field would receive and `device.side_effects` shows anything the shell executed.
- The fixture also swaps in a fake clock, so `settle()` polling takes no real time, and pins `MAX_W` and `TRAJ_LOG` so a developer's shell environment cannot change results.
- The fake only encodes what we believe adb returns. A change to how the backend talks to a device still has to be exercised against a real emulator or device through an MCP client.

### Adding an iOS backend

There is no backend interface yet. The contract is whatever the tool layer calls on `dev`: the methods `settle`, `compact_tree`, `current_activity`, `screenshot`, `tap`, `swipe`, `text`, `key`, `launch`, `snapshot`, and `size`, plus the attributes `elements` and `scale`.

The seam is Android-shaped in one place: `settle()` returns raw uiautomator XML and `compact_tree()` parses it. An iOS backend has to provide both halves, or that boundary has to move first.
