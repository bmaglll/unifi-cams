#!/usr/bin/env python3
"""UniFi Protect webhook listener — receives motion events, sends desktop notifications,
and auto-launches mpv PIP streams for cameras with motion."""

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PROTECT_LISTEN_PORT", "9999"))
COOLDOWN = int(os.environ.get("PROTECT_COOLDOWN", "30"))
TOKEN = os.environ.get("PROTECT_TOKEN", "")
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOUND = os.environ.get("PROTECT_SOUND", os.path.join(_SCRIPT_DIR, "assets", "notification_sound.mp3"))

# MAC address -> {"name": "...", "stream": "rtsps://..."}
CAMERAS: dict[str, dict] = json.loads(os.environ.get("PROTECT_CAMERAS", "{}"))

# Per-device cooldown tracking: device_mac -> last notification timestamp
_last_notify: dict[str, float] = {}

# Per-device mpv process tracking: device_mac -> Popen
_mpv_procs: dict[str, subprocess.Popen] = {}

MPV_CMD = [
    "mpv",
    "--profile=low-latency",
    "--demuxer-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
]


def save_thumbnail(data_uri: str, event_id: str) -> str | None:
    """Decode base64 thumbnail from payload and save to temp file."""
    try:
        # Strip "data:image/jpeg;base64," prefix
        header, b64data = data_uri.split(",", 1)
        img_bytes = base64.b64decode(b64data)
        tmp = os.path.join(tempfile.gettempdir(), f"protect_{event_id}.jpg")
        with open(tmp, "wb") as f:
            f.write(img_bytes)
        return tmp
    except Exception as e:
        print(f"[protect-notify] Thumbnail decode failed: {e}", flush=True)
        return None


def _play_sound():
    """Play the notification sound if configured and the file exists."""
    if SOUND and os.path.isfile(SOUND):
        print(f"[protect-notify] Playing sound: {SOUND}", flush=True)
        subprocess.Popen(
            ["mpv", "--no-video", "--really-quiet", SOUND],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _notify_and_stream(cmd: list, mac: str, camera_name: str, stream_url: str | None):
    """Send notification; if user clicks 'View Stream', launch mpv PIP."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.stdout.strip() != "view" or not stream_url:
        return
    # Skip if mpv is already running for this camera
    existing = _mpv_procs.get(mac)
    if existing and existing.poll() is None:
        print(f"[protect-notify] mpv already open for {camera_name}", flush=True)
        return
    print(f"[protect-notify] Launching mpv PIP for {camera_name}", flush=True)
    _mpv_procs[mac] = subprocess.Popen(
        MPV_CMD + ["--title=Picture-in-Picture", stream_url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Verify bearer token
        if TOKEN:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {TOKEN}":
                self.send_response(401)
                self.end_headers()
                return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()

        # Parse the nested alarm structure
        alarm = payload.get("alarm", {})
        triggers = alarm.get("triggers", [])
        thumbnail_uri = payload.get("thumbnail", "")

        if not triggers:
            return

        # Save thumbnail once for all triggers in this payload
        thumb_path = None
        if not thumbnail_uri:
            thumbnail_uri = alarm.get("thumbnail", "")
        if thumbnail_uri:
            event_id = triggers[0].get("eventId", "event")
            thumb_path = save_thumbnail(thumbnail_uri, event_id)

        for trigger in triggers:
            mac = trigger.get("device", "unknown")
            event_type = trigger.get("key", "motion")
            timestamp_ms = trigger.get("timestamp")

            cam_info = CAMERAS.get(mac, {})
            camera_name = cam_info.get("name", mac) if isinstance(cam_info, dict) else cam_info

            # Cooldown check
            now = time.monotonic()
            last = _last_notify.get(mac, 0)
            if now - last < COOLDOWN:
                continue

            # Skip if PIP stream is already open for this camera
            existing = _mpv_procs.get(mac)
            if existing and existing.poll() is None:
                print(f"[protect-notify] Stream open for {camera_name}, skipping notification", flush=True)
                continue

            _last_notify[mac] = now

            # Format timestamp
            ts = ""
            if timestamp_ms:
                ts = f"\n{time.strftime('%I:%M:%S %p', time.localtime(timestamp_ms / 1000))}"

            description = event_type.replace("_", " ").title()

            body = f"{description}{ts}"
            if thumb_path:
                body += f'\n<img src="file://{thumb_path}" alt="thumbnail"/>'

            stream_url = cam_info.get("stream") if isinstance(cam_info, dict) else None

            cmd = [
                "notify-send",
                "-a", "UniFi Protect",
                "-i", os.path.join(_SCRIPT_DIR, "assets", "UI.svg"),
                "-t", "10000",
            ]
            if stream_url:
                cmd += ["--wait", "-A", "view=View Stream"]
            cmd += [camera_name, body]

            _play_sound()

            # Run in thread so --wait doesn't block the webhook handler
            threading.Thread(
                target=_notify_and_stream,
                args=(cmd, mac, camera_name, stream_url),
                daemon=True,
            ).start()

    def log_message(self, format, *args):
        print(f"[protect-notify] {args[0]}", flush=True)


def main():
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    print(f"[protect-notify] Listening on 0.0.0.0:{PORT}", flush=True)
    print(f"[protect-notify] Cameras: {CAMERAS}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[protect-notify] Shutting down", flush=True)
        server.server_close()


if __name__ == "__main__":
    main()
