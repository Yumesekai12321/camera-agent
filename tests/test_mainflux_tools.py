import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import yaml

from camera_agent.config import ConfigurationError, Settings
from tools.check_mainflux import resolve_device_publish_key, senml_probe
from tools.setup_mainflux_rules import (
    build_parser,
    build_profile,
    build_profile_create_payload,
    build_rules,
    main as setup_mainflux_rules_main,
    normalize_thing_ids,
    parse_device_targets,
    profile_is_current,
    validate_login_input,
)


class MainfluxToolTests(unittest.TestCase):
    def test_probe_is_senml_and_event_is_explicit(self):
        normal = {item["n"]: item["v"] for item in senml_probe()}
        event = {item["n"]: item["v"] for item in senml_probe(rule_event=True)}
        offline = {
            item["n"]: item["v"]
            for item in senml_probe(camera_offline_event=True)
        }
        self.assertEqual(normal["rule_violation_event"], 0)
        self.assertEqual(event["rule_violation_event"], 1)
        self.assertEqual(offline["camera_offline_event"], 1)
        self.assertEqual(offline["camera_online"], 0)

    def test_publish_key_is_selected_by_device_manifest_not_global_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "devices.yaml"
            manifest.write_text(
                yaml.safe_dump(
                    {
                        "version": 2,
                        "fleet": {"preview": False},
                        "devices": [
                            {
                                "device_id": "camera-01",
                                "display_name": "Camera 01",
                                "enabled": False,
                                "source_type": "webcam",
                                "source": {"index": 0},
                                "mainflux": {
                                    "thing_key_env": "CAMERA_01_THING_KEY"
                                },
                                "process_interval": 1.0,
                                "rules": {"template": "default", "overrides": {}},
                            },
                            {
                                "device_id": "camera-02",
                                "display_name": "Camera 02",
                                "enabled": False,
                                "source_type": "webcam",
                                "source": {"index": 1},
                                "mainflux": {
                                    "thing_key_env": "CAMERA_02_THING_KEY"
                                },
                                "process_interval": 1.0,
                                "rules": {"template": "default", "overrides": {}},
                            },
                        ],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "CAMERA_01_THING_KEY": "key-one",
                    "CAMERA_02_THING_KEY": "key-two",
                    "MAINFLUX_THING_KEY": "wrong-global-key",
                },
                clear=True,
            ):
                settings = Settings.from_env(root / "missing.env")
                self.assertEqual(
                    resolve_device_publish_key("camera-02", manifest, settings),
                    "key-two",
                )
                with self.assertRaisesRegex(ConfigurationError, "not found"):
                    resolve_device_publish_key("camera-03", manifest, settings)

    def test_rules_are_bound_to_thing_and_use_event_pulses(self):
        rules = build_rules("camera-01", "thing-id")
        self.assertEqual(len(rules), 2)
        self.assertTrue(
            all(rule["input"]["thing_ids"] == ["thing-id"] for rule in rules)
        )
        fields = {rule["conditions"][0]["field"] for rule in rules}
        self.assertEqual(fields, {"rule_violation_event", "camera_offline_event"})

    def test_rules_are_named_and_isolated_per_device(self):
        first = build_rules("camera-01", "thing-1")
        second = build_rules("camera-02", "thing-2")
        self.assertNotEqual(
            {rule["name"] for rule in first},
            {rule["name"] for rule in second},
        )
        self.assertTrue(all(rule["input"]["thing_ids"] == ["thing-1"] for rule in first))
        self.assertTrue(all(rule["input"]["thing_ids"] == ["thing-2"] for rule in second))
        self.assertEqual(normalize_thing_ids("thing-1"), ["thing-1"])

    def test_cli_device_targets_pair_ids_without_accepting_secrets(self):
        targets = parse_device_targets(
            ["camera-01=thing-1", "camera-02"],
            [],
            [],
        )
        self.assertEqual(targets, [("camera-01", "thing-1"), ("camera-02", None)])
        with self.assertRaisesRegex(ValueError, "Duplicate device_id"):
            parse_device_targets(["camera-01", "camera-01"], [], [])
        with self.assertRaisesRegex(ValueError, "Duplicate Mainflux Thing ID"):
            parse_device_targets(
                ["camera-01=thing-1", "camera-02=thing-1"], [], []
            )

    def test_standalone_cli_never_creates_a_thing_without_a_key_sink(self):
        self.assertIn(
            "tools.add_device --provision-mainflux",
            build_parser().format_help(),
        )
        result = SimpleNamespace(
            device_id="camera-01",
            thing_id="thing-1",
            thing_key=None,
            profile_id="profile-1",
            rule_ids={},
            status="unchanged",
        )
        with (
            patch("tools.setup_mainflux_rules.load_dotenv"),
            patch("tools.setup_mainflux_rules.getpass", return_value="password123"),
            patch("tools.setup_mainflux_rules.MainfluxProvisioner") as provisioner_type,
            patch("builtins.print"),
        ):
            provisioner = provisioner_type.return_value
            provisioner.provision_device.return_value = result

            exit_code = setup_mainflux_rules_main(
                [
                    "--group-id",
                    "group-1",
                    "--device",
                    "camera-01",
                    "--email",
                    "admin@example.com",
                ]
            )

        self.assertEqual(exit_code, 0)
        provisioner.provision_device.assert_called_once_with(
            "camera-01",
            "camera-01",
            thing_id=None,
            dry_run=False,
            allow_create=False,
        )

    def test_mainflux_login_input_matches_server_constraints(self):
        self.assertIsNone(validate_login_input("admin@example.com", "password123"))
        self.assertIn("email", validate_login_input("admin", "password123"))
        self.assertIn("8", validate_login_input("admin@example.com", "short"))
        self.assertIn("non-whitespace", validate_login_input("admin@example.com", "pass word"))

    def test_profile_enables_senml_writer_and_rules(self):
        profile = build_profile()
        self.assertTrue(profile_is_current(profile))
        self.assertEqual(
            profile["config"]["content_type"], "application/senml+json"
        )
        payload = build_profile_create_payload()
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["name"], profile["name"])
        self.assertEqual(profile["metadata"]["provisioning_version"], 2)


if __name__ == "__main__":
    unittest.main()
