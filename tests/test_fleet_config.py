import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from camera_agent.config import ConfigurationError, Settings
from camera_agent.fleet_config import FleetConfig


class FleetConfigV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "devices.yaml"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    @staticmethod
    def base_settings() -> Settings:
        return Settings.from_env(Path("missing.env"))

    def load(self, environment=None, *, require_mainflux=False) -> FleetConfig:
        with patch.dict(os.environ, environment or {}, clear=True):
            return FleetConfig.from_yaml(
                self.path,
                self.base_settings(),
                require_mainflux=require_mainflux,
            )

    def test_loads_generic_rtsp_metadata_and_effective_rule_overrides(self):
        self.write(
            """
version: 2
fleet:
  preview: false
devices:
  - device_id: warehouse-camera
    display_name: Warehouse camera
    enabled: true
    source_type: rtsp
    source:
      adapter: generic
      host: camera.example.test
      port: 8554
      path: Streaming/Channels/101
      username_env: WAREHOUSE_RTSP_USER
      password_env: WAREHOUSE_RTSP_PASS
    mainflux:
      thing_id: thing-warehouse
      group_id: group-cameras
      thing_name: camera-agent-warehouse-camera
      thing_key_env: WAREHOUSE_THING_KEY
    monitor_roi: [0.1, 0.2, 0.3, 0.4]
    process_interval: 1.5
    rules:
      template: default
      overrides:
        facebook_usage:
          minimum_confidence: 0.81
          cooldown_seconds: 15
        camera_offline:
          trigger_after_seconds: 4
""",
        )
        fleet = self.load(
            {
                "WAREHOUSE_RTSP_USER": "camera user",
                "WAREHOUSE_RTSP_PASS": "p@ss/word",
                "WAREHOUSE_THING_KEY": "thing-secret",
                "MAINFLUX_ENABLED": "true",
            },
            require_mainflux=True,
        )

        self.assertFalse(fleet.preview)
        self.assertEqual(len(fleet.devices), 1)
        device = fleet.devices[0]
        self.assertEqual(device.device_id, "warehouse-camera")
        self.assertEqual(device.display_name, "Warehouse camera")
        self.assertEqual(device.source_type, "rtsp")
        self.assertEqual(device.mainflux_thing_id, "thing-warehouse")
        self.assertEqual(device.mainflux_group_id, "group-cameras")
        self.assertEqual(device.mainflux_thing_name, "camera-agent-warehouse-camera")
        self.assertEqual(device.mainflux_thing_key_env, "WAREHOUSE_THING_KEY")
        self.assertEqual(device.settings.monitor_roi, (0.1, 0.2, 0.3, 0.4))
        self.assertEqual(
            device.settings.rtsp_url,
            "rtsp://camera%20user:p%40ss%2Fword@camera.example.test:8554/"
            "Streaming/Channels/101",
        )
        self.assertEqual(device.rules_config.facebook.minimum_confidence, 0.81)
        self.assertEqual(device.rules_config.facebook.cooldown_seconds, 15.0)
        self.assertEqual(device.rules_config.camera_offline.trigger_after_seconds, 4.0)

    def test_rtsp_url_env_is_mutually_exclusive_with_structured_source(self):
        self.write(
            """
version: 2
fleet: {preview: false}
devices:
  - device_id: url-camera
    display_name: URL camera
    enabled: true
    source_type: rtsp
    source:
      url_env: URL_CAMERA_RTSP_URL
      host: camera.example.test
    mainflux:
      thing_key_env: URL_CAMERA_THING_KEY
""",
        )
        with self.assertRaisesRegex(ConfigurationError, "url_env.*mutually exclusive"):
            self.load(
                {"URL_CAMERA_RTSP_URL": "rtsp://camera/live"},
                require_mainflux=False,
            )

    def test_rtsp_url_env_resolves_without_storing_url_in_yaml(self):
        self.write(
            """
version: 2
fleet: {preview: false}
devices:
  - device_id: url-camera
    display_name: URL camera
    enabled: true
    source_type: rtsp
    source:
      url_env: URL_CAMERA_RTSP_URL
    mainflux:
      thing_key_env: URL_CAMERA_THING_KEY
""",
        )
        fleet = self.load(
            {"URL_CAMERA_RTSP_URL": "rtsp://user:password@camera.example.test/live"}
        )
        device = fleet.devices[0]
        self.assertIn("user:password", device.settings.rtsp_url)
        self.assertNotIn("user:password", device.settings.redacted_rtsp_url)

    def test_loads_adb_window_and_webcam_from_same_nested_schema(self):
        self.write(
            """
version: 2
fleet: {preview: true}
devices:
  - device_id: android-display
    display_name: Android display
    enabled: true
    source_type: adb
    source:
      serial_env: ANDROID_SERIAL
      fps: 1.2
      command_timeout: 5
      require_landscape: true
      minimum_frame_std: 3
    mainflux: {thing_key_env: ANDROID_THING_KEY}
  - device_id: named-window
    display_name: Named window
    enabled: true
    source_type: window
    source:
      title: CAMERA-LIVE
      capture_method: printwindow
      fps: 6
      minimum_frame_std: 3.5
    mainflux: {thing_key_env: WINDOW_THING_KEY}
  - device_id: virtual-camera
    display_name: Virtual camera
    enabled: true
    source_type: webcam
    source:
      index: 2
      backend: dshow
      width: 1920
      height: 1080
    mainflux: {thing_key_env: WEBCAM_THING_KEY}
""",
        )
        fleet = self.load({"ANDROID_SERIAL": "SERIAL-1"})

        self.assertEqual([device.source_type for device in fleet.devices], ["adb", "window", "webcam"])
        adb, window, webcam = fleet.devices
        self.assertEqual(adb.adb_serial, "SERIAL-1")
        self.assertEqual(adb.adb_fps, 1.2)
        self.assertTrue(adb.adb_require_landscape)
        self.assertEqual(window.window_title, "CAMERA-LIVE")
        self.assertEqual(window.window_capture_method, "printwindow")
        self.assertEqual(webcam.webcam_index, 2)
        self.assertEqual(webcam.webcam_width, 1920)

    def test_tapo_adapter_is_a_rtsp_preset_not_a_device_id_branch(self):
        self.write(
            """
version: 2
fleet: {preview: false}
devices:
  - device_id: arbitrary-entry-camera
    display_name: Arbitrary entry camera
    enabled: true
    source_type: rtsp
    source:
      adapter: tapo
      host: camera.example.test
      path: stream2
      username_env: CAMERA_USER
      password_env: CAMERA_PASS
    mainflux: {thing_key_env: CAMERA_THING_KEY}
""",
        )
        fleet = self.load({"CAMERA_USER": "user", "CAMERA_PASS": "password"})
        self.assertEqual(fleet.devices[0].settings.rtsp_adapter, "tapo")
        self.assertTrue(fleet.devices[0].settings.rtsp_url.endswith("/stream2"))

    def test_tapo_physical_alarm_is_optional_and_uses_existing_secret_refs(self):
        self.write(
            """
version: 2
fleet: {preview: false}
devices:
  - device_id: yume-1
    display_name: Yume 1
    enabled: true
    source_type: rtsp
    source:
      adapter: tapo
      host: camera.example.test
      path: stream1
      username_env: CAMERA_USER
      password_env: CAMERA_PASS
    physical_alarm:
      enabled: true
      provider: tapo
      duration_seconds: 2
      cooldown_seconds: 10
    mainflux: {thing_key_env: CAMERA_THING_KEY}
""",
        )
        fleet = self.load({"CAMERA_USER": "user", "CAMERA_PASS": "password"})
        device = fleet.devices[0]
        self.assertTrue(device.physical_alarm_enabled)
        self.assertEqual(device.physical_alarm_provider, "tapo")
        self.assertEqual(device.physical_alarm_duration_seconds, 2.0)
        self.assertEqual(device.physical_alarm_cooldown_seconds, 10.0)
        self.assertEqual(device.settings.rtsp_username, "user")

    def test_tapo_physical_alarm_rejects_non_tapo_sources(self):
        self.write(
            """
version: 2
fleet: {preview: false}
devices:
  - device_id: generic-camera
    display_name: Generic camera
    enabled: false
    source_type: webcam
    source: {index: 0}
    physical_alarm: {enabled: true, provider: tapo}
    mainflux: {thing_key_env: GENERIC_CAMERA_THING_KEY}
""",
        )
        with self.assertRaisesRegex(ConfigurationError, "requires a Tapo RTSP device"):
            self.load()

    def test_zero_enabled_devices_is_valid_for_management(self):
        self.write(
            """
version: 2
fleet: {preview: false}
devices:
  - device_id: future-camera
    display_name: Future camera
    enabled: false
    source_type: webcam
    source: {index: 0, backend: auto}
    mainflux: {thing_key_env: FUTURE_CAMERA_THING_KEY}
""",
        )
        fleet = self.load()
        self.assertEqual(fleet.devices, ())
        self.assertEqual(len(fleet.configured_devices), 1)
        self.assertFalse(fleet.configured_devices[0].enabled)

    def test_disabled_device_is_still_structurally_validated(self):
        self.write(
            """
version: 2
fleet: {preview: false}
devices:
  - device_id: Future-Camera
    display_name: Future camera
    enabled: false
    source_type: webcam
    source: {index: 0}
    mainflux: {thing_key_env: FUTURE_CAMERA_THING_KEY}
""",
        )
        with self.assertRaisesRegex(ConfigurationError, "device_id"):
            self.load()

    def test_rejects_missing_or_unsupported_manifest_version(self):
        for prefix in ("", "version: 1\n"):
            with self.subTest(prefix=prefix):
                self.write(prefix + "fleet: {preview: false}\ndevices: []\n")
                with self.assertRaisesRegex(ConfigurationError, "version.*2"):
                    self.load()

    def test_rejects_unknown_keys_at_every_schema_level(self):
        valid = """
version: 2
fleet:
  preview: false
devices:
  - device_id: generic-camera
    display_name: Generic camera
    enabled: false
    source_type: webcam
    source:
      index: 0
    mainflux:
      thing_key_env: GENERIC_CAMERA_THING_KEY
    rules:
      template: default
      overrides: {}
"""
        cases = {
            "root": valid + "unexpected: true\n",
            "device": valid.replace(
                "    display_name: Generic camera\n",
                "    display_name: Generic camera\n    office_01: true\n",
            ),
            "source": valid.replace(
                "      index: 0\n",
                "      index: 0\n      p2p_port: 9000\n",
            ),
            "mainflux": valid.replace(
                "      thing_key_env: GENERIC_CAMERA_THING_KEY\n",
                "      thing_key_env: GENERIC_CAMERA_THING_KEY\n"
                "      thing_key: literal-secret\n",
            ),
            "rules": valid.replace(
                "      template: default\n",
                "      template: default\n      alarm_level: 5\n",
            ),
        }
        for level, manifest in cases.items():
            with self.subTest(level=level):
                self.write(manifest)
                with self.assertRaisesRegex(ConfigurationError, "unknown"):
                    self.load()

    def test_rejects_duplicate_ids_thing_ids_and_key_references_even_when_disabled(self):
        duplicate_fields = {
            "device id": ("camera-a", "camera-a", "thing-a", "thing-b", "KEY_A", "KEY_B"),
            "Thing id": ("camera-a", "camera-b", "thing-a", "thing-a", "KEY_A", "KEY_B"),
            "Thing key env": ("camera-a", "camera-b", "thing-a", "thing-b", "KEY_A", "KEY_A"),
        }
        for expected, values in duplicate_fields.items():
            with self.subTest(expected=expected):
                first_id, second_id, first_thing, second_thing, first_key, second_key = values
                self.write(
                    f"""
version: 2
fleet: {{preview: false}}
devices:
  - device_id: {first_id}
    display_name: First
    enabled: false
    source_type: webcam
    source: {{index: 0}}
    mainflux: {{thing_id: {first_thing}, thing_key_env: {first_key}}}
  - device_id: {second_id}
    display_name: Second
    enabled: false
    source_type: webcam
    source: {{index: 1}}
    mainflux: {{thing_id: {second_thing}, thing_key_env: {second_key}}}
"""
                )
                with self.assertRaisesRegex(ConfigurationError, expected):
                    self.load()

    def test_dry_run_opportunistically_rejects_duplicate_resolved_key_values(self):
        self.write(
            """
version: 2
fleet: {preview: false}
devices:
  - device_id: camera-a
    display_name: Camera A
    enabled: false
    source_type: webcam
    source: {index: 0}
    mainflux: {thing_key_env: KEY_A}
  - device_id: camera-b
    display_name: Camera B
    enabled: false
    source_type: webcam
    source: {index: 1}
    mainflux: {thing_key_env: KEY_B}
""",
        )
        with self.assertRaisesRegex(ConfigurationError, "different Mainflux Thing key"):
            self.load({"KEY_A": "same-secret", "KEY_B": "same-secret"})

    def test_rejects_invalid_environment_reference_without_echoing_secret(self):
        self.write(
            """
version: 2
fleet: {preview: false}
devices:
  - device_id: generic-camera
    display_name: Generic camera
    enabled: false
    source_type: webcam
    source: {index: 0}
    mainflux: {thing_key_env: NOT-A VALID ENV}
""",
        )
        with self.assertRaisesRegex(ConfigurationError, "environment variable name"):
            self.load()

    def test_rejects_unknown_rule_template(self):
        self.write(
            """
version: 2
fleet: {preview: false}
devices:
  - device_id: generic-camera
    display_name: Generic camera
    enabled: false
    source_type: webcam
    source: {index: 0}
    mainflux: {thing_key_env: GENERIC_CAMERA_THING_KEY}
    rules: {template: office-01, overrides: {}}
""",
        )
        with self.assertRaisesRegex(ConfigurationError, "rules.template.*default"):
            self.load()


if __name__ == "__main__":
    unittest.main()
