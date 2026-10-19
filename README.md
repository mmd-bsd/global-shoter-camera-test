# High-FPS UVC Camera Console

A real-time Windows desktop tool for exploring a high-frame-rate USB/UVC camera,
adjusting every native control, measuring the **actual** frame rate, and confirming
whether the camera is **global shutter**.

## Detected camera on this machine

| Property | Value |
|---|---|
| DirectShow name | `openaicam` |
| USB ID | `VID_32E6` / `PID_9251` (icSpring, USB 2.0 UVC) |
| Image | monochrome (R=G=B) |
| Modes | 1280x720, 800x600, 640x480 |
| Pixel format | MJPG only |
| FPS range | 60 to 120 fps reported, but **native only 60 and 120** |
| Measured 720p120 | **~117-118 fps** (real frames delivered to the app) |
| Fixed output | **100 fps** via software publisher (10.000 ms ticks) |

Native controls exposed by this camera:

| Control | Range | Default | Auto/manual |
|---|---|---|---|
| Brightness | -128..127 | 0 | manual |
| Contrast | 0..63 | 30 | manual |
| Hue | -180..180 | 0 | manual |
| Saturation | 0..1 | 0 | manual |
| Sharpness | 0..31 | 8 | manual |
| Gamma | 1..500 | 105 | manual |
| White Balance | 2800..6500 K | 4000 | auto/manual |
| Backlight | 0..1 | 0 | manual |
| Gain | 0..63 | 34 | manual |
| Exposure | -13..-1 (log2 seconds = 0.12 ms..500 ms) | -5 | auto/manual |

Not exposed by this camera: Color Enable, Pan, Tilt, Roll, Zoom, Iris, Focus.

> **Auto exposure note:** on this unit DirectShow "auto" mode drives exposure to the
> minimum (-13) and produces a very dark image. The camera has been left at manual
> exposure -5, gain 34. Adjust or toggle auto from the **Controls** tab.

## Install and run

```bat
pip install -r requirements.txt
python camera_tool.py
```

The app opens the first DirectShow device whose name contains "openaicam"
(or device 0) at the highest resolution and 120 fps automatically.

`python camera_tool.py --self-test` runs a non-GUI check of device enumeration,
format enumeration, capture rate, native controls, target detection, verdict logic,
and the burst pipeline.

## What the app shows

* **Live preview** with an overlay showing measured FPS / requested FPS, frame
  interval, late frames, exposure and gain.
* **Live capture panel**: measured FPS (2-second sliding window), frame interval,
  decoder time, jitter, late-frame count, total frames. In fixed-rate mode the big
  number is the fixed output rate and the caption shows the raw camera rate.
* **Fixed-rate output**: select 100 fps (or 110/90/80) in the FPS box. The camera is
  opened at its native 120 fps and frames are published every 10.000 ms (or the
  selected period). Duplicates are counted when the camera stalls.
* **Controls tab**: every UVC control with its real min/max/step/default, a slider,
  exact-value spinbox, live current value, and auto/manual toggle where supported.
  Values are read and written through a second DirectShow filter instance, so they
  change live while the stream is running.
* **Global Shutter Test tab**: printable target, short-exposure burst, automatic
  analysis, slow-motion replay, and a saved report.
* **Info tab**: device, modes, control ranges and measurement notes.

## Fixed 100 fps output

This camera accepts any FPS value in DirectShow but only actually runs at **60 or
120 fps**:

| FPS requested | Actual camera rate |
|---|---|
| 120, 110, 105, 100, 95, 90 | ~117 fps (runs at 120) |
| 80, 75, 60 | ~60 fps |

So 100 fps cannot be a native setting. Selecting **100** in the app's FPS box opens
the camera at native 120 and a publisher thread emits the newest frame every
**10.000 ms (100.000 Hz)**. The publisher uses a high-resolution Windows waitable
timer and elevated thread priority. Raw frames arriving between ticks are skipped;
if the USB stream stalls, the last frame is repeated and counted as a duplicate.

Measured on this machine at 720p:

* fixed output: **100.00 fps average**, 10.003 ms median, ~0.34 ms jitter,
  p99 ~10.8 ms (occasional single 20 ms outlier)
* raw camera: ~117-118 fps
* the big FPS number shows the fixed output; the caption below shows the raw camera rate

The FPS dropdown offers 120, 110, 100, 90, 80, 60; the editable box accepts any value
between 60 and 120. Values other than 60/120 are implemented as fixed-rate software
output. The sensor itself still runs at 120 fps; this is a fixed-rate output stream,
not a hardware 100 fps mode.

For your own processing code, `CaptureWorker.read_latest()` returns
`(frame, frame_id, timestamp)` where the frame and timestamp are updated on the fixed
ticks; `frame_id` changes once per published frame. `CaptureWorker.stats()` exposes
`output_fps`, `out_interval_ms`, `out_jitter_ms` and `duplicates`.

## Confirming global shutter

UVC has **no standard field for shutter type**, so it cannot be read from metadata.
The camera's monochrome, MJPG-only 1280x720@120 USB 2.0 UVC profile matches the
common **OV9281 global-shutter module** family, but that is only a strong hint.
The optical test is the confirmation.

### The test

1. Open/print `gs_target.png` (button **Open test target** in the app). It is a
   solid black bar on a white page. Display it on a phone/tablet or print it.
2. Rest the camera on a table so it does not move.
3. Hold the target upright so the black bar fills most of the frame height.
4. Press **Apply** in *Test target and exposure* (short exposure + high gain).
   If the preview is too dark, pick a longer exposure in the dropdown and/or put a
   desk lamp on the target.
5. Press **Record burst**, and on **GO** sweep the target left-right as fast as you
   can, back and forth, until the burst ends.
6. The app switches to replay automatically. Scrub frame by frame.

**What you see:**

* **Global shutter:** the bar stays vertical. It may be motion-blurred, but it does
  not slant.
* **Rolling shutter:** the bar is sheared into a parallelogram. The slant follows
  the sweep direction and reverses when you reverse the sweep.

**Automatic second opinion:** the app tracks the bar's principal axis, measures its
tilt (degrees from vertical) and its horizontal speed each frame, then computes the
correlation between tilt and velocity over the fast frames.

* `|corr| >= 0.60` with a tilt span `>= 1.5 deg` at high speed -> **rolling shutter**.
* `|corr| <= 0.40` at high speed -> **global shutter** (static tilt and rotation
  wobble do not count, because they are not correlated with sweep direction).
* otherwise -> **inconclusive**; sweep faster or improve the target visibility.

The replay is the definitive check; the numeric verdict is advisory.

Burst frames and `report.txt` are saved under `captures/`.

## How the measured FPS is computed

Timestamps are taken in the capture thread each time `cv2.read()` returns a frame.
FPS is `(N-1)/(t_last - t_first)` over a sliding 2-second window. "Late" counts frame
intervals longer than 1.8x the requested frame period. This shows what actually
arrived, not just what the driver was asked for.

## Files

| File | Purpose |
|---|---|
| `camera_tool.py` | the application |
| `gs_target.png` | printable global-shutter test target (A4, 150 dpi) |
| `requirements.txt` | Python dependencies |
| `captures/` | created when you save a burst |

## Troubleshooting

* **Black or very dark preview** - manual exposure is too short. Raise exposure in
  the Controls tab or pick a longer test exposure. The camera at -13 sees almost
  nothing in a dim room.
* **Target not found / inconclusive** - fill more of the frame height with the black
  bar, keep it upright, light it well, rest the camera, and sweep faster.
* **Measured FPS below 120** - 120 fps is the requested/driver rate; expect
  ~115-119 fps after USB transfer and MJPG decoding on a busy CPU. Close other apps
  and use a USB 2.0 port with no hub if the rate drops much lower.
* **Auto exposure produces a dark image** - this unit's DirectShow auto mode settles
  at the minimum exposure; use manual exposure.
