# droid-mcp

An MCP server that lets an AI agent such as Claude Code use an Android emulator or phone. The agent sees the screen as a short numbered list of what's on it, plus a screenshot. It acts by number ("tap `[12]`") instead of guessing pixels, and every action returns the new screen in the same call.

```
activity: com.android.settings/.homepage.SettingsHomepageActivity
[0] LinearLayout "" id=search_action_bar tap
[1] TextView "Search Settings" id=search_bar_title
[2] ScrollView "" id=main_content_scrollable_container scroll
[3] RecyclerView "" id=recycler_view scroll
[4] LinearLayout "" tap
[5] TextView "Google" id=title
[6] TextView "Services & preferences" id=summary
...
```

Android only for now. iOS is planned.

## What you need

- [uv](https://docs.astral.sh/uv/)
- Android platform-tools: `adb` on your `PATH`
- A device that `adb devices` lists as `device`:
  - **Emulator:** `emulator -list-avds`, then `emulator -avd <name>`.
  - **Phone:** turn on USB debugging in Developer options, plug it in, and accept the prompt on the phone. Keep the screen on and unlocked while tests run. Developer options → **Stay awake** keeps it on while charging.
- For the faster droid-agent backend (recommended, see below), also:
  - a JDK 17 or newer (the one bundled with Android Studio works),
  - the Android SDK with build-tools and an SDK platform installed.

## Install

```bash
git clone git@github.com:quanta-studio/Eureka.git droid-mcp
cd droid-mcp
uv sync
agent/build.sh   # builds the on-device agent; skip it if you won't use DROID_AGENT=1
```

`agent/build.sh` finds the SDK through `ANDROID_HOME`, or `~/Library/Android/sdk` if that's unset. It compiles against platform 36. If you have a different one installed, set `AGENT_PLATFORM`, e.g. `AGENT_PLATFORM=35 agent/build.sh`.

## Use it from your app's project in Claude Code

Find your device's serial:

```bash
adb devices -l
```

Then register the server from your app's repository, not this one:

```bash
cd ~/code/your-app
claude mcp add droid \
  -e ANDROID_SERIAL=emulator-5554 \
  -e DROID_AGENT=1 \
  -- uv run --directory /absolute/path/to/droid-mcp droid-mcp
```

Start a new Claude Code session there and run `/mcp` to check that `droid` is connected.

- **Scope:** by default the server is registered for you, in that project only. Add `--scope user` to have it in every project. `--scope project` writes `.mcp.json` into the repo for teammates, but they need the same absolute path.
- **To change a setting,** for example to point at another device: `claude mcp remove droid`, then add it again.

Other MCP clients take the same command in their JSON config:

```json
{
  "mcpServers": {
    "droid": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/droid-mcp", "droid-mcp"],
      "env": { "ANDROID_SERIAL": "emulator-5554", "DROID_AGENT": "1" }
    }
  }
}
```

## Settings

All are environment variables, read when the server starts.

| Variable | What it does |
| --- | --- |
| `ANDROID_SERIAL` | Which device to use. Required when more than one is attached. |
| `DROID_AGENT` | `1` drives the device through droid-agent, which is about 2–3× faster per step. Needs `agent/build.sh`. Off by default. |
| `SHOT_MAX_W` | Screenshot width in pixels (default 540). Smaller costs fewer image tokens. |
| `TRAJ_LOG` | Path of a JSONL file that gets one line per step: the action, its arguments and a hash of the resulting screen. |

## Tools

Every tool except `snapshot` returns the new screen, with a screenshot unless you pass `screenshot: false`.

| Tool | What it does |
| --- | --- |
| `observe` | The current screen. |
| `tap(element)` | Taps an element by its number in the last screen. |
| `tap_xy(x, y)` | Taps a point in screenshot pixels, for games and canvases that list no elements. |
| `type_text(text, element?, submit?)` | Optionally taps a field first, types, and optionally presses enter. |
| `swipe(direction)` | Scrolls `up`, `down`, `left` or `right`. `down` reveals what's further down. |
| `press(key)` | `back`, `home`, `enter`, `recents` or `delete`. |
| `launch_app(package)` | Starts an app, e.g. `com.android.settings`. |
| `snapshot(op, name)` | Emulator only: `save` or `load` the whole emulator state. |

How to read a screen:

- **Numbers are only valid for the latest screen.** Any new screen renumbers everything.
- **Flags:**
  - `tap`: clickable.
  - `scroll`: scrollable.
  - `checked`, `focused`: the element's state.
  - `covered`: something is on top of the element, so `tap` refuses it. Dismiss what's covering it, or use `tap_xy`.
- **Header lines:** `keyboard: showing` appears while the soft keyboard is up.

## Running a test on your app

1. **Install the build you want to test**, e.g. `./gradlew installDebug` or `adb install app-debug.apk`.
2. **Start from a known state.**
   - **Emulator:** set the app up once (fresh install, logged out), then save a snapshot with `adb emu avd snapshot save clean`, or ask Claude to save one. A test can then begin with `snapshot load clean`.
   - **Phone:** snapshots aren't available. Reset the app with `adb shell pm clear <package>`, which wipes its data.
3. **Ask for the test in plain language.** Name the package, the steps, and what counts as a pass. For example:

   > Use the droid tools. Load the emulator snapshot `clean`, launch `com.example.app`, sign in with the test account `qa@example.com` / `Test1234`, open Rewards, and check that the points balance shows a number. Only take screenshots when you need to see something the element list doesn't show. At the end, list each step and whether it passed.

4. **To keep a record,** set `TRAJ_LOG` when you register the server.

Tips:

- **Use a test account.** Whatever Claude types, passwords included, goes to the device, and `TRAJ_LOG` records it too.
- **Screenshots are the expensive part.** If the element list is enough for a flow, say so in your request.
- **Elements without a label show up as `""`.** That's an accessibility gap in the app; Claude can still tap them by number.
- **Unicode:** with `DROID_AGENT=1`, any text can be typed (accents, CJK, emoji). Without it, only printable ASCII can be typed.

## Which backend?

**Default (uiautomator):** each step starts `uiautomator` and `adb shell input` on the device.
- **Speed:** about 3.5–5.5 s per step on a Galaxy A07, screenshot included.
- **Setup:** nothing to build.

**droid-agent (`DROID_AGENT=1`):** a small process kept running on the device for the whole session. It's pushed and started automatically and needs no root and no installed app.
- **Speed:** on the Galaxy A07, action steps averaged 1.8 s instead of 4.1 s, and looking at an unchanged screen took 0.1 s instead of 3.4 s.
- **Accessibility layouts:** it keeps accessibility switched on, and some apps lay their screens out differently while it is on.
- **Exclusive:** while it runs, nothing else can use uiautomator on that device. That includes Appium, Espresso or UI Automator tests, and `uiautomator dump`.
- **Testing:** so far it has only been tested on Android 16.

## Troubleshooting

| Message or symptom | What to do |
| --- | --- |
| `more than one device/emulator` | Set `ANDROID_SERIAL` to the device from `adb devices`. |
| `The device returned no UI tree; its screen is probably off or locked.` | Wake and unlock the device. |
| `…droid-agent.jar is missing; build it with agent/build.sh` | Run `agent/build.sh` in this repo, or remove `DROID_AGENT=1`. |
| `droid-agent exited on start: could not connect to UI automation…` | Something else on the device holds its one UI automation connection: `uiautomator`, an Appium session or an instrumentation test. Stop it and try again. Another droid-mcp is not a problem; the newer one takes over. |
| `agent/build.sh` can't find `android.jar` or `d8` | Install an SDK platform and build-tools in Android Studio's SDK Manager, or set `ANDROID_HOME` / `AGENT_PLATFORM`. |
| `Element [n] is hidden behind …` | A keyboard, dialog or other control is over it. Dismiss it, scroll, or use `tap_xy`. |
| `Snapshots need an emulator…` | Snapshots only work on emulators; reset a phone with `adb shell pm clear <package>`. |
| `droid` doesn't appear in `/mcp` | Check the absolute path in the `claude mcp add` command, and that `uv run droid-mcp` starts from this repo without errors. |

## Development

```bash
uv run pytest        # tests; no device needed
uv run ruff check    # lint
uv run ruff format   # format
uv run python agent/bench.py --serial <serial> --out results/   # compare the two backends on a device
```

`agent/bench.py` taps through the Settings app, so only run it on a device where that's fine. CLAUDE.md describes how the server works inside.
