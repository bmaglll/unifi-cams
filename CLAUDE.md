# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

UniFi Cams — desktop camera dashboard and notification system for Linux. GTK4 + GStreamer live camera viewer with motion event webhook notifications. Includes PIP stream launcher.

## Architecture

Two main components, both configured via `.env` (see `.env.example`):

- **unifi-notify.py** — HTTP webhook server (stdlib `http.server`) that receives UniFi Protect motion events on port 9999. Sends desktop notifications via `notify-send` with thumbnails. Clicking "View Stream" on a notification launches an mpv PIP window. Uses per-camera cooldown and tracks active mpv processes to avoid duplicates. Notification handling runs in daemon threads to avoid blocking the webhook handler.

- **unifi-dashboard.py** — GTK4 + GStreamer GUI that displays live RTSPS camera streams. Uses `playbin` + `gtk4paintablesink` for GPU-accelerated video rendering. Bottom panel lists cameras with checkboxes (toggle stream) and PIP buttons. Reads `.env` directly (does not use environment variables).

Shell helpers:
- **unifi-stream.sh** — Standalone mpv PIP launcher for a single RTSP stream
- **unifi-dashboard.sh** — nix-shell launcher for `unifi-dashboard.py` (provides GTK4/GStreamer/PyGObject)

## Running

```sh
# Webhook notification listener
set -a && source .env && set +a
python3 unifi-notify.py

# Camera dashboard GUI (nix-shell provides dependencies)
./unifi-dashboard.sh
```

## Dependencies

- Python 3.10+ (no pip packages — stdlib only)
- mpv (PIP streams + notification sounds)
- libnotify / notify-send (desktop notifications)
- ffmpeg (unifi-notify thumbnail capture)
- GTK4 + PyGObject + GStreamer (unifi-dashboard — provided by `unifi-dashboard.sh` nix-shell)

## Configuration

Config is split between `.env` and `cameras.json` (neither committed):
- `.env` — `UNIFI_TOKEN`, `UNIFI_COOLDOWN`, `UNIFI_SNOOZE_MINS`, `UNIFI_SOUND`, etc.
- `cameras.json` — MAC address → `{"name": "...", "stream": "rtsps://..."}` map (read directly by both scripts)

## Conventions

- No pip packages — unifi-notify uses stdlib only; unifi-dashboard uses PyGObject (provided by nix-shell)
- Camera thumbnails are cached in `/tmp/unifi-cams/` as `{MAC}.jpg`, shared between both components
- mpv windows use `--title=Picture-in-Picture` so Hyprland window rules can float them
- All log output is prefixed with `[unifi-notify]` and uses `flush=True`
