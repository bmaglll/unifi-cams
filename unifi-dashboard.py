#!/usr/bin/env python3
"""Camera Dashboard — GTK4 + GStreamer live camera viewer.

Displays live RTSPS camera streams using GStreamer's playbin +
gtk4paintablesink for GPU-accelerated video rendering. Bottom panel
lists cameras with checkboxes (toggle stream) and PIP buttons (launch mpv).
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Gdk, Gst, GLib, Gio

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"


def load_dotenv(path):
    """Parse .env file → (dict of KEY=VALUE, raw lines list)."""
    settings = {}
    lines = []
    if path.exists():
        text = path.read_text()
        lines = text.splitlines(keepends=True)
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            settings[key] = value
    return settings, lines


def save_dotenv(path, settings, original_lines):
    """Write settings back to .env, preserving comments and blank lines."""
    written_keys = set()
    out = []
    for line in original_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, _ = stripped.partition("=")
            key = key.strip()
            if key in settings:
                out.append(f"{key}={settings[key]}\n")
                written_keys.add(key)
                continue
        out.append(line if line.endswith("\n") else line + "\n")
    # Append any new keys not already in the file
    for key, value in settings.items():
        if key not in written_keys:
            out.append(f"{key}={value}\n")
    path.write_text("".join(out))


def _restart_notify():
    """Kill any running unifi-notify.py and respawn with current .env values."""
    result = subprocess.run(
        ["pgrep", "-f", "python3.*unifi-notify"], capture_output=True, text=True
    )
    for pid in result.stdout.strip().splitlines():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

    env = os.environ.copy()
    settings, _ = load_dotenv(ENV_PATH)
    env.update(settings)

    subprocess.Popen(
        ["python3", str(SCRIPT_DIR / "unifi-notify.py")],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


SETTINGS_FIELDS = [
    ("UNIFI_LISTEN_HOST", "Listen Host", "text", "0.0.0.0"),
    ("UNIFI_LISTEN_PORT", "Listen Port", "number", "9999"),
    ("UNIFI_HOST", "UniFi Host", "text", "192.168.1.1"),
    ("UNIFI_TOKEN", "Auth Token", "password", ""),
    ("UNIFI_COOLDOWN", "Cooldown (seconds)", "number", "30"),
    ("UNIFI_SNOOZE_MINS", "Snooze (minutes)", "number", "30"),
    ("UNIFI_SOUND", "Sound File", "file", ""),
    ("UNIFI_PIP_MUTED", "PIP Muted", "toggle", "0"),
]


Gst.init(None)
Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)


def load_cameras():
    """Load cameras from cameras.json."""
    cam_path = SCRIPT_DIR / "cameras.json"
    with open(cam_path) as f:
        return json.load(f)
    return {}


class CameraStream:
    """Manages a GStreamer pipeline fed by ffmpeg for one camera.

    ffmpeg handles the RTSPS/TLS/SRTP connection (which GStreamer's rtspsrc
    can't do with UniFi's self-signed certs). It outputs an mpegts container
    to stdout, which GStreamer demuxes, decodes, and renders via gtk4paintablesink.
    """

    def __init__(self, stream_url):
        self.stream_url = stream_url
        self.ffmpeg_proc = None
        self._reconnect_id = None

        # Build pipeline: fdsrc → decodebin → videoconvert → gtk4paintablesink
        #                                   → audioconvert → volume → autoaudiosink
        self.pipeline = Gst.Pipeline.new()

        self.fdsrc = Gst.ElementFactory.make("fdsrc")
        self.fdsrc.set_property("blocksize", 4096)
        self.decodebin = Gst.ElementFactory.make("decodebin")
        self.videoconvert = Gst.ElementFactory.make("videoconvert")
        self.sink = Gst.ElementFactory.make("gtk4paintablesink")
        self.sink.set_property("sync", False)

        self.audioconvert = Gst.ElementFactory.make("audioconvert")
        self.volume = Gst.ElementFactory.make("volume")
        self.volume.set_property("volume", 0.0)
        self.audiosink = Gst.ElementFactory.make("autoaudiosink")
        self.audiosink.set_property("sync", False)

        for el in (self.fdsrc, self.decodebin, self.videoconvert, self.sink,
                   self.audioconvert, self.volume, self.audiosink):
            self.pipeline.add(el)

        self.fdsrc.link(self.decodebin)
        self.videoconvert.link(self.sink)
        self.audioconvert.link(self.volume)
        self.volume.link(self.audiosink)

        # decodebin creates pads dynamically — link video pads to videoconvert
        self.decodebin.connect("pad-added", self._on_pad_added)
        self.decodebin.connect("element-added", self._on_element_added)

        # Create GTK Picture widget from the paintable
        paintable = self.sink.get_property("paintable")
        picture = Gtk.Picture.new_for_paintable(paintable)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)

        # Overlay with "Connecting..." label until first frame
        self.loading_label = Gtk.Label(label="Connecting...")
        self.loading_label.set_halign(Gtk.Align.CENTER)
        self.loading_label.set_valign(Gtk.Align.CENTER)

        self.widget = Gtk.Overlay()
        self.widget.set_child(picture)
        self.widget.add_overlay(self.loading_label)
        self.widget.set_hexpand(True)
        self.widget.set_vexpand(True)
        self.widget.set_size_request(320, 180)

    def _on_pad_added(self, decodebin, pad):
        caps = pad.get_current_caps()
        if not caps:
            caps = pad.query_caps(None)
        name = caps.get_structure(0).get_name()
        if name.startswith("video/"):
            sink_pad = self.videoconvert.get_static_pad("sink")
            if not sink_pad.is_linked():
                pad.link(sink_pad)
        elif name.startswith("audio/"):
            sink_pad = self.audioconvert.get_static_pad("sink")
            if not sink_pad.is_linked():
                pad.link(sink_pad)

    def _on_element_added(self, decodebin, element):
        """Minimize buffering on internal queues created by decodebin."""
        factory = element.get_factory()
        if factory and factory.get_name() in ("queue", "queue2"):
            element.set_property("max-size-buffers", 1)
            element.set_property("max-size-time", 0)
            element.set_property("max-size-bytes", 0)

    def play(self):
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        bus.connect("message::eos", self._on_bus_eos)
        bus.connect("message::async-done", self._on_async_done)
        self._start_pipeline()

    def _start_pipeline(self):
        """(Re)start ffmpeg and set the GStreamer pipeline to PLAYING."""
        self.ffmpeg_proc = subprocess.Popen(
            [
                "ffmpeg", "-loglevel", "error",
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-probesize", "32768",
                "-analyzeduration", "0",
                "-rtsp_transport", "tcp",
                "-i", self.stream_url,
                "-c:v", "copy", "-c:a", "copy",
                "-f", "mpegts",
                "-muxdelay", "0",
                "-muxpreload", "0",
                "pipe:1",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.fdsrc.set_property("fd", self.ffmpeg_proc.stdout.fileno())
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        if self._reconnect_id is not None:
            GLib.source_remove(self._reconnect_id)
            self._reconnect_id = None
        self.pipeline.set_state(Gst.State.NULL)
        if self.ffmpeg_proc:
            self.ffmpeg_proc.kill()
            self.ffmpeg_proc.wait()
            self.ffmpeg_proc = None

    def _on_async_done(self, bus, msg):
        """Hide loading label once pipeline is streaming."""
        if self.loading_label:
            self.loading_label.set_visible(False)

    def _on_bus_eos(self, bus, msg):
        print("[stream] EOS — stream ended, reconnecting...", flush=True)
        self._schedule_reconnect()

    def _on_bus_error(self, bus, msg):
        err, debug = msg.parse_error()
        print(f"[stream] ERROR: {err.message}", flush=True)
        if debug:
            print(f"[stream] DEBUG: {debug}", flush=True)
        self._schedule_reconnect()

    def _schedule_reconnect(self):
        if self._reconnect_id is None:
            self._reconnect_id = GLib.timeout_add_seconds(2, self._do_reconnect)

    def _do_reconnect(self):
        self._reconnect_id = None
        self.restart()
        return GLib.SOURCE_REMOVE

    def restart(self):
        """Tear down and restart the pipeline (e.g. after suspend/resume)."""
        if self.ffmpeg_proc:
            self.ffmpeg_proc.kill()
            self.ffmpeg_proc.wait()
            self.ffmpeg_proc = None
        self.pipeline.set_state(Gst.State.NULL)
        if self.loading_label:
            self.loading_label.set_visible(True)
        self._start_pipeline()


_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <method name="Activate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="Scroll">
      <arg type="i" name="delta" direction="in"/>
      <arg type="s" name="orientation" direction="in"/>
    </method>
    <property name="Id" type="s" access="read"/>
    <property name="Category" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <signal name="NewStatus"><arg type="s" name="status"/></signal>
    <signal name="NewIcon"/>
    <signal name="NewTitle"/>
  </interface>
</node>
"""


class SystemTrayIcon:
    """StatusNotifierItem tray icon via GIO D-Bus (no extra dependencies).

    Registers the org.kde.StatusNotifierItem D-Bus interface and notifies
    the StatusNotifierWatcher (e.g. waybar tray) of its presence.
    Falls back gracefully if no watcher is running.
    """

    def __init__(self, on_activate):
        self._on_activate = on_activate
        self._status = "Passive"
        self._conn = None
        self._reg_id = None
        self._service_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.available = False

        # Set up a minimal freedesktop icon theme so the tray can load the SVG
        self._icon_name = "unifi-cams"
        self._icon_theme_path = "/tmp/unifi-cams-icons"
        icon_dir = Path(self._icon_theme_path) / "hicolor" / "scalable" / "apps"
        icon_dir.mkdir(parents=True, exist_ok=True)
        icon_link = icon_dir / "unifi-cams.svg"
        src = SCRIPT_DIR / "assets" / "UI.svg"
        if not icon_link.exists() and src.exists():
            icon_link.symlink_to(src)

        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as e:
            print(f"[tray] D-Bus session bus unavailable: {e}", flush=True)
            return

        # Own a well-known bus name
        self._conn.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "RequestName",
            GLib.Variant("(su)", (self._service_name, 0)),
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE, -1, None,
        )

        # Register the StatusNotifierItem object
        node_info = Gio.DBusNodeInfo.new_for_xml(_SNI_XML)
        iface_info = node_info.lookup_interface("org.kde.StatusNotifierItem")
        self._reg_id = self._conn.register_object(
            "/StatusNotifierItem",
            iface_info,
            self._handle_method_call,
            self._handle_get_property,
            None,
        )

        # Register with the watcher (may not be running)
        try:
            self._conn.call_sync(
                "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self._service_name,)),
                None, Gio.DBusCallFlags.NONE, 1000, None,
            )
            self.available = True
            print("[tray] Registered with StatusNotifierWatcher", flush=True)
        except Exception as e:
            print(f"[tray] No StatusNotifierWatcher ({e}) — minimize will iconify instead", flush=True)

    def _handle_method_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "Activate":
            GLib.idle_add(self._on_activate)
            invocation.return_value(None)
        elif method in ("SecondaryActivate", "Scroll"):
            invocation.return_value(None)
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", method
            )

    def _handle_get_property(self, conn, sender, path, iface, prop):
        return {
            "Id":              GLib.Variant("s", "unifi-cams"),
            "Category":        GLib.Variant("s", "ApplicationStatus"),
            "Status":          GLib.Variant("s", self._status),
            "Title":           GLib.Variant("s", "Camera Dashboard"),
            "IconName":        GLib.Variant("s", self._icon_name),
            "IconThemePath":   GLib.Variant("s", self._icon_theme_path),
            "Menu":            GLib.Variant("o", "/"),
            "ItemIsMenu":      GLib.Variant("b", False),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip":         GLib.Variant("(sa(iiay)ss)", ("", [], "Camera Dashboard", "")),
        }.get(prop)

    def _emit_new_status(self, status):
        if self._conn and self._reg_id:
            self._conn.emit_signal(
                None, "/StatusNotifierItem",
                "org.kde.StatusNotifierItem", "NewStatus",
                GLib.Variant("(s)", (status,)),
            )

    def show(self):
        self._status = "Active"
        self._emit_new_status("Active")

    def hide(self):
        self._status = "Passive"
        self._emit_new_status("Passive")

    def unregister(self):
        if self._reg_id and self._conn:
            self._conn.unregister_object(self._reg_id)
            self._reg_id = None
        if self._conn:
            try:
                self._conn.call_sync(
                    "org.freedesktop.DBus", "/org/freedesktop/DBus",
                    "org.freedesktop.DBus", "ReleaseName",
                    GLib.Variant("(s)", (self._service_name,)),
                    None, Gio.DBusCallFlags.NONE, -1, None,
                )
            except Exception:
                pass


class SettingsView(Gtk.Box):
    """Settings form that reads/writes the .env file."""

    def __init__(self, on_save, on_cancel):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._on_save_cb = on_save
        self._on_cancel_cb = on_cancel
        self._entries = {}  # env_key -> Gtk.Entry or Gtk.Switch

        # Header
        header = Gtk.Label(label="Settings")
        header.add_css_class("title-2")
        header.set_margin_top(12)
        header.set_margin_bottom(8)
        self.append(header)
        self.append(Gtk.Separator())

        # Scrollable form
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        form.set_margin_start(16)
        form.set_margin_end(16)
        form.set_margin_top(12)
        form.set_margin_bottom(12)

        for env_key, label_text, field_type, default in SETTINGS_FIELDS:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            label = Gtk.Label(label=label_text)
            label.set_xalign(0)
            label.set_size_request(160, -1)
            row.append(label)

            if field_type == "toggle":
                widget = Gtk.Switch()
                widget.set_halign(Gtk.Align.START)
                widget.set_valign(Gtk.Align.CENTER)
            else:
                widget = Gtk.Entry()
                widget.set_hexpand(True)
                if field_type == "password":
                    widget.set_visibility(False)
                    widget.set_input_purpose(Gtk.InputPurpose.PASSWORD)
                elif field_type == "number":
                    widget.set_input_purpose(Gtk.InputPurpose.NUMBER)

            row.append(widget)
            self._entries[env_key] = widget
            form.append(row)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(form)
        self.append(scroll)

        # Bottom bar
        self.append(Gtk.Separator())
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(8)
        btn_box.set_margin_bottom(8)
        btn_box.set_margin_end(16)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self._on_cancel_cb())
        btn_box.append(cancel_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", lambda _: self._do_save())
        btn_box.append(save_btn)

        self.append(btn_box)

    def populate(self):
        """Load current .env values into the form."""
        settings, _ = load_dotenv(ENV_PATH)
        for env_key, _, field_type, default in SETTINGS_FIELDS:
            value = settings.get(env_key, default)
            widget = self._entries[env_key]
            if field_type == "toggle":
                widget.set_active(value.lower() in ("1", "true", "yes"))
            else:
                widget.set_text(value)

    def _do_save(self):
        """Collect form values and write to .env."""
        _, original_lines = load_dotenv(ENV_PATH)
        new_settings = {}
        for env_key, _, field_type, _ in SETTINGS_FIELDS:
            widget = self._entries[env_key]
            if field_type == "toggle":
                new_settings[env_key] = "1" if widget.get_active() else "0"
            else:
                new_settings[env_key] = widget.get_text()
        save_dotenv(ENV_PATH, new_settings, original_lines)
        self._on_save_cb()


class CamDashboard(Gtk.ApplicationWindow):
    def __init__(self, app, cameras):
        super().__init__(application=app, title="Camera Dashboard")
        self.set_default_size(1000, 400)

        self.cameras = cameras  # {mac: {"name": ..., "stream": ...}}
        self.streams = {}  # mac -> CameraStream
        self.sliders = {}  # mac -> Gtk.Scale
        self.tray = SystemTrayIcon(on_activate=self._show_window)

        # Stack for dashboard / settings views
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(200)
        self.set_child(self.stack)

        # Main vertical layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.stack.add_named(vbox, "dashboard")

        # --- Stream area (top, expandable) ---
        self.stream_area = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=4
        )
        self.stream_area.set_homogeneous(True)
        self.stream_area.set_vexpand(True)
        self.stream_area.set_hexpand(True)

        stream_frame = Gtk.Frame()
        stream_frame.set_child(self.stream_area)
        vbox.append(stream_frame)

        # --- Camera list (bottom) ---
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)

        for mac, info in cameras.items():
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hbox.set_margin_start(8)
            hbox.set_margin_end(8)
            hbox.set_margin_top(4)
            hbox.set_margin_bottom(4)

            # Checkbox to toggle stream
            check = Gtk.CheckButton()
            check.connect("toggled", self._on_stream_toggle, mac)
            hbox.append(check)

            # Camera name label
            label = Gtk.Label(label=info["name"])
            label.set_xalign(0)
            label.set_hexpand(True)
            hbox.append(label)

            # Volume slider (muted + insensitive until stream is active)
            slider = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05
            )
            slider.set_value(0.0)
            slider.set_draw_value(False)
            slider.set_size_request(120, -1)
            slider.set_sensitive(False)
            slider.connect("value-changed", self._on_volume_changed, mac)
            self.sliders[mac] = slider
            hbox.append(slider)

            # PIP button
            pip_btn = Gtk.Button(label="PIP")
            pip_btn.connect("clicked", self._on_pip_clicked, info["stream"])
            hbox.append(pip_btn)

            row.set_child(hbox)
            listbox.append(row)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(120)
        scroll.set_child(listbox)

        list_frame = Gtk.Frame()
        list_frame.set_child(scroll)
        vbox.append(list_frame)

        # Gear button row
        gear_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        gear_row.set_halign(Gtk.Align.END)
        gear_row.set_margin_end(8)
        gear_row.set_margin_top(4)
        gear_row.set_margin_bottom(4)
        gear_btn = Gtk.Button()
        gear_btn.set_icon_name("emblem-system-symbolic")
        gear_btn.add_css_class("flat")
        gear_btn.connect("clicked", self._on_settings_clicked)
        gear_row.append(gear_btn)

        min_btn = Gtk.Button()
        min_btn.set_icon_name("window-minimize-symbolic")
        min_btn.add_css_class("flat")
        min_btn.connect("clicked", self._on_minimize_clicked)
        gear_row.append(min_btn)

        close_btn = Gtk.Button()
        close_btn.set_icon_name("window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.connect("clicked", lambda _: self.get_application().quit())
        gear_row.append(close_btn)

        vbox.append(gear_row)

        # Settings view
        self.settings_view = SettingsView(
            on_save=self._on_settings_save,
            on_cancel=self._on_settings_cancel,
        )
        self.stack.add_named(self.settings_view, "settings")

    def _show_window(self):
        self.set_visible(True)
        self.present()
        self.tray.hide()

    def _on_minimize_clicked(self, _):
        if self.tray.available:
            self.set_visible(False)
            self.tray.show()
        else:
            self.iconify()

    def _on_settings_clicked(self, _button):
        self.settings_view.populate()
        self.stack.set_visible_child_name("settings")

    def _on_settings_save(self):
        _restart_notify()
        self.stack.set_visible_child_name("dashboard")

    def _on_settings_cancel(self):
        self.stack.set_visible_child_name("dashboard")

    def _on_stream_toggle(self, check, mac):
        if check.get_active():
            self._start_stream(mac)
        else:
            self._stop_stream(mac)

    def _start_stream(self, mac):
        if mac in self.streams:
            return
        info = self.cameras[mac]
        stream = CameraStream(info["stream"])
        self.streams[mac] = stream
        self.stream_area.append(stream.widget)
        stream.play()
        self.sliders[mac].set_sensitive(True)

    def _stop_stream(self, mac):
        stream = self.streams.pop(mac, None)
        if stream is None:
            return
        stream.stop()
        self.stream_area.remove(stream.widget)
        self.sliders[mac].set_value(0.0)
        self.sliders[mac].set_sensitive(False)

    def _on_volume_changed(self, slider, mac):
        stream = self.streams.get(mac)
        if stream:
            stream.volume.set_property("volume", slider.get_value())

    def _on_pip_clicked(self, button, stream_url):
        subprocess.Popen(
            [str(SCRIPT_DIR / "unifi-stream.sh"), stream_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def do_close_request(self):
        for stream in self.streams.values():
            stream.stop()
        self.streams.clear()
        self.tray.unregister()
        return False  # allow close to proceed


class CamDashboardApp(Gtk.Application):
    def __init__(self, cameras):
        super().__init__(application_id="com.local.unifi-cams")
        self.cameras = cameras
        self._sleep_sub = None

    def do_activate(self):
        win = CamDashboard(self, self.cameras)
        win.present()
        self._subscribe_sleep()

    def _subscribe_sleep(self):
        """Listen for system suspend/resume via logind PrepareForSleep."""
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._sleep_sub = conn.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                "/org/freedesktop/login1",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_sleep,
            )
            print("[dashboard] Subscribed to sleep/wake signals", flush=True)
        except Exception as e:
            print(f"[dashboard] Could not subscribe to sleep signals: {e}", flush=True)

    def _on_prepare_for_sleep(self, conn, sender, path, iface, signal, params):
        going_to_sleep = params.unpack()[0]
        if not going_to_sleep:
            print("[dashboard] System resumed — restarting streams", flush=True)
            GLib.idle_add(self._restart_all_streams)

    def _restart_all_streams(self):
        win = self.get_active_window()
        if not win:
            return
        for stream in win.streams.values():
            stream.restart()
        print(f"[dashboard] Restarted {len(win.streams)} stream(s)", flush=True)


def main():
    cameras = load_cameras()
    if not cameras:
        print("No cameras found in .env UNIFI_CAMERAS", file=sys.stderr)
        raise SystemExit(1)

    app = CamDashboardApp(cameras)
    app.run(None)


if __name__ == "__main__":
    main()
