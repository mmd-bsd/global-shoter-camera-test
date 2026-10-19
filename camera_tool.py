#!/usr/bin/env python3
"""
High-FPS UVC camera console for Windows.

Features
--------
* Live preview from a DirectShow (USB/UVC) camera.
* Real measured FPS from returned-frame timestamps (frame interval, jitter, late frames).
* Fixed-rate software output: e.g. select 100 fps; the camera runs at its native 120
  and the app publishes frames on a precise 100.000 Hz schedule. Both the raw camera
  rate and the fixed output rate are displayed.
* Every native DirectShow/UVC control exposed by the camera, with real min/max/step/
  default and live values: brightness, contrast, hue, saturation, sharpness, gamma,
  white balance, backlight, gain, exposure, plus focus/zoom/etc. when supported.
* Auto/manual switching for exposure and white balance (via IAMCameraControl /
  IAMVideoProcAmp while the stream is running).
* Global-shutter verification: short-exposure burst capture of a fast-moving vertical
  edge, automatic edge-tilt analysis (rolling shutter shears the edge back and forth),
  and slow-motion frame replay for a definitive visual check.

Run:
    python camera_tool.py
    python camera_tool.py --self-test

Requires:
    pip install opencv-python numpy pillow pygrabber comtypes
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import math
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from ctypes import POINTER, c_long, cast
from ctypes.wintypes import DWORD, RECT
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageTk

import tkinter as tk
from tkinter import ttk, messagebox

from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
from pygrabber.dshow_core import BITMAPINFOHEADER, IAMStreamConfig, VIDEOINFOHEADER
from pygrabber.dshow_graph import (
    DeviceCategories,
    FilterGraph,
    FilterType,
    SystemDeviceEnum,
    show_properties,
)
from pygrabber.dshow_ids import FormatTypes, subtypes


# --------------------------------------------------------------------------------------
# DirectShow control interfaces (video proc amp + camera control)
# --------------------------------------------------------------------------------------

class IAMVideoProcAmp(IUnknown):
    _iid_ = GUID("{C6E13360-30AC-11D0-A18C-00A0C9118956}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetRange",
                  (["in"], c_long, "Property"),
                  (["out"], POINTER(c_long), "pMin"),
                  (["out"], POINTER(c_long), "pMax"),
                  (["out"], POINTER(c_long), "pSteppingDelta"),
                  (["out"], POINTER(c_long), "pDefault"),
                  (["out"], POINTER(c_long), "pCapsFlags")),
        COMMETHOD([], HRESULT, "Set",
                  (["in"], c_long, "Property"),
                  (["in"], c_long, "lValue"),
                  (["in"], c_long, "Flags")),
        COMMETHOD([], HRESULT, "Get",
                  (["in"], c_long, "Property"),
                  (["out"], POINTER(c_long), "lValue"),
                  (["out"], POINTER(c_long), "Flags")),
    ]


class IAMCameraControl(IUnknown):
    _iid_ = GUID("{C6E13370-30AC-11D0-A18C-00A0C9118956}")
    _methods_ = IAMVideoProcAmp._methods_


VPA_CONTROLS = [
    (0, "Brightness"), (1, "Contrast"), (2, "Hue"), (3, "Saturation"),
    (4, "Sharpness"), (5, "Gamma"), (6, "Color Enable"), (7, "White Balance"),
    (8, "Backlight"), (9, "Gain"),
]
CAM_CONTROLS = [
    (0, "Pan"), (1, "Tilt"), (2, "Roll"), (3, "Zoom"),
    (4, "Exposure"), (5, "Iris"), (6, "Focus"),
]
ALL_CONTROL_NAMES = [name for _, name in VPA_CONTROLS + CAM_CONTROLS]


@dataclass
class ControlInfo:
    group: str          # "video" or "camera"
    prop: int
    label: str
    minimum: int
    maximum: int
    step: int
    default: int
    caps: int           # bit 0 = auto supported, bit 1 = manual supported
    value: int
    flags: int          # 1 = auto, 2 = manual

    @property
    def supports_auto(self) -> bool:
        return bool(self.caps & 1)

    @property
    def supports_manual(self) -> bool:
        return bool(self.caps & 2) or not (self.caps & 1)


class DShowControls:
    """Second DirectShow filter instance used to read/write UVC controls live.

    Opening a second (non-streaming) filter instance works while OpenCV streams from
    the same device, so controls can be changed without touching the capture path.
    """

    def __init__(self, device_index: int):
        self._sde = SystemDeviceEnum()
        self._filter, self.device_name = self._sde.get_filter_by_index(
            DeviceCategories.VideoInputDevice, device_index)
        self.vpa = None
        self.cam = None
        try:
            self.vpa = self._filter.QueryInterface(IAMVideoProcAmp)
        except Exception:
            pass
        try:
            self.cam = self._filter.QueryInterface(IAMCameraControl)
        except Exception:
            pass

    def _iface(self, group: str):
        iface = self.vpa if group == "video" else self.cam
        if iface is None:
            raise RuntimeError(f"{group} controls not available")
        return iface

    def discover(self) -> list[ControlInfo]:
        found: list[ControlInfo] = []
        for group, iface, defs in (("video", self.vpa, VPA_CONTROLS),
                                   ("camera", self.cam, CAM_CONTROLS)):
            if iface is None:
                continue
            for prop, label in defs:
                try:
                    mn, mx, step, dflt, caps = iface.GetRange(prop)
                    value, flags = iface.Get(prop)
                except Exception:
                    continue
                if step <= 0:
                    step = 1
                found.append(ControlInfo(group, prop, label, int(mn), int(mx),
                                         int(step), int(dflt), int(caps or 3),
                                         int(value), int(flags)))
        return found

    def get(self, group: str, prop: int) -> tuple[int, int]:
        value, flags = self._iface(group).Get(prop)
        return int(value), int(flags)

    def set_flag(self, group: str, prop: int, auto: bool, value: int = 0) -> None:
        # DirectShow flags: 1 = auto, 2 = manual
        self._iface(group).Set(int(prop), int(value), 1 if auto else 2)

    def show_property_page(self) -> None:
        show_properties(self._filter)

    def close(self) -> None:
        self.vpa = None
        self.cam = None
        self._filter = None
        self._sde = None
        gc.collect()


# --------------------------------------------------------------------------------------
# Device / format enumeration
# --------------------------------------------------------------------------------------

class VIDEOINFOHEADER2(ctypes.Structure):
    _fields_ = (
        ("source", RECT), ("target", RECT),
        ("bit_rate", DWORD), ("bit_error_rate", DWORD),
        ("avg_time_per_frame", ctypes.c_longlong),
        ("dwInterlaceFlags", DWORD), ("dwCopyProtectFlags", DWORD),
        ("dwPictAspectRatioX", DWORD), ("dwPictAspectRatioY", DWORD),
        ("dwControlFlags", DWORD), ("dwReserved2", DWORD),
        ("bmi_header", BITMAPINFOHEADER),
    )


def enumerate_devices() -> list[str]:
    try:
        graph = FilterGraph()
        names = list(graph.get_input_devices())
        del graph
        gc.collect()
        return names
    except Exception:
        return []


def enumerate_formats(device_index: int) -> list[dict]:
    """Return unique (fourcc, width, height) modes with their fps range."""
    graph = FilterGraph()
    vi = None
    sc = None
    formats: list[dict] = []
    try:
        graph.add_video_input_device(device_index)
        vi = graph.filters[FilterType.video_input]
        sc = vi.get_out().QueryInterface(IAMStreamConfig)
        count, _ = sc.GetNumberOfCapabilities()
        seen = set()
        for i in range(count):
            mt, cap = sc.GetStreamCaps(i)
            fourcc = subtypes.get(str(mt.contents.subtype))
            if not fourcc:
                continue
            fmt_guid = str(mt.contents.formattype)
            try:
                if fmt_guid == str(GUID(FormatTypes.FORMAT_VideoInfo)):
                    vih = cast(mt.contents.pbFormat, POINTER(VIDEOINFOHEADER)).contents
                elif fmt_guid == str(GUID(FormatTypes.FORMAT_VideoInfo2)):
                    vih = cast(mt.contents.pbFormat, POINTER(VIDEOINFOHEADER2)).contents
                else:
                    continue
            except Exception:
                continue
            w = int(vih.bmi_header.biWidth)
            h = abs(int(vih.bmi_header.biHeight))
            avg = int(vih.avg_time_per_frame)
            fps_default = 1e7 / avg if avg else 0.0
            fps_min = 1e7 / cap.MaxFrameInterval if cap.MaxFrameInterval else fps_default
            fps_max = 1e7 / cap.MinFrameInterval if cap.MinFrameInterval else fps_default
            key = (fourcc, w, h)
            if key in seen:
                continue
            seen.add(key)
            formats.append({
                "fourcc": fourcc, "width": w, "height": h,
                "fps_min": float(fps_min), "fps_max": float(fps_max),
                "fps_default": float(fps_default),
            })
    finally:
        sc = None
        vi = None
        try:
            graph.remove_filters()
        except Exception:
            pass
        graph = None
        gc.collect()
    formats.sort(key=lambda f: (f["width"] * f["height"], f["fps_max"]), reverse=True)
    return formats


# --------------------------------------------------------------------------------------
# Capture worker, burst capture, skew analysis
# --------------------------------------------------------------------------------------

def exposure_text(value: float) -> str:
    """DirectShow exposure is log2(seconds)."""
    seconds = 2.0 ** value
    if seconds >= 1e-3:
        return f"{seconds * 1e3:.2f} ms"
    return f"{seconds * 1e6:.0f} us"


def ensure_test_target() -> str:
    """Create the printable target (A4 portrait, 150 dpi) if it is missing."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gs_target.png")
    if not os.path.exists(path):
        width, height = 1240, 1754
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        bar_x0, bar_x1 = int(width * 0.45), int(width * 0.55)
        bar_y0, bar_y1 = int(height * 0.12), int(height * 0.88)
        draw.rectangle([bar_x0, bar_y0, bar_x1, bar_y1], fill="black")
        try:
            title_font = ImageFont.truetype("arial.ttf", 42)
            small_font = ImageFont.truetype("arial.ttf", 30)
        except Exception:
            title_font = ImageFont.load_default()
            small_font = title_font
        draw.text((40, 40), "GLOBAL SHUTTER TEST TARGET", fill="black", font=title_font)
        draw.text((40, 104),
                  "Hold so the black bar fills the camera frame vertically.",
                  fill="black", font=small_font)
        draw.text((40, height - 90),
                  "Record a burst, then sweep left-right as fast as you can.",
                  fill="black", font=small_font)
        image.save(path)
    return path


ANALYSIS_WIDTH = 640


def detect_bar(frame: np.ndarray) -> Optional[dict]:
    """Find the solid, high-aspect, near-vertical black bar of the test target.

    Returns tilt (degrees from vertical, signed), bar centroid and the bar's
    principal axis.  Requiring one solid component avoids latching onto random
    background edges the way a Hough-line detector does.
    """
    h, w = frame.shape[:2]
    sw = min(ANALYSIS_WIDTH, w)
    sh = max(1, int(round(h * sw / w)))
    gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (sw, sh),
                      interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    image_area = float(sw * sh)
    best = None
    best_score = 0.0
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < 0.004 * image_area:
            continue
        _, _, _, box_h = cv2.boundingRect(contour)
        if box_h < 0.30 * sh:
            continue
        points = contour.reshape(-1, 2).astype(np.float32)
        if len(points) < 5:
            continue
        try:
            _, eigenvectors = cv2.PCACompute(points, mean=None)
        except cv2.error:
            continue
        vx, vy = float(eigenvectors[0][0]), float(eigenvectors[0][1])
        if abs(vy) < abs(vx):          # principal axis must be near vertical
            continue
        tilt = math.degrees(math.atan2(vx, vy))
        if abs(tilt) > 35.0:
            continue
        (cx, cy), (rw, rh), _ = cv2.minAreaRect(contour)
        long_side = max(rw, rh)
        short_side = max(1e-3, min(rw, rh))
        aspect = long_side / short_side
        if aspect < 3.0:               # must be a bar, not a blob
            continue
        fill = area / max(1e-6, long_side * short_side)
        if fill < 0.50:                # must be a solid bar
            continue
        score = area * min(aspect, 12.0)
        if score <= best_score:
            continue
        best_score = score
        rad = math.radians(tilt)
        half = long_side * 0.5
        best = {
            "tilt": tilt, "x": float(cx), "y": float(cy),
            "area": area, "aspect": aspect, "fill": fill,
            "long": long_side, "short": short_side,
            "line": (float(cx - math.sin(rad) * half),
                     float(cy - math.cos(rad) * half),
                     float(cx + math.sin(rad) * half),
                     float(cy + math.cos(rad) * half)),
            "scale": w / float(sw),
        }
    return best


class BarAnalyzer:
    """Tracks the target bar: tilt plus horizontal velocity between frames.

    Rolling shutter shears the bar in the direction of motion, so tilt and
    horizontal velocity become strongly correlated.  A global shutter keeps the
    bar's tilt independent of sweep direction.
    """

    def __init__(self):
        self._prev: Optional[tuple[float, float, float]] = None  # ts, x, y

    def analyze(self, frame: np.ndarray, ts: float) -> dict:
        bar = detect_bar(frame)
        vx = None
        if bar is not None:
            if self._prev is not None:
                prev_ts, prev_x, _ = self._prev
                dt = ts - prev_ts
                dx = bar["x"] - prev_x
                sw = min(ANALYSIS_WIDTH, frame.shape[1])
                # reject jumps (different object) and long gaps (missed detections)
                if 0.0 < dt <= 0.05 and abs(dx) <= 0.45 * sw:
                    vx = dx / dt
            self._prev = (ts, bar["x"], bar["y"])
        else:
            self._prev = None
        return {
            "found": bar is not None,
            "tilt": None if bar is None else bar["tilt"],
            "x": None if bar is None else bar["x"],
            "vx": vx,
            "line": None if bar is None else bar["line"],
            "area": 0.0 if bar is None else bar["area"],
            "aspect": 0.0 if bar is None else bar["aspect"],
            "scale": 1.0 if bar is None else bar["scale"],
        }


class BurstSession:
    """Collects a short burst of JPEG frames and per-frame tilt metrics."""

    def __init__(self, max_frames: int = 100, jpeg_quality: int = 92):
        self.max_frames = max_frames
        self.jpeg_quality = jpeg_quality
        self.queue: queue.Queue = queue.Queue(maxsize=40)
        self.frames: list[tuple[float, bytes]] = []
        self.metrics: list[dict] = []
        self.offered = 0
        self.dropped = 0
        self.done = threading.Event()
        self._finished = False
        self.started_at = time.perf_counter()
        self.writer = threading.Thread(target=self._writer, daemon=True)
        self.writer.start()

    def offer(self, frame: np.ndarray, ts: float) -> None:
        if self.offered >= self.max_frames:
            return
        try:
            self.queue.put_nowait((ts, frame))
            self.offered += 1
        except queue.Full:
            self.dropped += 1

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.queue.put(None)

    def _writer(self) -> None:
        analyzer = BarAnalyzer()
        while True:
            item = self.queue.get()
            if item is None:
                break
            ts, frame = item
            try:
                ok, buf = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
                if not ok:
                    continue
                metrics = analyzer.analyze(frame, ts)
                metrics["ts"] = ts
                self.frames.append((ts, buf.tobytes()))
                self.metrics.append(metrics)
            except Exception:
                continue
        self.done.set()


def evaluate_burst(metrics: list[dict]) -> tuple[str, str, dict]:
    """Return (verdict, explanation, statistics).

    A rolling shutter shears the target bar in the direction of horizontal motion,
    so the bar tilt correlates with the bar's horizontal velocity. A global shutter
    keeps the bar's tilt essentially constant, independent of sweep direction.
    """
    found = [m for m in metrics if m.get("found") and m.get("tilt") is not None]
    fast = [m for m in found
            if m.get("vx") is not None and abs(float(m["vx"])) >= 500.0]
    stats = {"found": len(found), "total": len(metrics), "n": len(fast)}
    if len(found) < 8 or len(fast) < 6:
        return ("inconclusive",
                "The test target was not tracked in enough frames, or the sweep was "
                "too slow. Hold the black bar so it fills most of the frame height, "
                "then sweep it left-right as fast as you can.",
                stats)
    tilts = np.array([float(m["tilt"]) for m in fast])
    vxs = np.array([float(m["vx"]) for m in fast])
    scales = np.array([float(m.get("scale", 1.0)) for m in fast])
    lo, hi = float(tilts.min()), float(tilts.max())
    span = hi - lo
    max_speed = float(np.max(np.abs(vxs)) * float(np.median(scales)))
    corr = 0.0
    if float(tilts.std()) > 1e-6 and float(vxs.std()) > 1e-6:
        corr = float(np.corrcoef(tilts, vxs)[0, 1])
    same = int(np.sum(tilts * vxs > 0))
    stats.update({"min": lo, "max": hi, "span": span, "max_speed": max_speed,
                  "corr": corr, "same": same, "n": len(fast)})
    if span >= 1.5 and abs(corr) >= 0.60 and max_speed >= 1200.0:
        return ("rolling",
                "The bar's tilt follows the sweep direction: tilt correlates with "
                f"horizontal speed (corr {corr:+.2f}, tilt span {span:.1f} deg). "
                "This is the rolling-shutter signature.",
                stats)
    if abs(corr) <= 0.40 and max_speed >= 1200.0:
        # Static tilt (hand-held angle) and rotation wobble are fine here: what
        # matters is that sweep-direction shear does not appear.
        return ("global",
                f"The bar's tilt is independent of sweep direction while moving fast "
                f"(corr {corr:+.2f}, tilt span {span:.1f} deg). No rolling-shutter "
                "shear was detected; this is consistent with a global shutter.",
                stats)
    return ("inconclusive",
            "The result is ambiguous. Keep the camera still, make sure the whole bar "
            "is visible and upright, and sweep faster left-right. Replay the frames "
            "to judge visually.",
            stats)


_high_res_timer = False


def enable_high_res_timer() -> None:
    """Request 1 ms Windows timer resolution for the fixed-rate publisher."""
    global _high_res_timer
    if _high_res_timer:
        return
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
        _high_res_timer = True
    except Exception:
        pass


def disable_high_res_timer() -> None:
    global _high_res_timer
    if not _high_res_timer:
        return
    try:
        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass
    _high_res_timer = False


class CaptureWorker:
    def __init__(self):
        self.cap: Optional[cv2.VideoCapture] = None
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.lock = threading.Lock()
        self.latest_frame: Optional[np.ndarray] = None
        self.latest_id = 0
        self.latest_ts = 0.0
        self.timestamps: deque[float] = deque(maxlen=500)      # raw camera frames
        self.decode_times: deque[float] = deque(maxlen=200)
        self.nominal_fps = 120.0                              # camera rate
        self.frame_count = 0                                  # raw frames
        self.error: Optional[str] = None
        self.burst: Optional[BurstSession] = None
        self.last_burst: Optional[BurstSession] = None

        # fixed-rate software output
        self.raw_frame: Optional[np.ndarray] = None
        self.raw_id = 0
        self.raw_ts = 0.0
        self.fixed_rate = 0.0
        self.output_rate = 0.0
        self.publisher_thread: Optional[threading.Thread] = None
        self.publisher_running = False
        self.publish_timestamps: deque[float] = deque(maxlen=500)
        self.publish_events: deque[tuple[float, bool]] = deque(maxlen=500)
        self.out_frame_count = 0
        self.dup_count = 0
        self._last_pub_raw_id = 0

    def open(self, device_index: int, fmt: dict, fps: float,
             fixed_rate: float = 0.0):
        self.close()
        cap = cv2.VideoCapture(device_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            return False, f"Cannot open camera index {device_index}.", None
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fmt["fourcc"]))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, fmt["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, fmt["height"])
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            return False, "Camera opened but did not deliver a frame.", None
        actual = {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps_reported": float(cap.get(cv2.CAP_PROP_FPS)),
            "fourcc": int(cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little")
                       .decode("ascii", "replace"),
            "camera_fps": float(fps),
            "fixed_rate": float(fixed_rate),
        }
        fixed_rate = float(fixed_rate)
        if fixed_rate >= fps - 0.01:
            fixed_rate = 0.0
        self.cap = cap
        self.nominal_fps = float(fps)
        self.fixed_rate = fixed_rate
        self.output_rate = fixed_rate if fixed_rate > 0 else float(fps)
        self.frame_count = 0
        self.out_frame_count = 0
        self.dup_count = 0
        self._last_pub_raw_id = 0
        self.timestamps.clear()
        self.publish_timestamps.clear()
        self.publish_events.clear()
        self.decode_times.clear()
        self.error = None
        self.raw_frame = frame
        self.raw_ts = time.perf_counter()
        self.raw_id += 1
        with self.lock:
            self.latest_frame = frame
            self.latest_ts = self.raw_ts
            self.latest_id += 1
            self.running = True
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            if self.fixed_rate > 0:
                self.publisher_running = True
                self.publisher_thread = threading.Thread(
                    target=self._publisher_loop, daemon=True)
                self.publisher_thread.start()
        return True, "", actual

    def _run(self) -> None:
        fails = 0
        while self.running and self.cap is not None:
            t0 = time.perf_counter()
            ok, frame = self.cap.read()
            t1 = time.perf_counter()
            if not ok or frame is None:
                fails += 1
                if fails > 40:
                    self.error = "Capture stopped: no frames received."
                    self.running = False
                    break
                time.sleep(0.003)
                continue
            fails = 0
            with self.lock:
                self.raw_frame = frame
                self.raw_ts = t1
                self.raw_id += 1
                self.frame_count += 1
                self.timestamps.append(t1)
                self.decode_times.append(t1 - t0)
                if self.fixed_rate <= 0:
                    self.latest_frame = frame
                    self.latest_ts = t1
                    self.latest_id += 1
                    self.publish_timestamps.append(t1)
                    self.publish_events.append((t1, False))
                    self.out_frame_count += 1
                burst = self.burst
            if burst is not None:
                burst.offer(frame, t1)
                if burst.offered >= burst.max_frames:
                    with self.lock:
                        if self.burst is burst:
                            self.burst = None
                        self.last_burst = burst
                    burst.finish()

    def _publisher_loop(self) -> None:
        """Publish the newest raw frame on a fixed schedule.

        The camera is opened at its native maximum (120 fps). This thread emits a
        frame every 1/fixed_rate seconds, using the newest frame available at each
        tick. Raw frames that arrive between ticks are skipped; if the camera stalls,
        the last frame is repeated (counted as a duplicate). The output timestamps
        are therefore fixed even when the USB stream jitters.

        A high-resolution waitable timer is used when available; time.sleep is only
        the fallback. The thread runs at elevated priority so decoding and the GUI
        cannot delay the ticks.
        """
        kernel32 = ctypes.windll.kernel32
        timer = None
        period = 1.0 / self.fixed_rate
        next_tick = time.perf_counter() + period
        try:
            kernel32.GetCurrentThread.restype = ctypes.c_void_p
            kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 2)  # HIGHEST
        except Exception:
            pass
        try:
            kernel32.CreateWaitableTimerExW.restype = ctypes.c_void_p
            kernel32.CreateWaitableTimerExW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong]
            kernel32.SetWaitableTimer.argtypes = [
                ctypes.c_void_p, POINTER(ctypes.c_longlong), ctypes.c_long,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            # CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS
            timer = kernel32.CreateWaitableTimerExW(None, None, 0x00000002, 0x001F0003)
            if not timer:
                timer = None
        except Exception:
            timer = None
        try:
            while self.publisher_running:
                if timer:
                    remaining = next_tick - time.perf_counter()
                    due = ctypes.c_longlong(-max(1, int(remaining * 1e7)))
                    kernel32.SetWaitableTimer(timer, ctypes.byref(due), 0,
                                              None, None, 0)
                    kernel32.WaitForSingleObject(timer, 0xFFFFFFFF)
                else:
                    delay = next_tick - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                tick = time.perf_counter()
                with self.lock:
                    frame = self.raw_frame
                    raw_id = self.raw_id
                    if frame is not None:
                        duplicate = raw_id == self._last_pub_raw_id
                        self.latest_frame = frame
                        self.latest_ts = tick
                        self.latest_id += 1
                        self.publish_timestamps.append(tick)
                        self.publish_events.append((tick, duplicate))
                        self.out_frame_count += 1
                        if duplicate:
                            self.dup_count += 1
                        self._last_pub_raw_id = raw_id
                next_tick += period
                if tick - next_tick > 0.05:
                    next_tick = tick + period
        finally:
            if timer:
                kernel32.CloseHandle(timer)

    def stats(self) -> dict:
        with self.lock:
            ts = list(self.timestamps)
            dec = list(self.decode_times)
            pub = list(self.publish_events)
            frames = self.frame_count
            out_frames = self.out_frame_count
            nominal = self.nominal_fps
            fixed_rate = self.fixed_rate
            output_rate = self.output_rate
        now = time.perf_counter()
        window = [t for t in ts if now - t <= 2.0]
        out = {"fps": 0.0, "interval_ms": 0.0, "jitter_ms": 0.0,
               "late": 0, "frames": frames, "decode_ms": 0.0,
               "nominal": nominal, "fixed_active": fixed_rate > 0,
               "output_rate": output_rate, "output_fps": 0.0,
               "out_interval_ms": 0.0, "out_jitter_ms": 0.0,
               "duplicates": 0, "out_frames": out_frames}
        if len(window) >= 3:
            dts = np.diff(window)
            out["fps"] = (len(window) - 1) / max(window[-1] - window[0], 1e-9)
            out["interval_ms"] = float(np.median(dts) * 1000.0)
            out["jitter_ms"] = float(np.std(dts) * 1000.0)
            if nominal > 0:
                out["late"] = int(np.sum(dts > 1.8 / nominal))
        if dec:
            out["decode_ms"] = float(np.median(dec) * 1000.0)
        pub_window = [e for e in pub if now - e[0] <= 2.0]
        if len(pub_window) >= 3:
            pts = np.array([e[0] for e in pub_window])
            pdts = np.diff(pts)
            out["output_fps"] = (len(pts) - 1) / max(pts[-1] - pts[0], 1e-9)
            out["out_interval_ms"] = float(np.median(pdts) * 1000.0)
            out["out_jitter_ms"] = float(np.std(pdts) * 1000.0)
            out["duplicates"] = int(sum(1 for e in pub_window if e[1]))
        return out

    def read_latest(self):
        with self.lock:
            return self.latest_frame, self.latest_id, self.latest_ts

    def start_burst(self, max_frames: int = 100) -> Optional[BurstSession]:
        if not self.running:
            return None
        session = BurstSession(max_frames=max_frames)
        with self.lock:
            self.burst = session
            self.last_burst = session
        return session

    def end_burst(self) -> Optional[BurstSession]:
        with self.lock:
            session = self.burst
            self.burst = None
            if session is not None:
                self.last_burst = session
        if session is not None:
            session.finish()
        return session

    def close(self) -> None:
        self.publisher_running = False
        pt = self.publisher_thread
        if pt is not None and pt.is_alive():
            pt.join(timeout=0.8)
        self.publisher_thread = None
        self.end_burst()
        self.running = False
        t = self.thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        cap = self.cap
        self.cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        with self.lock:
            self.latest_frame = None
            self.raw_frame = None
            self.latest_id += 1
            self.thread = None
            self.fixed_rate = 0.0
            self.output_rate = 0.0


# --------------------------------------------------------------------------------------
# UI helpers
# --------------------------------------------------------------------------------------

BG = "#101418"
PANEL = "#171c21"
FIELD = "#222930"
FG = "#e8edf2"
MUTED = "#93a0ab"
ACCENT = "#22e08a"
WARN = "#ffb02e"
BAD = "#ff5f6d"


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class ScrollFrame(ttk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.inner.bind("<MouseWheel>", self._wheel)

    def _on_inner(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class ControlRow:
    """One live UVC control: slider + exact value + auto/manual toggle."""

    def __init__(self, app: "CameraApp", parent, info: ControlInfo):
        self.app = app
        self.info = info
        self._updating = False
        self._last_user = 0.0
        self._pending: Optional[int] = None
        self._pending_job = None

        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="x", padx=8, pady=3)

        top = ttk.Frame(self.frame)
        top.pack(fill="x")
        ttk.Label(top, text=info.label, width=15).pack(side="left")

        self.var = tk.DoubleVar(value=info.value)
        self.text_var = tk.StringVar(value=str(info.value))
        self.auto_var = tk.BooleanVar(value=bool(info.flags & 1))

        self.scale = ttk.Scale(top, from_=info.minimum, to=info.maximum,
                               orient="horizontal", variable=self.var,
                               command=self._on_scale)
        self.scale.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.spin = ttk.Spinbox(top, from_=info.minimum, to=info.maximum,
                                increment=max(1, info.step), width=6,
                                textvariable=self.text_var, command=self._on_entry)
        self.spin.pack(side="left")
        self.spin.bind("<Return>", self._on_entry)
        self.spin.bind("<FocusOut>", self._on_entry)

        if info.supports_auto:
            self.auto_check = ttk.Checkbutton(top, text="Auto", variable=self.auto_var,
                                              command=self._on_auto)
            self.auto_check.pack(side="left", padx=(6, 0))
        else:
            self.auto_check = None

        self.sub = ttk.Label(self.frame, text="", foreground=MUTED,
                             font=("Segoe UI", 8))
        self.sub.pack(anchor="w", padx=(120, 0))
        self._update_sub()

    def _update_sub(self):
        info = self.info
        parts = [f"{info.minimum}..{info.maximum}", f"step {info.step}",
                 f"default {info.default}"]
        if info.label == "Exposure":
            parts.append(f"now {exposure_text(info.value)}")
        if info.label == "White Balance":
            parts.append("Kelvin")
        if info.flags & 1:
            parts.append("AUTO")
        self.sub.configure(text=" | ".join(parts))

    def _on_scale(self, raw):
        if self._updating:
            return
        step = max(1, self.info.step)
        value = int(round(float(raw) / step) * step)
        value = max(self.info.minimum, min(self.info.maximum, value))
        self.text_var.set(str(value))
        self._last_user = time.monotonic()
        self._pending = value
        if self._pending_job is None:
            self._pending_job = self.frame.after(30, self._push_pending)

    def _push_pending(self):
        self._pending_job = None
        value = self._pending
        self._pending = None
        if value is not None and not self.auto_var.get():
            self.app.apply_control(self.info, value, auto=False)

    def _on_entry(self, _event=None):
        if self._updating:
            return
        try:
            value = int(float(self.text_var.get()))
        except Exception:
            self.text_var.set(str(self.info.value))
            return
        value = max(self.info.minimum, min(self.info.maximum, value))
        self.text_var.set(str(value))
        self.var.set(value)
        self._last_user = time.monotonic()
        if not self.auto_var.get():
            self.app.apply_control(self.info, value, auto=False)
        return "break"

    def _on_auto(self):
        if self._updating:
            return
        auto = bool(self.auto_var.get())
        self._last_user = time.monotonic()
        self.app.apply_control(self.info, int(self.var.get()), auto=auto)
        self.frame.after(120, self.refresh)

    def refresh(self):
        if self.app.controls is None:
            return
        try:
            value, flags = self.app.controls.get(self.info.group, self.info.prop)
        except Exception:
            return
        self.info.value = value
        self.info.flags = flags
        is_auto = bool(flags & 1)
        if time.monotonic() - self._last_user > 0.8:
            self._updating = True
            try:
                self.var.set(value)
                self.text_var.set(str(value))
                self.auto_var.set(is_auto)
            finally:
                self._updating = False
        self.scale.state(["disabled"] if is_auto else ["!disabled"])
        if not self.info.supports_manual:
            self.spin.state(["disabled"])
        else:
            self.spin.state(["disabled"] if is_auto else ["!disabled"])
        self._update_sub()


# --------------------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------------------

class CameraApp:
    PREVIEW_MS = 20
    STATS_MS = 250
    POLL_MS = 500

    def __init__(self, root: tk.Tk):
        self.root = root
        enable_high_res_timer()
        self.closing = False
        self.worker = CaptureWorker()
        self.controls: Optional[DShowControls] = None
        self.control_rows: list[ControlRow] = []
        self.control_by_label: dict[str, ControlRow] = {}
        self.devices: list[str] = []
        self.formats: list[dict] = []
        self.actual_info: Optional[dict] = None
        self.last_stats: dict = {}
        self.saved_test_state: Optional[dict] = None

        # replay state
        self.replay_frames: list[tuple[float, bytes]] = []
        self.replay_metrics: list[dict] = []
        self.replay_active = False
        self.replay_index = 0
        self.replay_playing = False
        self.replay_next_time = 0.0
        self.replay_speed = 10.0
        self._replay_cache_index = -1
        self._replay_cache_frame: Optional[np.ndarray] = None

        # render cache
        self._photo = None
        self._img_key = None
        self._last_burst: Optional[BurstSession] = None

        self._setup_style()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(self.PREVIEW_MS, self.render_tick)
        self.root.after(self.STATS_MS, self.stats_tick)
        self.root.after(self.POLL_MS, self.poll_controls)
        self.refresh_devices(auto_open=True)

    # ---------------------------------------------------------------- UI construction
    def _setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        self.root.configure(bg=BG)
        style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                        troughcolor="#0b0e11", bordercolor="#2b333b",
                        lightcolor=PANEL, darkcolor=PANEL)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("TButton", padding=(8, 4))
        style.configure("Accent.TButton", padding=(10, 5))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                        padding=(12, 6))
        style.map("TNotebook.Tab",
                  background=[("selected", FIELD)],
                  foreground=[("selected", FG)])
        style.configure("TLabelframe", background=BG, foreground=FG)
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED)

    def _build_ui(self):
        self.root.title("High-FPS Camera Console  |  real FPS + UVC controls + global-shutter test")
        self.root.geometry("1480x880")
        self.root.minsize(1100, 680)

        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(fill="x")

        ttk.Label(top, text="Camera").pack(side="left")
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(top, textvariable=self.device_var,
                                         state="readonly", width=26)
        self.device_combo.pack(side="left", padx=(6, 4))
        self.device_combo.bind("<<ComboboxSelected>>", self.on_device_selected)
        ttk.Button(top, text="Refresh", command=self.refresh_devices).pack(side="left", padx=(0, 12))

        ttk.Label(top, text="Mode").pack(side="left")
        self.mode_var = tk.StringVar()
        self.mode_combo = ttk.Combobox(top, textvariable=self.mode_var,
                                       state="readonly", width=34)
        self.mode_combo.pack(side="left", padx=(6, 4))
        self.mode_combo.bind("<<ComboboxSelected>>", self.on_mode_selected)

        ttk.Label(top, text="FPS").pack(side="left")
        self.fps_var_input = tk.StringVar(value="120")
        self.fps_combo = ttk.Combobox(top, textvariable=self.fps_var_input,
                                      width=7, values=["120", "110", "100", "90", "80", "60"])
        self.fps_combo.pack(side="left", padx=(6, 2))
        self.fps_combo.bind("<<ComboboxSelected>>", self.update_rate_note)
        self.fps_combo.bind("<Return>", self.update_rate_note)
        self.rate_note = ttk.Label(top, text="", style="Muted.TLabel")
        self.rate_note.pack(side="left", padx=(2, 12))

        ttk.Button(top, text="Open", command=self.open_camera).pack(side="left")
        ttk.Button(top, text="Close", command=self.close_camera).pack(side="left", padx=4)
        ttk.Button(top, text="Driver property page",
                   command=self.open_driver_page).pack(side="left", padx=(12, 0))

        main = ttk.Panedwindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.Frame(main)
        right = ttk.Frame(main, width=500)
        main.add(left, weight=1)
        main.add(right, weight=0)

        self.preview = tk.Canvas(left, bg="#000000", highlightthickness=0, bd=0)
        self.preview.pack(fill="both", expand=True)
        self._img_item = self.preview.create_image(0, 0, anchor="nw")
        self._blank_text = None

        # Live stats panel
        stats_box = ttk.LabelFrame(right, text=" Live capture ", padding=8)
        stats_box.pack(fill="x", pady=(0, 6))
        self.stat_fps = tk.StringVar(value="--")
        self.stat_fps_caption = tk.StringVar(value="measured FPS")
        self.stat_req = tk.StringVar(value="--")
        self.stat_interval = tk.StringVar(value="--")
        self.stat_jitter = tk.StringVar(value="--")
        self.stat_aux_label = tk.StringVar(value="late (2s)")
        self.stat_late = tk.StringVar(value="--")
        self.stat_frames = tk.StringVar(value="--")
        self.stat_decode = tk.StringVar(value="--")

        big = tk.Label(stats_box, textvariable=self.stat_fps, bg=BG, fg=ACCENT,
                       font=("Consolas", 24, "bold"))
        big.grid(row=0, column=0, sticky="w", padx=(0, 14))
        tk.Label(stats_box, textvariable=self.stat_fps_caption, bg=BG, fg=MUTED,
                 font=("Segoe UI", 8)).grid(row=1, column=0, sticky="nw", padx=(2, 14))
        ttk.Label(stats_box, text="camera").grid(row=0, column=1, sticky="w")
        ttk.Label(stats_box, textvariable=self.stat_req).grid(row=0, column=2, sticky="w", padx=(4, 12))
        ttk.Label(stats_box, text="interval").grid(row=0, column=3, sticky="w")
        ttk.Label(stats_box, textvariable=self.stat_interval).grid(row=0, column=4, sticky="w", padx=(4, 12))
        ttk.Label(stats_box, text="decode").grid(row=1, column=1, sticky="w")
        ttk.Label(stats_box, textvariable=self.stat_decode).grid(row=1, column=2, sticky="w", padx=(4, 12))
        ttk.Label(stats_box, text="jitter").grid(row=1, column=3, sticky="w")
        ttk.Label(stats_box, textvariable=self.stat_jitter).grid(row=1, column=4, sticky="w", padx=(4, 12))
        ttk.Label(stats_box, textvariable=self.stat_aux_label).grid(row=2, column=1, sticky="w")
        ttk.Label(stats_box, textvariable=self.stat_late).grid(row=2, column=2, sticky="w", padx=(4, 12))
        ttk.Label(stats_box, text="frames").grid(row=2, column=3, sticky="w")
        ttk.Label(stats_box, textvariable=self.stat_frames).grid(row=2, column=4, sticky="w", padx=(4, 12))

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)

        # Controls tab
        controls_tab = ttk.Frame(self.notebook)
        self.notebook.add(controls_tab, text="Controls")
        bar = ttk.Frame(controls_tab)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Button(bar, text="Refresh values", command=self.refresh_control_values).pack(side="left")
        ttk.Button(bar, text="Reset all to defaults", command=self.reset_defaults).pack(side="left", padx=6)
        self.controls_hint = ttk.Label(bar, text="", style="Muted.TLabel")
        self.controls_hint.pack(side="left", padx=8)

        self.controls_scroll = ScrollFrame(controls_tab)
        self.controls_scroll.pack(fill="both", expand=True)
        self.controls_inner = self.controls_scroll.inner
        self.controls_footer = ttk.Label(self.controls_inner, text="", style="Muted.TLabel",
                                         wraplength=440, justify="left")
        self.controls_footer.pack(fill="x", padx=10, pady=(6, 10))

        # Global shutter tab
        gs_tab = ttk.Frame(self.notebook)
        self.notebook.add(gs_tab, text="Global Shutter Test")
        gs_scroll = ScrollFrame(gs_tab)
        gs_scroll.pack(fill="both", expand=True)
        gs = gs_scroll.inner

        tk.Label(gs, text="Is this camera really global shutter?",
                 bg=BG, fg=FG, font=("Segoe UI", 13, "bold"),
                 wraplength=450, justify="left").pack(anchor="w", padx=10, pady=(10, 2))
        instructions = (
            "1. Open or print the supplied test target (button below): a solid black "
            "bar on a white page. Display it on a phone/tablet or print it.\n"
            "2. Rest the camera on a table (do not hand-hold it). Hold the target "
            "upright so the black bar fills most of the frame height.\n"
            "3. Apply short exposure below (pick a longer one if the preview is "
            "black), then Record burst. A desk lamp on the target helps.\n"
            "4. On GO, sweep the target left-right across the view as fast as you "
            "can, back and forth, until the burst ends.\n\n"
            "How to read it: a global shutter keeps the bar vertical (it may be "
            "motion-blurred). A rolling shutter shears it into a slant, and the slant "
            "follows the sweep direction. Replay the frames slowly — that visual check "
            "is definitive. The automatic verdict tracks the bar and correlates its "
            "tilt with its horizontal speed as a second opinion."
        )
        tk.Label(gs, text=instructions, bg=BG, fg=MUTED, font=("Segoe UI", 9),
                 wraplength=450, justify="left").pack(anchor="w", padx=10, pady=(0, 8))

        setup = ttk.LabelFrame(gs, text=" Test target and exposure ", padding=8)
        setup.pack(fill="x", padx=10, pady=4)
        ttk.Button(setup, text="Open test target",
                   command=self.open_test_target).grid(row=0, column=0, sticky="w")
        ttk.Label(setup, text="exposure", style="Muted.TLabel").grid(
            row=0, column=1, padx=(10, 4))
        self.test_exp_var = tk.StringVar()
        self.test_exp_combo = ttk.Combobox(setup, textvariable=self.test_exp_var,
                                           state="readonly", width=18, values=[])
        self.test_exp_combo.grid(row=0, column=2, sticky="w")
        ttk.Button(setup, text="Apply", command=self.prepare_test_settings).grid(
            row=0, column=3, padx=6)
        ttk.Button(setup, text="Restore", command=self.restore_test_settings).grid(
            row=0, column=4)
        ttk.Label(setup, text="Short exposure + high gain makes a fast sweep crisp. "
                             "Light the target if the preview is dark.",
                  style="Muted.TLabel", font=("Segoe UI", 8)).grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(4, 0))

        burst_box = ttk.LabelFrame(gs, text=" Burst capture ", padding=8)
        burst_box.pack(fill="x", padx=10, pady=4)
        ttk.Button(burst_box, text="Record burst (100 frames)",
                   command=self.record_burst).pack(side="left")
        ttk.Button(burst_box, text="Stop", command=self.stop_burst).pack(side="left", padx=6)
        self.burst_progress = tk.StringVar(value="Idle")
        ttk.Label(burst_box, textvariable=self.burst_progress).pack(side="left", padx=8)

        verdict_box = ttk.LabelFrame(gs, text=" Automatic analysis ", padding=8)
        verdict_box.pack(fill="x", padx=10, pady=4)
        self.verdict_label = tk.Label(verdict_box, text="No burst recorded yet.",
                                      bg=BG, fg=MUTED, font=("Segoe UI", 12, "bold"),
                                      wraplength=430, justify="left")
        self.verdict_label.pack(anchor="w")
        self.gs_metrics = tk.StringVar(value="")
        ttk.Label(verdict_box, textvariable=self.gs_metrics, style="Muted.TLabel",
                  wraplength=430, justify="left").pack(anchor="w", pady=(4, 0))

        replay_box = ttk.LabelFrame(gs, text=" Slow-motion replay ", padding=8)
        replay_box.pack(fill="x", padx=10, pady=4)
        self.replay_slider_var = tk.DoubleVar(value=0)
        self.replay_slider = ttk.Scale(replay_box, from_=0, to=0, orient="horizontal",
                                       variable=self.replay_slider_var,
                                       command=self.on_replay_scrub)
        self.replay_slider.grid(row=0, column=0, columnspan=5, sticky="ew", pady=(0, 4))
        replay_box.columnconfigure(0, weight=1)
        ttk.Button(replay_box, text="<<", width=3,
                   command=lambda: self.step_replay(-1)).grid(row=1, column=0, sticky="w")
        self.play_button = ttk.Button(replay_box, text="Play 10 fps",
                                      command=self.toggle_replay_play)
        self.play_button.grid(row=1, column=1)
        ttk.Button(replay_box, text=">>", width=3,
                   command=lambda: self.step_replay(1)).grid(row=1, column=2)
        ttk.Button(replay_box, text="Live preview",
                   command=self.stop_replay).grid(row=1, column=3, padx=(6, 0))
        ttk.Button(replay_box, text="Save burst...",
                   command=self.save_burst).grid(row=1, column=4, padx=(6, 0))
        self.replay_label = tk.StringVar(value="")
        ttk.Label(replay_box, textvariable=self.replay_label,
                  style="Muted.TLabel").grid(row=2, column=0, columnspan=5,
                                             sticky="w", pady=(4, 0))

        tk.Label(gs, text=("Note: UVC does not report the shutter type in any standard "
                           "control. The burst test is optical, not metadata-based."),
                 bg=BG, fg=MUTED, font=("Segoe UI", 8), wraplength=450,
                 justify="left").pack(anchor="w", padx=10, pady=(4, 10))

        # Info tab
        info_tab = ttk.Frame(self.notebook)
        self.notebook.add(info_tab, text="Info")
        self.info_text = tk.Text(info_tab, bg=PANEL, fg=FG, insertbackground=FG,
                                 relief="flat", wrap="word", padx=10, pady=8,
                                 font=("Consolas", 9))
        info_scroll = ttk.Scrollbar(info_tab, orient="vertical",
                                    command=self.info_text.yview)
        self.info_text.configure(yscrollcommand=info_scroll.set)
        info_scroll.pack(side="right", fill="y")
        self.info_text.pack(fill="both", expand=True)
        self.info_text.insert("1.0", "Open a camera to see details.")
        self.info_text.configure(state="disabled")

        self.status_var = tk.StringVar(value="Ready.")
        status = ttk.Label(self.root, textvariable=self.status_var, style="Muted.TLabel",
                           padding=(10, 4))
        status.pack(fill="x", side="bottom")

    # ------------------------------------------------------------------ device handling
    def set_status(self, text: str):
        self.status_var.set(text)

    def refresh_devices(self, _event=None, auto_open: bool = False):
        was_open = self.worker.running
        self.devices = enumerate_devices()
        self.device_combo["values"] = self.devices
        if not self.devices:
            self.set_status("No DirectShow video devices found.")
            self.device_var.set("")
            self.formats = []
            self.mode_combo["values"] = []
            return
        selected = self.device_var.get()
        if selected not in self.devices:
            preferred = next((d for d in self.devices if "openaicam" in d.lower()), self.devices[0])
            selected = preferred
            self.device_var.set(selected)
        self.device_combo.current(self.devices.index(selected))
        self.rebuild_formats()
        if auto_open and not was_open:
            self.open_camera()

    def selected_device_index(self) -> int:
        name = self.device_var.get()
        try:
            return self.devices.index(name)
        except ValueError:
            return 0

    def rebuild_formats(self):
        index = self.selected_device_index()
        try:
            self.formats = enumerate_formats(index)
        except Exception as exc:
            self.formats = []
            self.set_status(f"Could not enumerate formats: {exc}")
        labels = []
        for f in self.formats:
            if abs(f["fps_max"] - f["fps_min"]) < 0.5:
                fps_txt = f"{f['fps_max']:.0f} fps"
            else:
                fps_txt = f"{f['fps_min']:.0f}-{f['fps_max']:.0f} fps"
            labels.append(f"{f['width']}x{f['height']} {f['fourcc']}  {fps_txt}")
        self.mode_combo["values"] = labels
        if labels:
            current = self.mode_combo.current()
            if current < 0 or current >= len(labels):
                self.mode_combo.current(0)
            self.on_mode_selected()
            if not self.worker.running:
                self.set_status(f"{len(self.devices)} device(s), {len(self.formats)} mode(s).")

    def on_device_selected(self, _event=None):
        self.rebuild_formats()
        if self.worker.running:
            self.set_status("Device selection changed — press Open to apply.")

    def on_mode_selected(self, _event=None):
        idx = self.mode_combo.current()
        if idx < 0 or idx >= len(self.formats):
            return
        fmt = self.formats[idx]
        candidates = [fmt["fps_max"], 110, 100, 90, 80, 60, fmt["fps_min"]]
        rates = sorted({int(round(r)) for r in candidates
                        if fmt["fps_min"] - 0.5 <= r <= fmt["fps_max"] + 0.5 and r > 0},
                       reverse=True)
        if not rates:
            rates = [int(round(fmt["fps_max"]))]
        self.fps_combo["values"] = [str(r) for r in rates]
        self.fps_var_input.set(str(rates[0]))
        self.update_rate_note()

    def update_rate_note(self, _event=None):
        idx = self.mode_combo.current()
        if idx < 0 or idx >= len(self.formats):
            return
        fmt = self.formats[idx]
        try:
            rate = float(self.fps_var_input.get())
        except ValueError:
            self.rate_note.configure(text="invalid")
            return
        if rate <= 0 or rate > fmt["fps_max"] + 0.5:
            self.rate_note.configure(text="invalid")
            return
        native = (abs(rate - fmt["fps_max"]) <= 0.5
                  or abs(rate - fmt["fps_min"]) <= 0.5)
        self.rate_note.configure(text="native" if native else "software fixed")

    def selected_rate_plan(self, fmt: dict) -> tuple[float, float]:
        """Return (camera_native_fps, fixed_output_fps). fixed=0 means native."""
        try:
            requested = float(self.fps_var_input.get())
        except ValueError:
            requested = float(fmt["fps_max"])
        requested = max(1.0, min(requested, float(fmt["fps_max"])))
        if (abs(requested - fmt["fps_max"]) <= 0.5
                or abs(requested - fmt["fps_min"]) <= 0.5):
            return requested, 0.0
        # The camera snaps in-between requests to its native maximum. Open it at
        # 120 and let the fixed-rate publisher emit exactly the requested rate.
        return float(fmt["fps_max"]), requested

    def open_camera(self):
        if not self.formats:
            messagebox.showerror("Camera", "No camera format available. Press Refresh.")
            return
        idx = self.mode_combo.current()
        if idx < 0:
            idx = 0
        fmt = self.formats[idx]
        camera_fps, fixed_rate = self.selected_rate_plan(fmt)
        self.update_rate_note()
        self.close_camera(silent=True)
        ok, msg, actual = self.worker.open(
            self.selected_device_index(), fmt, camera_fps, fixed_rate)
        if not ok:
            messagebox.showerror("Capture", msg)
            self.set_status(msg)
            return
        self.actual_info = actual
        try:
            self.controls = DShowControls(self.selected_device_index())
            infos = self.controls.discover()
        except Exception as exc:
            self.controls = None
            infos = []
            self.set_status(f"Capture running, but UVC controls unavailable: {exc}")
        self.build_control_rows(infos)
        self.stop_replay(update_only=True)
        self.update_info_tab()
        if fixed_rate > 0:
            rate_txt = (f"camera {camera_fps:g} fps -> fixed output "
                        f"{fixed_rate:g} fps (software)")
        else:
            rate_txt = f"camera {camera_fps:g} fps"
        self.set_status(
            f"Streaming {actual['width']}x{actual['height']} {actual['fourcc']}, "
            f"{rate_txt} ({len(infos)} UVC controls).")

    def close_camera(self, silent: bool = False):
        if self.saved_test_state is not None:
            self.restore_test_settings(silent=True)
        if self.controls is not None:
            try:
                self.controls.close()
            except Exception:
                pass
            self.controls = None
        self.worker.close()
        self.actual_info = None
        self.clear_control_rows()
        self.stat_fps.set("--")
        self.stat_fps_caption.set("measured FPS")
        self.stat_req.set("--")
        self.stat_interval.set("--")
        self.stat_jitter.set("--")
        self.stat_aux_label.set("late (2s)")
        self.stat_late.set("--")
        self.stat_frames.set("--")
        self.stat_decode.set("--")
        self.update_info_tab()
        if not silent:
            self.set_status("Camera closed.")

    def open_driver_page(self):
        if self.controls is not None:
            try:
                self.set_status("Opening the driver property page (close it to continue)...")
                self.controls.show_property_page()
                self.set_status("Driver property page closed.")
                return
            except Exception as exc:
                messagebox.showerror("Driver property page", str(exc))
        else:
            messagebox.showinfo("Driver property page",
                                "Open a camera first. This camera exposes no native page "
                                "or controls are unavailable.")

    # --------------------------------------------------------------------- controls tab
    def clear_control_rows(self):
        for widget in list(self.controls_inner.winfo_children()):
            if widget is not self.controls_footer:
                try:
                    widget.destroy()
                except Exception:
                    pass
        self.control_rows.clear()
        self.control_by_label.clear()

    def build_control_rows(self, infos: list[ControlInfo]):
        self.clear_control_rows()
        video = [i for i in infos if i.group == "video"]
        camera = [i for i in infos if i.group == "camera"]
        if video:
            ttk.Label(self.controls_inner, text="IMAGE CONTROLS  (IAMVideoProcAmp)",
                      foreground=ACCENT, font=("Segoe UI", 8, "bold")).pack(
                anchor="w", padx=10, pady=(8, 2))
            for info in video:
                self._add_control_row(info)
        if camera:
            ttk.Label(self.controls_inner, text="CAMERA CONTROLS  (IAMCameraControl)",
                      foreground=ACCENT, font=("Segoe UI", 8, "bold")).pack(
                anchor="w", padx=10, pady=(10, 2))
            for info in camera:
                self._add_control_row(info)
        missing = [n for n in ALL_CONTROL_NAMES if n not in self.control_by_label]
        hint = f"{len(infos)} live controls" if infos else "No controls"
        self.controls_hint.configure(text=hint)
        self.controls_footer.configure(
            text=("Not exposed by this camera: " + ", ".join(missing)) if missing else "")
        self._build_test_exposure_choices()
        # keep the footer at the bottom
        self.controls_footer.pack_forget()
        self.controls_footer.pack(fill="x", padx=10, pady=(8, 12))

    def _add_control_row(self, info: ControlInfo):
        row = ControlRow(self, self.controls_inner, info)
        self.control_rows.append(row)
        self.control_by_label[info.label] = row

    def apply_control(self, info: ControlInfo, value: int, auto: bool):
        if self.controls is None:
            return
        value = max(info.minimum, min(info.maximum, int(value)))
        try:
            self.controls.set_flag(info.group, info.prop, auto=auto, value=value)
            info.value = value
            info.flags = 1 if auto else 2
            row = self.control_by_label.get(info.label)
            if row:
                row._update_sub()
        except Exception as exc:
            self.set_status(f"Setting {info.label} failed: {exc}")

    def refresh_control_values(self):
        for row in self.control_rows:
            row.refresh()

    def poll_controls(self):
        if not self.closing:
            for row in self.control_rows:
                try:
                    row.refresh()
                except Exception:
                    pass
            self.root.after(self.POLL_MS, self.poll_controls)

    def reset_defaults(self):
        if self.controls is None:
            return
        for row in self.control_rows:
            info = row.info
            try:
                self.controls.set_flag(info.group, info.prop, auto=False,
                                       value=info.default)
            except Exception:
                continue
        self.root.after(120, self.refresh_control_values)
        self.set_status("All controls reset to their DirectShow defaults (manual).")

    def control_value(self, label: str) -> Optional[int]:
        row = self.control_by_label.get(label)
        return row.info.value if row else None

    # ------------------------------------------------------------------ global shutter
    def _build_test_exposure_choices(self):
        row = self.control_by_label.get("Exposure")
        if row is None:
            self.test_exp_combo["values"] = []
            self.test_exp_var.set("")
            return
        info = row.info
        lo, hi = info.minimum, info.maximum
        # short-exposure candidates near the minimum, in log2-second units
        candidates = [v for v in range(lo, min(hi, lo + 6) + 1)]
        labels = [f"{v}  ({exposure_text(v)})" for v in candidates]
        self.test_exp_combo["values"] = labels
        default_index = min(3, len(labels) - 1)
        if labels:
            self.test_exp_var.set(labels[default_index])

    def _parse_test_exposure(self) -> Optional[int]:
        m = re.match(r"\s*(-?\d+)", self.test_exp_var.get())
        return int(m.group(1)) if m else None

    def open_test_target(self):
        try:
            path = ensure_test_target()
            os.startfile(path)
        except Exception as exc:
            messagebox.showerror("Test target", f"Cannot open the target image: {exc}")

    def prepare_test_settings(self):
        if self.controls is None:
            messagebox.showinfo("Test settings", "Open the camera first.")
            return
        exp_row = self.control_by_label.get("Exposure")
        exp_value = self._parse_test_exposure()
        if exp_row is None or exp_value is None:
            messagebox.showinfo("Test settings",
                                "This camera exposes no manual exposure control.")
            return
        gain_row = self.control_by_label.get("Gain")
        state = {}
        try:
            state["exposure"] = self.controls.get("camera", exp_row.info.prop)
        except Exception as exc:
            messagebox.showerror("Test settings", f"Cannot read exposure: {exc}")
            return
        if gain_row is not None:
            try:
                state["gain"] = self.controls.get("video", gain_row.info.prop)
            except Exception:
                gain_row = None
        self.saved_test_state = state

        try:
            self.controls.set_flag("camera", exp_row.info.prop, auto=False,
                                   value=exp_value)
        except Exception as exc:
            messagebox.showerror("Test settings", f"Cannot set exposure: {exc}")
            return
        if gain_row is not None:
            target_gain = min(gain_row.info.maximum, max(gain_row.info.minimum, 45))
            try:
                self.controls.set_flag("video", gain_row.info.prop, auto=False,
                                       value=target_gain)
            except Exception:
                pass
        self.root.after(120, self.refresh_control_values)
        self.set_status(
            f"Test settings applied: exposure {exp_value} ({exposure_text(exp_value)}), "
            "auto exposure off. Restore with the button when done.")

    def restore_test_settings(self, silent: bool = False):
        state = self.saved_test_state
        self.saved_test_state = None
        if not state or self.controls is None:
            if not silent:
                self.set_status("No saved test settings to restore.")
            return
        try:
            if "exposure" in state:
                value, flags = state["exposure"]
                self.controls.set_flag("camera", 4, auto=bool(flags & 1), value=value)
            if "gain" in state:
                value, flags = state["gain"]
                self.controls.set_flag("video", 9, auto=bool(flags & 1), value=value)
        except Exception as exc:
            if not silent:
                self.set_status(f"Restore failed: {exc}")
            return
        self.root.after(120, self.refresh_control_values)
        if not silent:
            self.set_status("Previous exposure/gain restored.")

    def record_burst(self):
        if not self.worker.running:
            messagebox.showinfo("Burst", "Open the camera first.")
            return
        self.stop_replay(update_only=True)
        self.burst_progress.set("Get ready...")
        self.verdict_label.configure(text="Recording burst...", fg=WARN)
        self.gs_metrics.set("")
        self._countdown(3)

    def _countdown(self, n: int):
        if self.closing:
            return
        if n > 0:
            self.burst_progress.set(f"Get ready: {n}...")
            self.root.after(700, lambda: self._countdown(n - 1))
            return
        self.burst_progress.set("GO - sweep the edge now!")
        session = self.worker.start_burst(100)
        if session is None:
            self.burst_progress.set("Could not start burst.")
            return
        self._last_burst = session
        self.root.after(100, lambda: self._poll_burst(session, time.perf_counter()))

    def _poll_burst(self, session: BurstSession, started: float):
        if self.closing:
            return
        if session.done.is_set():
            self._on_burst_ready(session)
            return
        elapsed = time.perf_counter() - started
        timeout = max(4.0, 100.0 / max(1.0, self.worker.nominal_fps) * 3.0)
        if elapsed > timeout:
            self.worker.end_burst()
            self.root.after(150, lambda: self._poll_burst(session, started))
            return
        self.burst_progress.set(
            f"Capturing: {session.offered}/100 offered, {len(session.frames)} stored")
        self.root.after(100, lambda: self._poll_burst(session, started))

    def stop_burst(self):
        session = self.worker.end_burst()
        if session is None:
            self.burst_progress.set("No burst running.")
            return
        self.burst_progress.set("Finishing burst...")
        self.root.after(100, lambda: self._wait_stopped(session))

    def _wait_stopped(self, session: BurstSession):
        if session.done.is_set():
            self._on_burst_ready(session)
        else:
            self.root.after(100, lambda: self._wait_stopped(session))

    def _on_burst_ready(self, session: BurstSession):
        self._last_burst = session
        self.replay_frames = list(session.frames)
        self.replay_metrics = list(session.metrics)
        self.replay_slider.configure(from_=0, to=max(0, len(self.replay_frames) - 1))
        self.replay_slider_var.set(0)
        self.replay_index = 0
        self.replay_playing = False
        self.play_button.configure(text="Play 10 fps")
        self._replay_cache_index = -1
        self._replay_cache_frame = None
        if self.replay_frames:
            self.replay_active = True
        self.burst_progress.set(
            f"Captured {len(self.replay_frames)} frames "
            f"({session.dropped} dropped by writer).")

        verdict, explanation, stats = evaluate_burst(self.replay_metrics)
        color = {"global": ACCENT, "rolling": BAD, "inconclusive": WARN}[verdict]
        title = {"global": "LIKELY GLOBAL SHUTTER",
                 "rolling": "ROLLING SHUTTER DETECTED",
                 "inconclusive": "INCONCLUSIVE"}[verdict]
        self.verdict_label.configure(text=f"{title}\n{explanation}", fg=color)
        if stats:
            self.gs_metrics.set(
                f"bar tilt: {stats.get('min', 0):+.1f} to {stats.get('max', 0):+.1f} deg "
                f"(span {stats.get('span', 0):.1f}) | "
                f"fast frames {stats.get('n', 0)} | "
                f"tilt/speed corr {stats.get('corr', 0):+.2f} | "
                f"max speed {stats.get('max_speed', 0):.0f} px/s | "
                f"bar found {stats.get('found', 0)}/{stats.get('total', 0)}")
        else:
            self.gs_metrics.set("No test-target measurements.")
        self.notebook.select(1)
        self.set_status("Burst ready. Scrub/play the replay; the visual check is definitive.")

    def _decoded_replay_frame(self) -> Optional[np.ndarray]:
        if not self.replay_frames:
            return None
        idx = max(0, min(self.replay_index, len(self.replay_frames) - 1))
        if idx != self._replay_cache_index or self._replay_cache_frame is None:
            _, jpg = self.replay_frames[idx]
            frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            self._replay_cache_index = idx
            self._replay_cache_frame = frame
        return self._replay_cache_frame

    def on_replay_scrub(self, raw):
        if not self.replay_frames:
            return
        self.replay_playing = False
        self.play_button.configure(text="Play 10 fps")
        self.replay_index = int(round(float(raw)))
        self.replay_index = max(0, min(self.replay_index, len(self.replay_frames) - 1))
        self.replay_label.set(
            f"Frame {self.replay_index + 1}/{len(self.replay_frames)}")

    def step_replay(self, delta: int):
        if not self.replay_frames:
            return
        self.replay_playing = False
        self.play_button.configure(text="Play 10 fps")
        self.replay_index = max(0, min(self.replay_index + delta,
                                       len(self.replay_frames) - 1))
        self.replay_slider_var.set(self.replay_index)
        self.replay_label.set(
            f"Frame {self.replay_index + 1}/{len(self.replay_frames)}")

    def toggle_replay_play(self):
        if not self.replay_frames:
            return
        self.replay_active = True
        self.replay_playing = not self.replay_playing
        self.play_button.configure(
            text="Pause" if self.replay_playing else "Play 10 fps")
        self.replay_next_time = time.perf_counter()

    def stop_replay(self, update_only: bool = False):
        self.replay_active = False
        self.replay_playing = False
        if hasattr(self, "play_button"):
            self.play_button.configure(text="Play 10 fps")
        if not update_only:
            self.set_status("Live preview.")

    def save_burst(self):
        if not self.replay_frames:
            messagebox.showinfo("Save burst", "No burst to save.")
            return
        base = os.path.dirname(os.path.abspath(__file__))
        folder = os.path.join(base, "captures",
                              "gs_test_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(folder, exist_ok=True)
        for i, (_, jpg) in enumerate(self.replay_frames):
            with open(os.path.join(folder, f"frame_{i:04d}.jpg"), "wb") as fh:
                fh.write(jpg)
        verdict, explanation, stats = evaluate_burst(self.replay_metrics)
        lines = [
            "Global shutter verification",
            "Date: " + datetime.now().isoformat(timespec="seconds"),
            "Camera: " + (self.worker_name() or "?"),
            "Mode: " + self.mode_var.get(),
            "Requested FPS: " + self.fps_var_input.get(),
            "Measured FPS: " + self.stat_fps.get(),
            "Verdict: " + verdict.upper(),
            "Explanation: " + explanation,
            f"Frames stored: {len(self.replay_frames)}",
            "Statistics: " + repr(stats),
            "",
            "Control snapshot:",
        ]
        for row in self.control_rows:
            info = row.info
            lines.append(f"  {info.label}: {info.value} (auto={bool(info.flags & 1)})")
        lines += ["", "Target bar per frame (tilt, horizontal speed):"]
        for i, m in enumerate(self.replay_metrics):
            tilt = "None" if m.get("tilt") is None else f"{m['tilt']:+.2f}"
            vx = m.get("vx")
            speed = "None" if vx is None else f"{vx * float(m.get('scale', 1.0)):.0f}"
            lines.append(f"  {i:04d}: found={m.get('found')} tilt={tilt} "
                         f"speed_px_s={speed} area={m.get('area', 0):.0f} "
                         f"aspect={m.get('aspect', 0):.2f}")
        with open(os.path.join(folder, "report.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        self.set_status(f"Saved {len(self.replay_frames)} frames to {folder}")
        try:
            os.startfile(folder)
        except Exception:
            pass

    def worker_name(self) -> str:
        return self.device_var.get()

    # --------------------------------------------------------------------------- info
    def update_info_tab(self):
        if not hasattr(self, "info_text"):
            return
        lines = []
        lines.append("DEVICE")
        lines.append("  " + (self.device_var.get() or "(none)"))
        if self.actual_info:
            a = self.actual_info
            lines.append(f"  negotiated: {a['width']}x{a['height']} {a['fourcc']}")
            camera_fps = a.get("camera_fps", a.get("fps_reported", 0.0))
            fixed_rate = a.get("fixed_rate", 0.0)
            if fixed_rate > 0:
                lines.append(f"  camera native: {camera_fps:.0f} fps -> "
                             f"fixed software output: {fixed_rate:.2f} fps")
            else:
                lines.append(f"  camera fps: {camera_fps:.0f}")
            lines.append("  backend: DirectShow (cv2.CAP_DSHOW), MJPG decode via OpenCV")
        else:
            lines.append("  not open")
        lines.append("")
        lines.append("SUPPORTED MODES (from IAMStreamConfig)")
        if self.formats:
            for f in self.formats:
                lines.append(f"  {f['width']}x{f['height']} {f['fourcc']}  "
                             f"{f['fps_min']:.0f}-{f['fps_max']:.0f} fps "
                             f"(default {f['fps_default']:.0f})")
        else:
            lines.append("  (none enumerated)")
        lines.append("")
        lines.append("FIXED OUTPUT RATE")
        lines.append("  This camera runs natively at 60 or 120 fps only. Requests")
        lines.append("  between those values are accepted by the driver but the camera")
        lines.append("  still runs at 120 (90-120) or 60 (60-80), which is why 100 fps")
        lines.append("  cannot be a native setting. Selecting 100 in the FPS box opens")
        lines.append("  the camera at 120 and a publisher thread emits the newest frame")
        lines.append("  every 10.000 ms (100.000 Hz), skipping raw frames. If the USB")
        lines.append("  stream stalls, the last frame is repeated and counted as a")
        lines.append("  duplicate. Output timestamps stay fixed regardless of camera")
        lines.append("  jitter. The big FPS number is the fixed output rate; the")
        lines.append("  caption below it shows the raw camera rate.")
        lines.append("")
        lines.append("REAL FPS")
        lines.append("  Measured from the timestamps of frames returned by cv2.read(),")
        lines.append("  over a 2-second sliding window. 'late' counts intervals longer")
        lines.append("  than 1.8x the requested frame period; it catches USB/decoder hiccups.")
        lines.append("  Requested FPS is the driver setting; measured FPS is what actually")
        lines.append("  arrived at the application.")
        lines.append("")
        lines.append("GLOBAL SHUTTER")
        lines.append("  UVC has no standard control for shutter type, so this cannot be")
        lines.append("  read from metadata. Use the Global Shutter Test tab with the")
        lines.append("  supplied target (gs_target.png, printable or on a phone):")
        lines.append("  record a short-exposure burst while sweeping the black bar")
        lines.append("  left-right, then replay the frames slowly.")
        lines.append("  Global shutter: the bar stays vertical (perhaps blurred).")
        lines.append("  Rolling shutter: the bar is sheared into a slant in the")
        lines.append("  direction of motion, so its tilt correlates with its speed.")
        lines.append("  The automatic verdict measures exactly that correlation.")
        lines.append("")
        lines.append("CONTROLS")
        lines.append("  Values and ranges come from IAMVideoProcAmp / IAMCameraControl")
        lines.append("  on a second DirectShow filter instance, updated live while the")
        lines.append("  capture runs. Exposure is in log2(seconds), as DirectShow defines.")
        self.info_text.configure(state="normal")
        self.info_text.delete("1.0", "end")
        self.info_text.insert("1.0", "\n".join(lines))
        self.info_text.configure(state="disabled")

    # ------------------------------------------------------------------ live rendering
    def stats_tick(self):
        if self.closing:
            return
        stats = self.worker.stats()
        self.last_stats = stats
        if self.worker.running:
            if stats.get("fixed_active"):
                self.stat_fps.set(f"{stats['output_fps']:.1f}")
                self.stat_fps_caption.set(
                    f"fixed output FPS  ·  camera {stats['fps']:.1f}")
                self.stat_req.set(f"{stats['nominal']:.0f}")
                self.stat_interval.set(f"{stats['out_interval_ms']:.2f} ms")
                self.stat_jitter.set(f"{stats['out_jitter_ms']:.2f} ms")
                self.stat_aux_label.set("duplicates (2s)")
                self.stat_late.set(str(stats["duplicates"]))
                self.stat_frames.set(str(stats["out_frames"]))
            else:
                self.stat_fps.set(f"{stats['fps']:.1f}")
                self.stat_fps_caption.set("measured FPS")
                self.stat_req.set(f"{stats['nominal']:.0f}")
                self.stat_interval.set(f"{stats['interval_ms']:.2f} ms")
                self.stat_jitter.set(f"{stats['jitter_ms']:.2f} ms")
                self.stat_aux_label.set("late (2s)")
                self.stat_late.set(str(stats["late"]))
                self.stat_frames.set(str(stats["frames"]))
            self.stat_decode.set(f"{stats['decode_ms']:.2f} ms")
        self.root.after(self.STATS_MS, self.stats_tick)

    def render_tick(self):
        if self.closing:
            return
        try:
            self._render_once()
        except Exception as exc:
            self.set_status(f"Render error: {exc}")
        self.root.after(self.PREVIEW_MS, self.render_tick)

    def _render_once(self):
        cw = max(2, self.preview.winfo_width())
        ch = max(2, self.preview.winfo_height())

        frame = None
        key = None
        replay_info = None
        if self.replay_active and self.replay_frames:
            if self.replay_playing:
                now = time.perf_counter()
                if now >= self.replay_next_time:
                    if self.replay_index < len(self.replay_frames) - 1:
                        self.replay_index += 1
                        self.replay_slider_var.set(self.replay_index)
                    else:
                        self.replay_playing = False
                        self.play_button.configure(text="Play 10 fps")
                    self.replay_next_time = now + 1.0 / self.replay_speed
            base = self._decoded_replay_frame()
            if base is not None:
                frame = base.copy()
                key = ("replay", self.replay_index, base.shape)
                replay_info = self._draw_replay_overlay(frame)
                self.replay_label.set(
                    f"Frame {self.replay_index + 1}/{len(self.replay_frames)}"
                    + (f"  |  bar tilt {replay_info['tilt']:+.1f} deg"
                       if replay_info and replay_info.get("tilt") is not None else "")
                    + ("" if replay_info and replay_info.get("found") else "  |  target not found"))
        else:
            live, frame_id, _ = self.worker.read_latest()
            if live is not None:
                frame = live
                key = ("live", frame_id, live.shape, cw, ch)

        if frame is None:
            if self._blank_text is None:
                self.preview.delete("all")
                self._img_item = self.preview.create_image(0, 0, anchor="nw")
                self._img_key = None
                self._blank_text = self.preview.create_text(
                    cw // 2, ch // 2, text="No camera\nSelect a device and press Open",
                    fill=MUTED, font=("Segoe UI", 14), justify="center")
            else:
                self.preview.coords(self._blank_text, cw // 2, ch // 2)
            return

        if self._blank_text is not None:
            self.preview.delete(self._blank_text)
            self._blank_text = None

        render_key = (key, cw, ch)
        if render_key != self._img_key:
            scale = min(cw / frame.shape[1], ch / frame.shape[0])
            dw = max(1, int(frame.shape[1] * scale))
            dh = max(1, int(frame.shape[0] * scale))
            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
            resized = cv2.resize(frame, (dw, dh), interpolation=interpolation)
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
            self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))
            self.preview.itemconfigure(self._img_item, image=self._photo)
            x0 = (cw - dw) // 2
            y0 = (ch - dh) // 2
            self.preview.coords(self._img_item, x0, y0)
            self._img_key = render_key
        else:
            x0 = (cw - frame.shape[1] * min(cw / frame.shape[1], ch / frame.shape[0])) // 2
            y0 = (ch - frame.shape[0] * min(cw / frame.shape[1], ch / frame.shape[0])) // 2
        self._draw_hud(x0, y0, replay_info)

    def _draw_replay_overlay(self, frame: np.ndarray) -> Optional[dict]:
        if not self.replay_metrics:
            return None
        idx = max(0, min(self.replay_index, len(self.replay_metrics) - 1))
        m = self.replay_metrics[idx]
        scale = float(m.get("scale", frame.shape[1] / ANALYSIS_WIDTH))
        if m.get("line"):
            x1, y1, x2, y2 = m["line"]
            p1 = (int(x1 * scale), int(y1 * scale))
            p2 = (int(x2 * scale), int(y2 * scale))
            cv2.line(frame, p1, p2, (0, 255, 120), 2)
        if m.get("tilt") is not None:
            vx = m.get("vx")
            speed_txt = "?" if vx is None else f"{vx * scale:.0f}"
            cv2.putText(frame,
                        f"tilt {m['tilt']:+.1f} deg   speed {speed_txt} px/s",
                        (14, frame.shape[0] - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.65,
                        (0, 255, 120), 2, cv2.LINE_AA)
        elif not m.get("found"):
            cv2.putText(frame, "test target not found in this frame",
                        (14, frame.shape[0] - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 200, 255), 2, cv2.LINE_AA)
        return m

    def _draw_hud(self, x0: int, y0: int, replay_info: Optional[dict]):
        self.preview.delete("hud")
        stats = self.last_stats or {}
        exp = self.control_value("Exposure")
        gain = self.control_value("Gain")
        exp_row = self.control_by_label.get("Exposure")
        exp_txt = "--"
        if exp is not None:
            auto = " AUTO" if exp_row and (exp_row.info.flags & 1) else ""
            exp_txt = f"{exp} ({exposure_text(exp)}{auto})"
        gain_txt = "--" if gain is None else str(gain)

        if self.replay_active and replay_info is not None:
            line1 = f"REPLAY {self.replay_index + 1}/{len(self.replay_frames)}"
            tilt = replay_info.get("tilt")
            vx = replay_info.get("vx")
            scale = float(replay_info.get("scale", 1.0))
            speed = "?" if vx is None else f"{vx * scale:.0f}"
            line2 = (f"bar tilt {tilt:+.1f} deg | speed {speed} px/s | "
                     f"found {replay_info.get('found')}"
                     if tilt is not None else "test target not found in this frame")
            line3 = "Global: bar stays vertical | Rolling: bar tilts with sweep"
        else:
            fps = stats.get("fps", 0.0)
            nominal = stats.get("nominal", 0.0)
            interval = stats.get("interval_ms", 0.0)
            late = stats.get("late", 0)
            if self.actual_info:
                fmt = (f"{self.actual_info['width']}x{self.actual_info['height']} "
                       f"{self.actual_info['fourcc']}")
            else:
                fmt = "no signal"
            if stats.get("fixed_active"):
                out_fps = stats.get("output_fps", 0.0)
                out_interval = stats.get("out_interval_ms", 0.0)
                dup = stats.get("duplicates", 0)
                line1 = f"OUT {out_fps:6.1f}  |  CAM {fps:6.1f}"
                line2 = (f"{fmt} | fixed frame {out_interval:.2f} ms "
                         f"| duplicates {dup}")
            else:
                line1 = f"FPS {fps:6.1f} / {nominal:.0f}"
                line2 = f"{fmt} | frame {interval:.2f} ms | late {late}"
            line3 = f"exp {exp_txt} | gain {gain_txt}"
        self.preview.create_rectangle(x0 + 8, y0 + 8, x0 + 520, y0 + 94,
                                      fill="#000000", stipple="gray25", outline="",
                                      tags="hud")
        self.preview.create_text(x0 + 16, y0 + 14, anchor="nw", text=line1,
                                 fill=ACCENT, font=("Consolas", 17, "bold"),
                                 tags="hud")
        self.preview.create_text(x0 + 16, y0 + 46, anchor="nw", text=line2,
                                 fill="#d9e2ea", font=("Consolas", 10),
                                 tags="hud")
        self.preview.create_text(x0 + 16, y0 + 66, anchor="nw", text=line3,
                                 fill="#d9e2ea", font=("Consolas", 10),
                                 tags="hud")

    # -------------------------------------------------------------------------- close
    def on_close(self):
        self.closing = True
        try:
            self.close_camera(silent=True)
        finally:
            self.controls = None
            self.root.update_idletasks()
            self.root.destroy()
            disable_high_res_timer()
            gc.collect()


# --------------------------------------------------------------------------------------
# Self test
# --------------------------------------------------------------------------------------

def run_self_test() -> int:
    print("=== camera_tool self-test ===")
    enable_high_res_timer()
    ok_all = True

    devices = enumerate_devices()
    print("DirectShow devices:", devices)
    if not devices:
        print("FAIL: no devices")
        disable_high_res_timer()
        return 1
    index = next((i for i, d in enumerate(devices) if "openaicam" in d.lower()), 0)
    print("Using device index", index, "->", devices[index])

    formats = enumerate_formats(index)
    print("Formats:")
    for f in formats:
        print("  ", f)
    if not formats:
        print("FAIL: no formats")
        disable_high_res_timer()
        return 1

    fmt = max(formats, key=lambda f: (f["width"] * f["height"], f["fps_max"]))
    fps = round(fmt["fps_max"])
    print(f"Opening {fmt['width']}x{fmt['height']} {fmt['fourcc']} @ {fps} fps")
    worker = CaptureWorker()
    ok, msg, actual = worker.open(index, fmt, fps)
    print("open:", ok, msg, actual)
    if not ok:
        disable_high_res_timer()
        return 1
    time.sleep(2.5)
    stats = worker.stats()
    print("stats:", stats)
    if stats["fps"] < fps * 0.85:
        print(f"WARN: measured {stats['fps']:.1f} fps, requested {fps}")
    else:
        print(f"PASS: measured {stats['fps']:.1f} fps")

    controls = DShowControls(index)
    infos = controls.discover()
    print(f"Live controls ({len(infos)}):")
    for info in infos:
        print(f"  {info.group:6s} {info.label:14s} {info.minimum}..{info.maximum} "
              f"step={info.step} default={info.default} value={info.value} "
              f"flags={info.flags} caps={info.caps}")
    if not infos:
        ok_all = False
        print("FAIL: no controls discovered")

    # synthetic test-target tilt checks
    def make_target(shear: int) -> np.ndarray:
        image = np.full((720, 1280, 3), 110, np.uint8)
        cv2.rectangle(image, (380, 20), (900, 700), (235, 235, 235), -1)
        bar_w, x0, y0, y1 = 90, 595, 60, 660
        poly = np.array([[x0, y0], [x0 + bar_w, y0],
                         [x0 + bar_w + shear, y1], [x0 + shear, y1]], np.int32)
        cv2.fillPoly(image, [poly], (10, 10, 10))
        return image

    straight = detect_bar(make_target(0))
    sheared = detect_bar(make_target(45))
    tilt_v = None if straight is None else straight["tilt"]
    tilt_s = None if sheared is None else sheared["tilt"]
    print(f"target bar tilt straight={tilt_v}, sheared={tilt_s} "
          f"(expect ~0 and ~4.3)")
    if (tilt_v is None or abs(tilt_v) > 1.0 or tilt_s is None
            or not (3.0 <= tilt_s <= 5.2)):
        ok_all = False
        print("FAIL: bar detector")
    else:
        print("PASS: bar detector")

    # synthetic verdict checks
    rolling_metrics = [
        {"found": True, "tilt": (-3.0 if i % 2 else 3.0),
         "vx": (-2000.0 if i % 2 else 2000.0), "scale": 1.0}
        for i in range(20)]
    global_metrics = [
        {"found": True, "tilt": (0.2, 0.25, 0.3, 0.25)[i % 4],
         "vx": (-2000.0 if i % 2 else 2000.0), "scale": 1.0}
        for i in range(20)]
    rv = evaluate_burst(rolling_metrics)[0]
    gv = evaluate_burst(global_metrics)[0]
    print("verdict synthetic rolling/global:", rv, gv)
    if rv != "rolling" or gv != "global":
        ok_all = False
        print("FAIL: verdict logic")
    else:
        print("PASS: verdict logic")
    ensure_test_target()
    print("test target:", os.path.basename(ensure_test_target()))

    # burst pipeline
    session = worker.start_burst(20)
    deadline = time.time() + 6
    while session is not None and not session.done.is_set() and time.time() < deadline:
        time.sleep(0.05)
    if session is None or not session.done.is_set():
        ok_all = False
        print("FAIL: burst did not finish")
    else:
        print(f"PASS: burst stored {len(session.frames)} frames, "
              f"{len(session.metrics)} metrics")

    # fixed-rate software output: camera native 120 -> 100.000 Hz output
    print(f"Fixed-rate test: camera {fps} fps -> fixed output 100 fps")
    controls.close()
    ok, msg, actual = worker.open(index, fmt, fps, 100.0)
    print("open fixed:", ok, msg, actual)
    if not ok:
        ok_all = False
        print("FAIL: could not open for fixed-rate test")
    else:
        time.sleep(3.2)
        fixed_stats = worker.stats()
        print("fixed stats:", fixed_stats)
        if not (98.0 <= fixed_stats["output_fps"] <= 102.0):
            ok_all = False
            print(f"FAIL: fixed output {fixed_stats['output_fps']:.2f} fps")
        else:
            print(f"PASS: fixed output {fixed_stats['output_fps']:.2f} fps "
                  f"(camera {fixed_stats['fps']:.2f}), "
                  f"interval {fixed_stats['out_interval_ms']:.3f} ms, "
                  f"jitter {fixed_stats['out_jitter_ms']:.3f} ms, "
                  f"duplicates {fixed_stats['duplicates']}")

    worker.close()
    disable_high_res_timer()
    print("=== self-test", "PASS" if ok_all else "FAIL", "===")
    return 0 if ok_all else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()
    enable_dpi_awareness()
    root = tk.Tk()
    CameraApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
