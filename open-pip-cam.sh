#!/usr/bin/env bash
# open-pip-cam.sh — Launch a camera stream in a floating PIP window via mpv
# Uses the "Picture-in-Picture" title so Hyprland's existing window rules apply.

if [[ -z "$1" ]]; then
  echo "Usage: open-pip-cam.sh <stream-url>"
  echo "Example: open-pip-cam.sh 'rtsps://192.168.1.1:7441/token?enableSrtp'"
  exit 1
fi

exec mpv \
  --title="Picture-in-Picture" \
  --profile=low-latency \
  --demuxer-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5 \
  "$1"
