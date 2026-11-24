"""AWS IoT Core MQTT client (mutual-TLS, via awsiotsdk).

Topics (prefix defaults to waterbuoy/<thing>):
  <prefix>/status   published: online/offline (LWT), heartbeat/status replies
  <prefix>/capture  published: one message per stored image (includes "camera": <name>)
  <prefix>/cmd      subscribed: JSON commands, e.g.
                    {"action": "capture"}                      (all cameras)
                    {"action": "capture", "camera": "cam0"}    (one camera)
                    {"action": "set_interval", "seconds": 600}
                    {"action": "status"}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Callable

from .config import Config

log = logging.getLogger(__name__)

CommandHandler = Callable[[dict], None]


class IotClient:
    def __init__(self, cfg: Config, on_command: CommandHandler):
        from awscrt import mqtt
        from awsiot import mqtt_connection_builder

        self._mqtt = mqtt
        self.cfg = cfg
        self.on_command = on_command
        prefix = cfg.topic_prefix
        self.t_status = f"{prefix}/status"
        self.t_capture = f"{prefix}/capture"
        self.t_cmd = f"{prefix}/cmd"

        will = mqtt.Will(
            topic=self.t_status,
            qos=mqtt.QoS.AT_LEAST_ONCE,
            payload=json.dumps({"state": "offline", "reason": "connection_lost"}).encode(),
            retain=False,
        )
        self._conn = mqtt_connection_builder.mtls_from_path(
            endpoint=cfg.iot.endpoint,
            port=cfg.iot.port,
            cert_filepath=str(cfg.iot.cert_path),
            pri_key_filepath=str(cfg.iot.key_path),
            ca_filepath=str(cfg.iot.ca_path),
            client_id=cfg.thing_name,
            clean_session=False,
            keep_alive_secs=cfg.iot.keep_alive_seconds,
            will=will,
            on_connection_interrupted=self._on_interrupted,
            on_connection_resumed=self._on_resumed,
        )

    # --- lifecycle -------------------------------------------------------
    def connect(self, timeout: float = 30) -> None:
        log.info("connecting to %s as %s", self.cfg.iot.endpoint, self.cfg.thing_name)
        self._conn.connect().result(timeout)
        fut, _ = self._conn.subscribe(
            topic=self.t_cmd, qos=self._mqtt.QoS.AT_LEAST_ONCE, callback=self._on_message
        )
        fut.result(timeout)
        log.info("connected; subscribed to %s", self.t_cmd)
        self.publish_status("online")

    def disconnect(self) -> None:
        try:
            self.publish_status("offline", reason="shutdown")
            self._conn.disconnect().result(10)
        except Exception:  # noqa: BLE001
            log.exception("error during disconnect")

    # --- publishing ------------------------------------------------------
    def publish(self, topic: str, body: dict) -> None:
        body.setdefault("thing", self.cfg.thing_name)
        body.setdefault("ts", datetime.now(timezone.utc).isoformat())
        try:
            self._conn.publish(topic, json.dumps(body), self._mqtt.QoS.AT_LEAST_ONCE)
        except Exception:  # noqa: BLE001 - never let telemetry kill capture
            log.exception("publish to %s failed", topic)

    def publish_status(self, state: str, **extra) -> None:
        self.publish(self.t_status, {"state": state, **extra})

    def publish_capture(self, **info) -> None:
        self.publish(self.t_capture, info)

    # --- callbacks -------------------------------------------------------
    def _on_message(self, topic, payload, **_kwargs):
        try:
            cmd = json.loads(payload)
            if not isinstance(cmd, dict):
                raise ValueError("command must be a JSON object")
        except ValueError as e:
            log.warning("bad command on %s: %s", topic, e)
            return
        log.info("command received: %s", cmd)
        try:
            self.on_command(cmd)
        except Exception:  # noqa: BLE001
            log.exception("command handler failed")

    def _on_interrupted(self, connection, error, **_kwargs):
        log.warning("IoT connection interrupted: %s", error)

    def _on_resumed(self, connection, return_code, session_present, **_kwargs):
        log.info("IoT connection resumed (rc=%s, session_present=%s)", return_code, session_present)
        if not session_present:
            fut, _ = connection.subscribe(
                topic=self.t_cmd, qos=self._mqtt.QoS.AT_LEAST_ONCE, callback=self._on_message
            )
            fut.add_done_callback(lambda f: log.info("resubscribed to %s", self.t_cmd))
