#!/usr/bin/env python
"""Wayland capture proof of concept: XDG ScreenCast portal + PipeWire.

Priority 0, step 8. Native Wayland has no equivalent of `XGetImage`: a
client cannot capture a window it does not own. The supported route is the
XDG ScreenCast portal, which asks the user to pick a source and hands back
a PipeWire node id to read frames from.

This is a standalone probe, not part of the watcher. It exists to answer
three questions before any of it is wired into `watcher.py`:

1. Does the KDE portal grant a *window* stream, not just a whole monitor?
2. What does a frame cost compared with the XCB path already in use?
3. Are the pixels usable - right size, right format, right window?

Run it directly:

    python tools/portal_poc.py [--frames N] [--save out.png]

It will open a KDE dialog asking which window or screen to share. That
consent step is not bypassable, and that is the point: it is the boundary
that makes arbitrary window capture safe on Wayland. Any design that needs
capture to start unattended has to account for a restore token, which this
probe also prints when the portal provides one.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib
    import dbus
    import dbus.mainloop.glib
except ImportError as e:                         # pragma: no cover - probe
    sys.exit(f"missing dependency: {e}\n"
             "needs python-gobject, gst-plugin-pipewire, python-dbus")


PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"

#: Portal source types. 1=monitor, 2=window, 4=virtual.
SOURCE_MONITOR, SOURCE_WINDOW = 1, 2


class PortalSession:
    """One ScreenCast session, from request to PipeWire node id.

    The portal API is a chain of asynchronous request objects: every call
    returns an object path that later emits a `Response` signal. Each step
    must complete before the next begins, so this walks them in order and
    drives a GLib main loop in between.
    """

    def __init__(self, source_types: int = SOURCE_WINDOW | SOURCE_MONITOR):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SessionBus()
        self.portal = self.bus.get_object(PORTAL, PORTAL_PATH)
        self.screencast = dbus.Interface(self.portal, SCREENCAST)
        self.source_types = source_types
        self.loop = GLib.MainLoop()
        self.session_path: str | None = None
        self.node_id: int | None = None
        self.restore_token: str | None = None
        self._result: dict | None = None
        self._token = 0

    # -- plumbing ----------------------------------------------------------

    def _handle(self) -> tuple[str, str]:
        """A unique request token and the object path it will appear at."""
        self._token += 1
        token = f"screenwatcher{self._token}"
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        return token, f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    def _await(self, path: str, timeout: int = 120) -> dict:
        """Block until the request at `path` responds."""
        self._result = None

        def on_response(code, results):
            self._result = {"code": int(code), "results": dict(results)}
            self.loop.quit()

        match = self.bus.add_signal_receiver(
            on_response, "Response", "org.freedesktop.portal.Request",
            PORTAL, path)
        source = GLib.timeout_add_seconds(timeout,
                                          lambda: (self.loop.quit(), False)[1])
        self.loop.run()
        GLib.source_remove(source)
        match.remove()
        if self._result is None:
            raise TimeoutError(f"portal request timed out: {path}")
        return self._result

    # -- the three portal steps -------------------------------------------

    def create(self) -> None:
        token, path = self._handle()
        self.screencast.CreateSession({
            "session_handle_token": f"sw{self._token}",
            "handle_token": token,
        })
        r = self._await(path)
        if r["code"] != 0:
            raise RuntimeError(f"CreateSession refused (code {r['code']})")
        self.session_path = str(r["results"]["session_handle"])

    def select(self, restore_token: str | None = None) -> None:
        """Ask the user to choose a source.

        `persist_mode=2` requests a restore token so a later run can skip the
        dialog. KDE may still decline to issue one, which is why the caller
        must treat its absence as normal rather than an error.
        """
        token, path = self._handle()
        opts = {
            "handle_token": token,
            "types": dbus.UInt32(self.source_types),
            "multiple": False,
            "cursor_mode": dbus.UInt32(1),       # 1 = hidden
            "persist_mode": dbus.UInt32(2),      # 2 = persist until revoked
        }
        if restore_token:
            opts["restore_token"] = restore_token
        self.screencast.SelectSources(self.session_path, opts)
        r = self._await(path)
        if r["code"] != 0:
            raise RuntimeError("source selection cancelled "
                               f"(code {r['code']})")

    def start(self) -> int:
        token, path = self._handle()
        self.screencast.Start(self.session_path, "", {"handle_token": token})
        r = self._await(path)
        if r["code"] != 0:
            raise RuntimeError(f"Start refused (code {r['code']})")
        results = r["results"]
        self.restore_token = (str(results["restore_token"])
                              if "restore_token" in results else None)
        streams = results.get("streams") or []
        if not streams:
            raise RuntimeError("portal returned no streams")
        self.node_id = int(streams[0][0])
        props = dict(streams[0][1])
        size = props.get("size")
        print(f"stream node={self.node_id} "
              f"size={tuple(size) if size else 'unknown'}")
        return self.node_id

    def open(self) -> int:
        """Run all three steps and return the PipeWire node id."""
        self.create()
        self.select()
        return self.start()


class PipeWireReader:
    """Pull frames off a PipeWire node as numpy arrays via GStreamer.

    `appsink` with `max-buffers=1, drop=true` is deliberate: the watcher
    polls on its own schedule and only ever wants the newest frame, so
    queued history is pure latency.
    """

    def __init__(self, node_id: int):
        Gst.init(None)
        self.pipeline = Gst.parse_launch(
            f"pipewiresrc path={node_id} ! videoconvert ! "
            "video/x-raw,format=RGB ! "
            "appsink name=sink max-buffers=1 drop=true sync=false")
        self.sink = self.pipeline.get_by_name("sink")
        self.pipeline.set_state(Gst.State.PLAYING)

    def frame(self, timeout: float = 5.0) -> np.ndarray | None:
        sample = self.sink.emit("try-pull-sample", int(timeout * Gst.SECOND))
        if sample is None:
            return None
        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            # PipeWire pads rows to a stride; deriving it from the buffer
            # rather than assuming w*3 avoids a sheared image on widths
            # that are not a multiple of the alignment.
            stride = info.size // h
            arr = np.frombuffer(info.data, dtype=np.uint8, count=h * stride)
            return arr.reshape(h, stride)[:, :w * 3].reshape(h, w, 3).copy()
        finally:
            buf.unmap(info)

    def close(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames", type=int, default=20,
                    help="frames to time (default 20)")
    ap.add_argument("--save", help="write the first frame here")
    args = ap.parse_args()

    print("opening portal - KDE will ask which window or screen to share")
    session = PortalSession()
    try:
        node = session.open()
    except Exception as e:
        print(f"portal failed: {type(e).__name__}: {e}")
        return 1
    if session.restore_token:
        print(f"restore token: {session.restore_token}")
    else:
        print("no restore token issued (consent needed again next run)")

    reader = PipeWireReader(node)
    try:
        first = reader.frame()
        if first is None:
            print("no frame arrived; is the stream active?")
            return 1
        print(f"first frame: {first.shape} {first.dtype}")
        if args.save:
            from PIL import Image
            Image.fromarray(first).save(args.save)
            print(f"saved {args.save}")

        times = []
        for _ in range(args.frames):
            t = time.perf_counter()
            if reader.frame(timeout=2.0) is None:
                break
            times.append((time.perf_counter() - t) * 1000)
        if times:
            times.sort()
            print(f"frames: n={len(times)} "
                  f"min={times[0]:.1f} median={times[len(times) // 2]:.1f} "
                  f"max={times[-1]:.1f} ms")
    finally:
        reader.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
