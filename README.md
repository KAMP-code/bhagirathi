# WaterBuoy

Raspberry Pi camera node that takes a picture every 5 minutes, stores it on local disk,
and reports to / takes commands from **AWS IoT Core** over MQTT (mutual TLS).

## Layout

```
src/waterbuoy/
  config.py    TOML config loader
  camera.py    camera backends: picamera2 | rpicam-still | mock
  storage.py   dated folders, atomic writes, free-space retention
  iot.py       AWS IoT Core MQTT client (awsiotsdk)
  app.py       scheduler loop + command handling
config/config.example.toml
systemd/waterbuoy.service
tests/
```

## Setup

```bash
python3 -m venv --system-site-packages .venv   # system site-packages for picamera2
.venv/bin/pip install -e '.[dev]'
cp config/config.example.toml config/config.toml   # then edit
```

Images are written to `<storage.base_dir>/YYYY/MM/DD/YYYYMMDDTHHMMSSZ.jpg` (UTC).
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

## Run

```bash
.venv/bin/python -m waterbuoy -c config/config.toml --once   # single test shot
.venv/bin/python -m waterbuoy -c config/config.toml          # service loop
.venv/bin/pytest                                             # tests (mock camera)
```

As a service:

```bash
sudo cp systemd/waterbuoy.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now waterbuoy
journalctl -u waterbuoy -f
```
