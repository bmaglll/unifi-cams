#!/usr/bin/env python3
"""Camera Dashboard — GTK4 + GStreamer live camera viewer.

Displays live RTSPS camera streams using GStreamer's playbin +
gtk4paintablesink for GPU-accelerated video rendering. Bottom panel
lists cameras with checkboxes (toggle stream) and PIP buttons (launch mpv).
"""

import json
import subprocess
import sys
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Gdk, Gst, GLib

SCRIPT_DIR = Path(__file__).resolve().parent

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
        # Launch ffmpeg: reads RTSPS, outputs mpegts to stdout (no re-encode)
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

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        bus.connect("message::async-done", self._on_async_done)

        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)
        if self.ffmpeg_proc:
            self.ffmpeg_proc.kill()
            self.ffmpeg_proc.wait()
            self.ffmpeg_proc = None

    def _on_async_done(self, bus, msg):
        """Hide loading label once pipeline is streaming."""
        if self.loading_label:
            self.loading_label.set_visible(False)

    @staticmethod
    def _on_bus_error(bus, msg):
        err, debug = msg.parse_error()
        print(f"[stream] ERROR: {err.message}", flush=True)
        if debug:
            print(f"[stream] DEBUG: {debug}", flush=True)


class CamDashboard(Gtk.ApplicationWindow):
    def __init__(self, app, cameras):
        super().__init__(application=app, title="Camera Dashboard")
        self.set_default_size(1000, 400)

        self.cameras = cameras  # {mac: {"name": ..., "stream": ...}}
        self.streams = {}  # mac -> CameraStream
        self.sliders = {}  # mac -> Gtk.Scale

        # Main vertical layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(vbox)

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
        return False  # allow close to proceed


class CamDashboardApp(Gtk.Application):
    def __init__(self, cameras):
        super().__init__(application_id="com.local.unifi-cams")
        self.cameras = cameras

    def do_activate(self):
        win = CamDashboard(self, self.cameras)
        win.present()


def main():
    cameras = load_cameras()
    if not cameras:
        print("No cameras found in .env UNIFI_CAMERAS", file=sys.stderr)
        raise SystemExit(1)

    app = CamDashboardApp(cameras)
    app.run(None)


if __name__ == "__main__":
    main()
