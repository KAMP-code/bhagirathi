"""Camera configuration and USB mode selection (no hardware needed)."""

import pytest

from waterbuoy.camera import choose_mode, make_cameras, parse_ffmpeg_formats
from waterbuoy.config import CameraConfig, load_config, parse_cameras

# `ffmpeg -f v4l2 -list_formats all` output of three kinds of camera.
NEXIGO = """\
[video4linux2,v4l2 @ 0x5556] Compressed:       mjpeg :          Motion-JPEG : 320x240 640x480 1280x720 1920x1080
[video4linux2,v4l2 @ 0x5556] Raw       :     yuyv422 :           YUYV 4:2:2 : 320x240 640x480 1280x720
[in#0 @ 0x5555] Error opening input: Immediate exit requested
"""
RAW_ONLY = """\
[video4linux2,v4l2 @ 0x1] Raw       :     yuyv422 :           YUYV 4:2:2 : 640x480 1280x1024 800x600
"""
H264_AND_STEPWISE = """\
[video4linux2,v4l2 @ 0x1] Compressed:        h264 :                H.264 : 1920x1080 1280x720
[video4linux2,v4l2 @ 0x1] Raw       :        nv12 :         Y/UV 4:2:0 : {32-2592, 2}x{32-1944, 2}
"""


def test_parse_formats():
    assert parse_ffmpeg_formats(NEXIGO) == {
        "mjpeg": [(320, 240), (640, 480), (1280, 720), (1920, 1080)],
        "yuyv422": [(320, 240), (640, 480), (1280, 720)],
    }
    assert parse_ffmpeg_formats(H264_AND_STEPWISE) == {"nv12": None}  # h264 is not usable
    assert parse_ffmpeg_formats("Error opening input") == {}


@pytest.mark.parametrize("text, w, h, fmt, expected", [
    (NEXIGO, 0, 0, "auto", ("mjpeg", 1920, 1080)),           # largest
    (NEXIGO, 1280, 720, "auto", ("mjpeg", 1280, 720)),       # mjpeg preferred over raw
    (NEXIGO, 1280, 720, "yuyv422", ("yuyv422", 1280, 720)),  # explicit format
    (RAW_ONLY, 0, 0, "auto", ("yuyv422", 1280, 1024)),       # camera without MJPEG
    (H264_AND_STEPWISE, 2000, 1000, "auto", ("nv12", 2000, 1000)),
])
def test_choose_mode(text, w, h, fmt, expected):
    assert choose_mode(parse_ffmpeg_formats(text), w, h, fmt) == expected


@pytest.mark.parametrize("text, w, h, fmt, match", [
    (NEXIGO, 2304, 1296, "auto", "does not support 2304x1296"),
    (NEXIGO, 1920, 1080, "yuyv422", "does not support 1920x1080"),
    (NEXIGO, 0, 0, "nv12", "does not offer format 'nv12'"),
    (H264_AND_STEPWISE, 0, 0, "auto", "set width/height"),
])
def test_choose_mode_errors(text, w, h, fmt, match):
    with pytest.raises(RuntimeError, match=match):
        choose_mode(parse_ffmpeg_formats(text), w, h, fmt)


def test_capture_defaults_apply_to_every_camera():
    cams = parse_cameras(
        [{"name": "a", "device": "/dev/video0"}, {"name": "b", "backend": "picamera2", "jpeg_quality": 70}],
        defaults={"jpeg_quality": 85, "device": "/dev/video9"},
    )
    assert [(c.name, c.backend, c.device, c.jpeg_quality) for c in cams] == [
        ("a", "usb", "/dev/video0", 85), ("b", "picamera2", "/dev/video9", 70)]


def test_legacy_single_camera_capture_section(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[capture]\ninterval_seconds = 60\nbackend = "picamera2"\nwidth = 2304\nheight = 1296\n')
    cfg = load_config(p)
    assert cfg.capture.interval_seconds == 60
    assert [(c.name, c.backend, c.width) for c in cfg.cameras] == [("cam0", "picamera2", 2304)]


def test_multiple_camera_tables(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[capture]\njpeg_quality = 80\n'
        '[[camera]]\nname = "north"\ndevice = "/dev/video0"\n'
        '[[camera]]\nname = "csi"\nbackend = "rpicam-still"\nindex = 1\n'
    )
    cfg = load_config(p)
    assert [(c.name, c.backend, c.index, c.jpeg_quality) for c in cfg.cameras] == [
        ("north", "usb", 0, 80), ("csi", "rpicam-still", 1, 80)]


@pytest.mark.parametrize("tables, match", [
    ([{"name": "a", "device": "/dev/x"}, {"name": "a", "device": "/dev/y"}], "unique"),
    ([{"name": "bad name", "device": "/dev/x"}], "must be 1-32 chars"),
    ([{"device": "/dev/x", "colour": "red"}], "unknown keys"),
    ([{"backend": "gopro"}], "unknown backend"),
    ([{"backend": "usb"}], "needs `device`"),
    ([{"device": "/dev/x", "width": 640}], "both width and height"),
])
def test_invalid_camera_config(tables, match):
    with pytest.raises(ValueError, match=match):
        parse_cameras(tables)


def test_missing_usb_camera_is_a_startup_error():
    with pytest.raises(RuntimeError, match="no device at /dev/nope"):
        make_cameras([CameraConfig(name="x", backend="usb", device="/dev/nope")])


def test_no_cameras_is_a_startup_error():
    with pytest.raises(RuntimeError, match="no cameras configured"):
        make_cameras([])
