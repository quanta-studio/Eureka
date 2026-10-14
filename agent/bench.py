"""Runs the same steps through uiautomator/adb and through droid-agent, times each step, and checks that both
backends observed the same screen after it.

    uv run python agent/bench.py --serial emulator-5554 [--rounds 3] [--quiet-ms 500] [--no-screenshot] [--out DIR]

It drives the Settings app, so run it on a device you don't mind being tapped. The backends take turns, because
uiautomator cannot connect while the agent holds the UiAutomation connection. With --out, the timings, the screen
each backend observed after each step and its screenshot (from the last round) are written to DIR.
"""

import argparse
import json
import statistics
import time
from pathlib import Path

import droid_mcp

SETTINGS = ["com.android.settings", "com.android.settings.intelligence", "com.google.android.settings.intelligence"]
SCENARIO = [
    ("press", {"key": "home"}),
    ("launch_app", {"package": "com.android.settings"}),
    ("observe", {}),
    ("swipe", {"direction": "down"}),
    ("swipe", {"direction": "up"}),
    ("tap", {"on": "Display"}),
    ("swipe", {"direction": "down"}),
    ("press", {"key": "back"}),
    ("tap", {"on": "Search"}),
    ("type_text", {"text": "wifi"}),
    ("press", {"key": "back"}),
    ("press", {"key": "back"}),
]


def element(tree, text):
    found = [int(line[1 : line.index("]")]) for line in tree.splitlines() if f'"{text}' in line]
    if not found:
        raise SystemExit(f'no element "{text}..." on this screen:\n{tree}')
    return found[0]


def label(tool, args):
    return f"{tool} {next(iter(args.values()), '')}".strip()


def run(backend, rounds, screenshot):
    droid_mcp.dev = backend
    times = [[] for _ in SCENARIO]
    screens = [None] * len(SCENARIO)
    shots = [None] * len(SCENARIO)
    for _ in range(rounds):
        # Settings search is its own package, left on top of the Settings task if a round ended inside it.
        for package in SETTINGS:
            backend.adb("shell", "am", "force-stop", package)
        tree = ""
        for i, (tool, args) in enumerate(SCENARIO):
            if "on" in args:
                args = {"element": element(tree, args["on"])}
            start = time.perf_counter()
            out = getattr(droid_mcp, tool)(**args, screenshot=screenshot)
            times[i].append(time.perf_counter() - start)
            tree = out[0].split("\n", 1)[1]
            screens[i] = out[0]
            shots[i] = out[1].data if screenshot else None
    return times, screens, shots


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--serial", default=droid_mcp.SERIAL)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--quiet-ms", type=int, default=droid_mcp.AGENT_QUIET_MS)
    p.add_argument("--no-screenshot", dest="screenshot", action="store_false")
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    droid_mcp.AGENT_QUIET_MS = a.quiet_ms

    agent = droid_mcp.AgentBackend(a.serial)
    start = time.perf_counter()
    agent._call("hello")
    started = time.perf_counter() - start
    agent_times, agent_screens, agent_shots = run(agent, a.rounds, a.screenshot)
    agent._close()
    adb = droid_mcp.AndroidBackend(a.serial)
    adb_times, adb_screens, adb_shots = run(adb, a.rounds, a.screenshot)

    print(f"agent start-up (push + launch + connect): {started:.2f}s, quiet period {a.quiet_ms} ms")
    print(f"{'step':<28}{'uiautomator':>12}{'agent':>9}{'speed-up':>10}  same screen")
    for (tool, args), t_adb, t_agent, s_adb, s_agent in zip(
        SCENARIO, adb_times, agent_times, adb_screens, agent_screens, strict=True
    ):
        m_adb, m_agent = statistics.median(t_adb), statistics.median(t_agent)
        same = "yes" if s_adb == s_agent else "NO"
        print(f"{label(tool, args):<28}{m_adb:>11.2f}s{m_agent:>8.2f}s{m_adb / m_agent:>9.1f}x  {same}")
    total_adb = sum(map(statistics.median, adb_times))
    total_agent = sum(map(statistics.median, agent_times))
    print(f"{'total':<28}{total_adb:>11.2f}s{total_agent:>8.2f}s{total_adb / total_agent:>9.1f}x")
    for (tool, args), s_adb, s_agent in zip(SCENARIO, adb_screens, agent_screens, strict=True):
        if s_adb != s_agent:
            print(f"\n--- {label(tool, args)}: uiautomator saw\n{s_adb}\n--- agent saw\n{s_agent}")

    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        steps = []
        for i, (tool, args) in enumerate(SCENARIO):
            step = {"step": label(tool, args), "uiautomator": {}, "agent": {}}
            for name, times, screens, shots in (
                ("uiautomator", adb_times, adb_screens, adb_shots),
                ("agent", agent_times, agent_screens, agent_shots),
            ):
                step[name] = {"times": [round(t, 3) for t in times[i]], "screen": screens[i]}
                if shots[i]:
                    step[name]["shot"] = f"{i:02d}-{name}.jpg"
                    (a.out / step[name]["shot"]).write_bytes(shots[i])
            steps.append(step)
        device = {
            k: adb.adb("shell", "getprop", prop).strip()
            for k, prop in (
                ("model", "ro.product.model"),
                ("android", "ro.build.version.release"),
                ("sdk", "ro.build.version.sdk"),
            )
        }
        result = {
            "device": device,
            "size": adb.size(),
            "rounds": a.rounds,
            "quiet_ms": a.quiet_ms,
            "agent_start_s": round(started, 3),
            "steps": steps,
        }
        (a.out / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
        print(f"\nwrote {a.out}/results.json and the screenshots it names")


if __name__ == "__main__":
    main()
