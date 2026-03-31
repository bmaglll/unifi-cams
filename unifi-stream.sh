#!/usr/bin/env bash
# unifi-stream.sh — Launch a camera stream in a floating PIP window via mpv
# Uses the "Picture-in-Picture" title so Hyprland's existing window rules apply.

if [[ -z "$1" ]]; then
  echo "Usage: unifi-stream.sh <stream-url>"
  echo "Example: unifi-stream.sh 'rtsps://192.168.1.1:7441/token?enableSrtp'"
  exit 1
fi

MUTE_FLAG=""
case "${UNIFI_PIP_MUTED:-0}" in
  1|true|yes) MUTE_FLAG="--mute=yes" ;;
esac

exec mpv \
  --title="Picture-in-Picture" \
  --profile=low-latency \
  --demuxer-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5 \
  $MUTE_FLAG \
  "$1"
