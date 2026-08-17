#!/bin/bash
# Canonical way to start VA_PODCAST_2 locally.
#
# This does NOT daemonize the server — it stays in the foreground and only
# runs as long as this terminal/process does. See docs/runtime-reliability.md.
cd "$(dirname "$0")"
source .venv/bin/activate
python app.py
