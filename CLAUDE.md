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

- **`AndroidBackend`** does everything by shelling out to `adb`. There is no on-device agent or companion app: the tree comes from `uiautomator dump`, images from `screencap`, input from `adb shell input`, app launch from `monkey`, and snapshots from `adb emu avd snapshot`.
- **The tool layer** is a set of `@mcp.tool()` functions on the FastMCP server `droid`. Each is a thin wrapper that performs one action on the module-global `dev` and then returns `_observe(...)`.

`_observe()` is the centre of the design. It runs `settle()`, then `compact_tree()`, then `_log()`, prepends the foreground activity, and optionally appends a screenshot. Every action tool returns it, and new action tools should too. `snapshot` is the only tool that doesn't.

### State that couples the tools together

- **Element indices are short-lived.** `dev.elements` maps index to the element's centre in device pixels and is rebuilt on every `compact_tree()` call. Because every action re-observes, indices are only valid against the most recent observation. Changing the filter in `compact_tree()` renumbers everything.
- **There are two coordinate spaces.** Element centres, swipes, and `adb input` use device pixels. `tap_xy` takes screenshot pixels and multiplies by `dev.scale`.
- **`dev.scale` is only set by `screenshot()`.** Until a screenshot has been taken in the process, it is 1.0 and `tap_xy` coordinates are treated as device pixels.
- **`settle()` dominates latency.** It needs two identical consecutive dumps, so every tool call costs at least two `uiautomator dump` round-trips, plus a `dumpsys` and the optional `screencap`. If the tree never stabilises within 5 seconds it returns the last dump without raising.
- **`swipe(direction)` names the scroll direction, not the finger direction.** `down` drags the finger up to reveal content further down.
- **`snapshot` is emulator-only**, since it goes through `adb emu`.
- **One device per server process.** `dev` is bound at import from `ANDROID_SERIAL`.
- **Bad arguments surface as raw `KeyError`s.** A stale `element`, or an unknown `key` or `direction`, fails in a dict lookup; there is no validation layer.
- **The trajectory log is not replayable on its own.** Each line holds the action, its arguments, and an MD5 of the resulting compact tree, but not the tree or the screenshot.

The server speaks MCP over stdio, so stdout is the protocol channel. Send debug output to stderr.

### Tests

The suite never touches adb. The `device` fixture in `tests/conftest.py` replaces the module-global `dev` with a backend whose `adb` method is `FakeDevice.adb` (`tests/fakes.py`), which answers each command from canned state and records the calls. Tests then call the tool functions directly, since `@mcp.tool()` returns the original function, and assert on `device.inputs` and the returned observation.

- Build UI dumps with `node()` and `hierarchy()`, and change the screen between steps with `device.show(...)`.
- The fixture also swaps in a fake clock, so `settle()` polling takes no real time, and pins `MAX_W` and `TRAJ_LOG` so a developer's shell environment cannot change results.
- The fake only encodes what we believe adb returns. A change to how the backend talks to a device still has to be exercised against a real emulator or device through an MCP client.

### Adding an iOS backend

There is no backend interface yet. The contract is whatever the tool layer calls on `dev`: the methods `settle`, `compact_tree`, `current_activity`, `screenshot`, `tap`, `swipe`, `text`, `key`, `launch`, `snapshot`, and `size`, plus the attributes `elements` and `scale`.

The seam is Android-shaped in one place: `settle()` returns raw uiautomator XML and `compact_tree()` parses it. An iOS backend has to provide both halves, or that boundary has to move first.
