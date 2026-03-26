#!/usr/bin/env bash
# unifi-dashboard.sh — Launch the camera dashboard GUI
# Wraps with nix-shell to provide GTK4 + GStreamer + PyGObject dependencies.
# Bind this to a Hyprland keybind for quick access.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

exec nix-shell -p \
  python3Packages.pygobject3 \
  gtk4 gdk-pixbuf gobject-introspection \
  gst_all_1.gstreamer \
  gst_all_1.gst-plugins-base \
  gst_all_1.gst-plugins-good \
  gst_all_1.gst-plugins-bad \
  gst_all_1.gst-plugins-ugly \
  gst_all_1.gst-libav \
  gst_all_1.gst-plugins-rs \
  --run "python3 $SCRIPT_DIR/unifi-dashboard.py"
