# WaterBuoy

Raspberry Pi camera node that takes a picture from each of its cameras every 5 minutes, stores it on local disk,
and reports to / takes commands from **AWS IoT Core** over MQTT (mutual TLS).

## Layout

```
src/waterbuoy/
  config.py    TOML config loader
  camera.py    camera backends: usb (ffmpeg/V4L2) | picamera2 | rpicam-still | mock
  storage.py   dated folders, atomic writes, free-space retention
  iot.py       AWS IoT Core MQTT client (awsiotsdk)
  app.py       scheduler loop + command handling
  algae.py     algal-bloom detection (colour indices) + scan CLI
  servo.py     hobby servo control on any GPIO pin via lgpio
  distance_sensor.py  ultrasonic distance sensor (Trig/Echo) via lgpio
  water_classifier.py clean / green / no water (pretrained YOLOv8, no training)
scripts/servo_demo.py
scripts/distance_demo.py
scripts/water_demo.py
config/config.example.toml
systemd/waterbuoy.service
tests/
```

## Setup

```bash
sudo apt install ffmpeg v4l-utils               # USB webcam capture
python3 -m venv --system-site-packages .venv   # system site-packages (picamera2 for ribbon cameras)
.venv/bin/pip install -e '.[dev]'
cp config/config.example.toml config/config.toml   # then edit
```

### Cameras

Any number of cameras, of any supported type, can be mixed; each is a `[[camera]]` table in
the config. Print ready-made tables for everything attached:

```bash
.venv/bin/python -m waterbuoy --list-cameras
```

| backend | cameras | notes |
|---|---|---|
| `usb` | any UVC webcam | formats and resolutions are probed at start-up; `input_format = "auto"` uses MJPEG when the camera has it, else raw YUV; `width = height = 0` uses the largest mode |
| `picamera2` | Pi ribbon (CSI) camera | kept open between shots; pick with `index` (0, 1) or `device` = libcamera Id |
| `rpicam-still` | Pi ribbon (CSI) camera | one process per shot |

- USB: use the `/dev/v4l/by-id/...-video-index0` link, which survives replugging. Two identical
  webcams often share one by-id name; `--list-cameras` then gives `/dev/v4l/by-path/...` links,
  which are tied to the USB port. The user must be in the `video` group.
- `warmup_frames` (USB) are dropped before each shot so auto-exposure can settle.
- Every configured camera is required: if one is missing at start-up the service exits and
  systemd retries every 10 s. A camera that fails later (e.g. unplugged) is reported as an error
  for that camera only; the others keep capturing.
- An old config with the camera settings directly under `[capture]` still works (one camera, `cam0`).

Images are written to `<storage.base_dir>/YYYY/MM/DD/YYYYMMDDTHHMMSSZ_<camera>.jpg` (UTC).
Set `base_dir` to the external hard drive mount, e.g. `/mnt/hdd/waterbuoy/images`. When free
space drops below `min_free_mb`, the oldest images are deleted.

## AWS IoT

1. In AWS IoT Core create a Thing (name = `device.thing_name`), create & download a certificate,
   attach a policy, and put the files in `certs/`:
   `device.pem.crt`, `private.pem.key`, `AmazonRootCA1.pem`.
2. Get your endpoint: `aws iot describe-endpoint --endpoint-type iot:Data-ATS`.
3. Set `[iot] enabled = true` and `endpoint`.

Example policy (replace region/account):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "iot:Connect",
     "Resource": "arn:aws:iot:REGION:ACCOUNT:client/${iot:Connection.Thing.ThingName}"},
    {"Effect": "Allow", "Action": "iot:Publish",
     "Resource": "arn:aws:iot:REGION:ACCOUNT:topic/waterbuoy/${iot:Connection.Thing.ThingName}/*"},
    {"Effect": "Allow", "Action": "iot:Subscribe",
     "Resource": "arn:aws:iot:REGION:ACCOUNT:topicfilter/waterbuoy/${iot:Connection.Thing.ThingName}/cmd"},
    {"Effect": "Allow", "Action": "iot:Receive",
     "Resource": "arn:aws:iot:REGION:ACCOUNT:topic/waterbuoy/${iot:Connection.Thing.ThingName}/cmd"}
  ]
}
```

### Topics

| Topic | Direction | Payload |
|---|---|---|
| `waterbuoy/<thing>/status` | device → cloud | `online` / `offline` (LWT) / `error`, counters, disk free |
| `waterbuoy/<thing>/capture` | device → cloud | file path, size, timestamp, trigger, disk free |
| `waterbuoy/<thing>/cmd` | cloud → device | `{"action":"capture"}`, `{"action":"set_interval","seconds":600}`, `{"action":"status"}` |

Capture keeps working if the network/IoT is down; the SDK reconnects automatically.

## Algal-bloom detection

With `[algae] enabled = true`, every capture is screened for algal bloom and the result is
added to the `capture` MQTT message as `algae: {label, bloom_fraction, valid_fraction, ...}`.
The label is one of:

| label | meaning |
|---|---|
| `clean` | fewer than `suspect_fraction` of usable pixels are bloom-green |
| `suspect` | between `suspect_fraction` and `bloom_fraction` |
| `bloom` | at least `bloom_fraction` |
| `unknown` | too few usable pixels (night, fog, glare) to decide |

A pixel counts as bloom-green when it is well exposed, saturated (`min_saturation`), has a
green hue (`hue_min`..`hue_max`), green beats red and blue by `green_margin`, and its
excess-green index is above `min_exg`. This is a colour heuristic, not a lab test, so calibrate it
on your own site. Use `roi` to crop out sky, shore vegetation or buoy parts. Fix the camera's
white balance first, because a green cast reads as algae.

Sort stored images into clean / contaminated sets:

```bash
.venv/bin/waterbuoy-algae scan /home/pi/waterbuoy-data/images -c config/config.toml \
    --sort-into /tmp/algae-sorted --overlay --csv /tmp/algae.csv
# -> /tmp/algae-sorted/{clean,suspect,bloom,unknown}/  (symlinks + magenta debug overlays)
```

## Servo

A hobby servo on GPIO16 (physical pin 36): ground (usually brown/black), +5 V (red), signal (orange/yellow/white).
Power it from 5 V and share ground with the Pi; use a separate 5 V supply if the Pi resets
or the servo stutters (a stalled servo can draw several hundred mA). Needs `sudo apt install python3-lgpio`
and the user in the `gpio` group.

```python
from waterbuoy.servo import Servo

with Servo(16) as s:            # defaults: 0-180 deg = 500-2400 us pulses at 50 Hz
    s.angle = 90                # jump
    s.move_to(0, speed=60)      # smooth sweep at 60 deg/s (blocking)
    s.detach()                  # stop pulses: no hum, no holding current
```

If the servo buzzes or strains at an end, narrow the range: `Servo(16, min_pulse_us=600, max_pulse_us=2300)`.

```bash
.venv/bin/python scripts/servo_demo.py              # demo: jumps, sweeps, wave, detach
.venv/bin/python scripts/servo_demo.py --angle 45   # hold 45 deg until Ctrl+C
.venv/bin/pytest tests/integration/test_servo_hw.py -v
```

The integration test moves the servo and reads GPIO16 back to check each pulse width
(within 50 us of the target) and the 50 Hz rate. A hobby servo has no position feedback, so
check by eye that the horn follows.

## Distance sensor

An ultrasonic Trig/Echo sensor (HC-SR04 style): VCC = +5 V, GND = GND, Trig -> GPIO23
(physical pin 16), Echo -> GPIO24 (physical pin 18). Echo is a 5 V signal, so put a
voltage divider on it (e.g. 1 kOhm in series, 2 kOhm to GND) to protect the 3.3 V pin.

```python
from waterbuoy.distance_sensor import DistanceSensor

with DistanceSensor(trig=23, echo=24) as d:
    print(d.read())             # metres, median of 5 pings; None if no echo
```

```bash
.venv/bin/python scripts/distance_demo.py           # live distance to your hand
.venv/bin/pytest tests/integration/test_distance_sensor_hw.py -v
```

The integration test needs the sensor pointed at something still within 2 cm - 4 m.

## Water classifier and green-water demo

`water_classifier.py` labels an image `clean_water`, `green_water` or `no_water` with no
training. A pretrained YOLOv8 ImageNet classifier (`yolov8s-cls.pt`, downloaded once to
`~/waterbuoy-data/models/`) decides *water or not*: the probability it gives to water
scenes, boats and water animals (lakeside, dam, canoe, water snake...) is the water score.
The share of strongly green pixels then separates green from clean water.

```bash
.venv/bin/pip install -e .[vision]          # ultralytics (YOLOv8)
```

```python
from waterbuoy.water_classifier import WaterClassifier

clf = WaterClassifier()
print(clf.classify("testimages/green-water-001.png"))   # green_water (water 0.54, green 0.25)
```

The demo watches the webcam; when it sees green water (2 frames in a row) the servo on
GPIO16 turns to 180 degrees, waits 5 s and returns to 0. Show it water pictures on a phone,
a screen or on paper.

```bash
.venv/bin/python scripts/water_demo.py                      # live webcam + preview window
.venv/bin/python scripts/water_demo.py --no-show            # no window
.venv/bin/python scripts/water_demo.py --images testimages/*   # classify files
.venv/bin/pytest tests/integration/test_water_classifier_hw.py -v
```

Thresholds (`water_threshold=0.15`, `green_threshold=0.10`) were set on the images in
`testimages/`; check them on pictures from the real site.

## Run

```bash
.venv/bin/python -m waterbuoy -c config/config.toml --once   # single test shot
.venv/bin/python -m waterbuoy -c config/config.toml          # service loop
.venv/bin/python -m waterbuoy -c config/config.toml --once --camera cam0   # one camera only
.venv/bin/pytest                                             # unit + hardware tests
```

`tests/integration` needs real hardware: it detects every attached camera and tests each one
(every USB pixel format, auto resolution, all cameras together in one app), and checks that every
camera in `config/config.toml` (or `$WATERBUOY_CONFIG`) works. It fails if no camera is attached.
Run only the unit tests with `.venv/bin/pytest --ignore=tests/integration`.

As a service:

```bash
sudo cp systemd/waterbuoy.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now waterbuoy
journalctl -u waterbuoy -f
```
