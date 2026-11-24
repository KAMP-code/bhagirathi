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
