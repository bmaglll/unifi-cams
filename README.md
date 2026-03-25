# UniFi Protect PIP Notifications

Desktop notification listener for UniFi Protect motion events. Sends notifications with camera thumbnails and lets you open a live PIP (picture-in-picture) stream via mpv.

## Dependencies

- **Python 3.10+**
- **mpv** — video player for PIP streams
- **libnotify** (`notify-send`) — desktop notifications
- A notification daemon like **SwayNC**, **mako**, or **dunst**

### Install on NixOS

These are likely already available if you're running Hyprland/Sway. Otherwise add `mpv` and `libnotify` to your system packages.

### Install on Arch

```sh
sudo pacman -S mpv libnotify
```

## Setup

1. **Copy the example env file and fill in your values:**

   ```sh
   cp .env.example .env
   ```

2. **Edit `.env`** with your UniFi Protect details:

   - `PROTECT_TOKEN` — Create a webhook in the Protect UI and copy the bearer token
   - `PROTECT_HOST` — IP of your Protect console (Cloud Key, UNVR, UDM, etc.)
   - `PROTECT_CAMERAS` — JSON mapping of camera MAC addresses to names and RTSP stream URLs

3. **Find your camera info:**

   - **MAC address**: Protect UI → Camera → Settings → General (format: uppercase, no colons, e.g. `AABBCCDDEEFF`)
   - **RTSP stream URL**: Protect UI → Camera → Settings → Advanced → RTSP

4. **Configure the webhook in UniFi Protect:**

   - Go to Settings → Notifications → Webhooks
   - Add a new webhook pointing to `http://<your-computer-ip>:9999`
   - Use the same bearer token you put in `.env`

## Usage

```sh
# Source the env file and run
set -a && source .env && set +a
python3 protect-notify.py
```

When motion is detected, you'll get a desktop notification with a thumbnail. Click **View Stream** to open a live PIP window via mpv.

### Standalone PIP stream

To manually open a camera stream in a PIP window:

```sh
./lucas-cam.sh 'rtsps://192.168.1.1:7441/your-stream-token?enableSrtp'
```

## Notification Sounds

A default sound (`notification_sound.mp3`) is included and plays automatically with each alert via mpv.

To use a custom sound, set `PROTECT_SOUND` in your `.env`:

```sh
PROTECT_SOUND=/path/to/custom-sound.mp3
```

To disable sound, set it to an empty string:

```sh
PROTECT_SOUND=
```
