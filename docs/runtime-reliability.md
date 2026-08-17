# Local runtime reliability audit

Findings from auditing why VA_PODCAST_2's local GUI sometimes became
unavailable, and what was (and wasn't) changed as a result.

## Root cause

Two independent, compounding factors — not a Topic Engine or GUI bug:

1. **Port 5000 is macOS's AirPlay Receiver port.** `ControlCenter.app`
   (which hosts AirPlay Receiver) runs by default on modern macOS and can
   intermittently bind port 5000. This was directly reproduced during this
   audit and in earlier sessions: `python app.py` failed with `Address
   already in use ... Port 5000 is in use by another program ... AirPlay
   Receiver`. It doesn't happen every time — the OS doesn't hold the port
   constantly — which produces exactly the "sometimes it loads, sometimes
   it doesn't" symptom.
2. **The dev server has no process supervision.** `python app.py` runs
   entirely in the foreground of whatever terminal/shell launched it, with
   Flask's `debug=True` reloader spawning one worker subprocess. There is
   no background daemon, no `launchd` job, and no auto-restart. If the
   terminal that launched it is closed, the shell session ends, or the
   machine sleeps in a way that kills the shell, the Flask process dies
   with it — silently, from the browser's point of view. The next refresh
   then hits a dead port and shows a connection failure.

Neither of these is a code defect in `app.py`'s Flask setup itself: host
(`127.0.0.1`), debug/reloader config, and the `if __name__ == "__main__"`
guard were all already correct.

## What was ruled out

- **The reloader is not killing itself on data writes.** `topic_engine/engine.py`
  rewrites `data.json` while the server may be running. Tested directly:
  rewriting `data.json` while the server was up produced no "Restarting
  with stat" log line and no dropped request — Werkzeug's default reloader
  only watches imported Python module files, not JSON data files.
- **The GUI has no hardcoded host or port.** Its **Go Hunting** control uses
  relative `/api/refresh` and `/api/refresh/status` routes. After a successful
  hunt, **View Updated Dashboard** reloads the current URL (including its
  selected timeframe). It still depends on the local Flask process being up.

## Fix

- **Moved the dev server from port 5000 to port 5050.** Changed in exactly
  one place — the `PORT` constant near the top of `app.py` — and referenced
  from `app.run()`. 5050 is not a macOS system service port. `README.md`
  updated to the new canonical URL.
- **Added `run.sh`** as the one documented way to start the app
  (`cd` to the repo, activate the venv, run `python app.py`) so the launch
  command is consistent and doesn't depend on remembering the venv-activate
  step.

## What this does NOT fix (by design — out of scope for this audit)

The server still only runs as long as its terminal process does. There is
still no background daemon, no `launchd` job, and no auto-restart. That is
a real, current limitation, not a hidden one: **VA_PODCAST_2 is a
foreground local dev tool today, not a persistent local service.** Adding
real process supervision (`launchd`, a background service, etc.) is a
separate, larger decision deliberately deferred rather than bundled into
this reliability pass.

## Canonical local URL

```text
http://127.0.0.1:5050/
```

Start with `./run.sh` (or `python app.py` after activating `.venv`). If the
GUI becomes unreachable, the first thing to check is whether that process
is still running — not the Topic Engine, not the GUI code.
