import copy
from io import StringIO
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from camera_agent.device_registry import (
    DeviceConflictError,
    DeviceRegistry,
    RegistryCommitError,
    RegistryValidationError,
    generated_env_name,
    validate_device_spec,
)
from tools.add_device import build_device_spec, main as add_device_main, onboard_device
from tools.check_device import check_device
from tools.remove_device import remove_device


def rtsp_device(
    device_id="warehouse-01",
    *,
    display_name="Warehouse camera",
    user_env="CAMERA_AGENT_WAREHOUSE_01_RTSP_USER",
    password_env="CAMERA_AGENT_WAREHOUSE_01_RTSP_PASS",
    thing_key_env="CAMERA_AGENT_WAREHOUSE_01_MAINFLUX_THING_KEY",
):
    return {
        "device_id": device_id,
        "display_name": display_name,
        "enabled": True,
        "source_type": "rtsp",
        "source": {
            "adapter": "generic",
            "host": "192.0.2.10",
            "port": 554,
            "path": "Streaming/Channels/101",
            "username_env": user_env,
            "password_env": password_env,
        },
        "mainflux": {
            "thing_id": "thing-warehouse-01",
            "thing_name": f"camera-agent-{device_id}",
            "thing_key_env": thing_key_env,
        },
        "monitor_roi": None,
        "process_interval": 1.5,
        "rules": {"template": "default", "overrides": {}},
    }


class FakeProvisioner:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def provision_device(
        self,
        device_id,
        display_name,
        *,
        thing_id=None,
        thing_key=None,
        dry_run=False,
    ):
        self.calls.append(
            {
                "device_id": device_id,
                "display_name": display_name,
                "thing_id": thing_id,
                "thing_key": thing_key,
                "dry_run": dry_run,
            }
        )
        if self.fail:
            raise RuntimeError("provisioning failed")

        class Result:
            pass

        result = Result()
        result.status = "dry-run" if dry_run else "created"
        result.thing_id = thing_id or "thing-created"
        result.thing_key = None if dry_run else (thing_key or "generated-thing-secret")
        return result


class DeviceToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "devices.yaml"
        self.env_path = self.root / ".env"
        self.manifest_path.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "fleet": {"preview": True},
                    "devices": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.env_path.write_text("KEEP_ME=unchanged\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def registry(self, **kwargs):
        return DeviceRegistry(self.manifest_path, self.env_path, **kwargs)

    @staticmethod
    def secrets(device):
        return {
            device["source"]["username_env"]: "camera-user",
            device["source"]["password_env"]: "camera-password",
            device["mainflux"]["thing_key_env"]: "mainflux-secret",
        }

    def test_empty_and_all_disabled_management_manifests_are_valid(self):
        registry = self.registry()
        self.assertEqual(registry.load_manifest()["devices"], [])

        disabled = rtsp_device()
        disabled["enabled"] = False
        result = registry.add_device(disabled, secrets=self.secrets(disabled))

        self.assertEqual(result.status, "added")
        self.assertFalse(registry.load_manifest()["devices"][0]["enabled"])

    def test_dry_run_is_byte_for_byte_and_does_not_mutate_provisioner(self):
        device = rtsp_device()
        before_manifest = self.manifest_path.read_bytes()
        before_env = self.env_path.read_bytes()
        before_names = set(os.listdir(self.root))
        provisioner = FakeProvisioner()

        def must_not_check_source(_device, _secrets):
            raise AssertionError("dry-run must not open a camera source")

        result = onboard_device(
            self.registry(),
            device,
            secret_values=self.secrets(device),
            provisioner=provisioner,
            provision_mainflux=True,
            dry_run=True,
            checker=must_not_check_source,
        )

        self.assertEqual(result.status, "dry-run")
        self.assertEqual(self.manifest_path.read_bytes(), before_manifest)
        self.assertEqual(self.env_path.read_bytes(), before_env)
        self.assertEqual(set(os.listdir(self.root)), before_names)
        self.assertEqual(len(provisioner.calls), 1)
        self.assertTrue(provisioner.calls[0]["dry_run"])

    def test_cli_enabled_device_uses_default_checker_unless_explicitly_skipped(self):
        base_args = [
            "--manifest",
            str(self.manifest_path),
            "--env-file",
            str(self.env_path),
            "--device-id",
            "virtual-01",
            "--display-name",
            "Virtual camera",
            "--source-type",
            "webcam",
        ]
        with patch("tools.check_device.check_device") as check_device:
            result = add_device_main(
                base_args,
                getpass_fn=lambda _prompt: "mainflux-key",
                output_fn=lambda _message: None,
                environ={},
            )
        self.assertEqual(result, 0)
        check_device.assert_called_once()

        second_manifest = self.root / "devices-second.yaml"
        second_manifest.write_text(
            "version: 2\nfleet: {preview: false}\ndevices: []\n",
            encoding="utf-8",
        )
        skipped_args = list(base_args)
        skipped_args[1] = str(second_manifest)
        skipped_args.extend(["--device-id", "virtual-02", "--skip-source-check"])
        with patch("tools.check_device.check_device") as check_device:
            result = add_device_main(
                skipped_args,
                getpass_fn=lambda _prompt: "mainflux-key-2",
                output_fn=lambda _message: None,
                environ={},
            )
        self.assertEqual(result, 0)
        check_device.assert_not_called()

    def test_source_checker_reads_one_frame_and_skips_disabled_devices(self):
        events: list[object] = []

        class FakeCamera:
            safe_url = "webcam:0 (fake)"
            last_error = None

            def __enter__(self):
                events.append("start")
                return self

            def __exit__(self, *_args):
                events.append("stop")

            def read_latest(self, *, timeout):
                events.append(("read", timeout))
                frame = SimpleNamespace(shape=(480, 640, 3))
                return SimpleNamespace(frame=frame)

        device = build_device_spec(
            device_id="probe-webcam",
            display_name="Probe webcam",
            source_type="webcam",
            source={"index": 0},
            mainflux={},
        )
        result = check_device(
            device,
            {},
            timeout=4.5,
            camera_factory=lambda _device, _secrets: FakeCamera(),
        )

        self.assertTrue(result.checked)
        self.assertEqual((result.width, result.height), (640, 480))
        self.assertEqual(events, ["start", ("read", 4.5), "stop"])

        disabled = copy.deepcopy(device)
        disabled["enabled"] = False
        disabled_result = check_device(
            disabled,
            {},
            camera_factory=lambda _device, _secrets: self.fail(
                "disabled devices must not open a source"
            ),
        )
        self.assertFalse(disabled_result.checked)

    def test_generic_rtsp_cli_defaults_to_auth_refs_and_allows_public_source(self):
        prompts: list[str] = []

        def hidden_value(prompt):
            prompts.append(prompt)
            return f"hidden-{len(prompts)}"

        result = add_device_main(
            [
                "--manifest",
                str(self.manifest_path),
                "--env-file",
                str(self.env_path),
                "--device-id",
                "generic-rtsp",
                "--display-name",
                "Generic RTSP",
                "--source-type",
                "rtsp",
                "--host",
                "camera.example.test",
                "--rtsp-path",
                "live/main",
            ],
            getpass_fn=hidden_value,
            checker=lambda _device, _secrets: None,
            output_fn=lambda _message: None,
            environ={},
        )
        self.assertEqual(result, 0)
        source = self.registry().load_manifest()["devices"][0]["source"]
        self.assertIn("username_env", source)
        self.assertIn("password_env", source)
        self.assertEqual(len(prompts), 3)  # username, password and Mainflux key

        public_manifest = self.root / "devices-public.yaml"
        public_manifest.write_text(
            "version: 2\nfleet: {preview: false}\ndevices: []\n",
            encoding="utf-8",
        )
        public_prompts: list[str] = []
        result = add_device_main(
            [
                "--manifest",
                str(public_manifest),
                "--env-file",
                str(self.root / ".env-public"),
                "--device-id",
                "public-rtsp",
                "--display-name",
                "Public RTSP",
                "--source-type",
                "rtsp",
                "--host",
                "public.example.test",
                "--rtsp-path",
                "live",
                "--rtsp-no-auth",
            ],
            getpass_fn=lambda prompt: public_prompts.append(prompt) or "mainflux-key",
            checker=lambda _device, _secrets: None,
            output_fn=lambda _message: None,
            environ={},
        )
        self.assertEqual(result, 0)
        public_source = DeviceRegistry(
            public_manifest, self.root / ".env-public"
        ).load_manifest()["devices"][0]["source"]
        self.assertNotIn("username_env", public_source)
        self.assertNotIn("password_env", public_source)
        self.assertEqual(len(public_prompts), 1)

    def test_identical_rerun_is_noop_but_conflicting_id_fails(self):
        device = rtsp_device()
        registry = self.registry()
        first = registry.add_device(device, secrets=self.secrets(device))
        after_first_manifest = self.manifest_path.read_bytes()
        after_first_env = self.env_path.read_bytes()

        second = registry.add_device(copy.deepcopy(device), secrets=self.secrets(device))

        self.assertEqual(first.status, "added")
        self.assertEqual(second.status, "unchanged")
        self.assertEqual(self.manifest_path.read_bytes(), after_first_manifest)
        self.assertEqual(self.env_path.read_bytes(), after_first_env)

        conflicting = copy.deepcopy(device)
        conflicting["display_name"] = "Unexpected implicit update"
        with self.assertRaises(DeviceConflictError):
            registry.add_device(conflicting, secrets=self.secrets(device))

    def test_duplicate_environment_reference_is_rejected_across_devices(self):
        first = rtsp_device()
        registry = self.registry()
        registry.add_device(first, secrets=self.secrets(first))
        second = rtsp_device(
            "warehouse-02",
            user_env="CAMERA_AGENT_WAREHOUSE_02_RTSP_USER",
            password_env="CAMERA_AGENT_WAREHOUSE_02_RTSP_PASS",
            thing_key_env=first["mainflux"]["thing_key_env"],
        )
        second["mainflux"]["thing_id"] = "thing-warehouse-02"

        with self.assertRaisesRegex(RegistryValidationError, "environment reference"):
            registry.add_device(second, secrets=self.secrets(second))

    def test_add_preserves_unrelated_top_level_data_and_existing_device(self):
        first = rtsp_device()
        registry = self.registry()
        registry.add_device(first, secrets=self.secrets(first))
        second = build_device_spec(
            device_id="android-wall",
            display_name="Android wall display",
            source_type="adb",
            source={"serial_env": "CAMERA_AGENT_ANDROID_WALL_ADB_SERIAL", "fps": 1.0},
            mainflux={
                "thing_id": "thing-android-wall",
                "thing_key_env": "CAMERA_AGENT_ANDROID_WALL_MAINFLUX_THING_KEY",
            },
        )
        registry.add_device(
            second,
            secrets={
                "CAMERA_AGENT_ANDROID_WALL_ADB_SERIAL": "serial-1",
                "CAMERA_AGENT_ANDROID_WALL_MAINFLUX_THING_KEY": "key-2",
            },
        )

        payload = registry.load_manifest()
        self.assertEqual(payload["fleet"], {"preview": True})
        self.assertEqual(
            [item["device_id"] for item in payload["devices"]],
            ["warehouse-01", "android-wall"],
        )
        self.assertEqual(payload["devices"][0], first)

    def test_builds_pentest_device_without_secret_source_prompts(self):
        device = build_device_spec(
            device_id="security-agent-01",
            display_name="Security assessment agent",
            source_type="pentest",
            source={"target_host": "127.0.0.1", "ports": [80, 443]},
            mainflux={"thing_key_env": "SECURITY_AGENT_01_MAINFLUX_THING_KEY"},
        )
        self.assertEqual(device["source_type"], "pentest")
        self.assertEqual(device["source"]["ports"], [80, 443])
        self.assertNotIn("password", repr(device).lower())

        result = add_device_main(
            [
                "--manifest",
                str(self.manifest_path),
                "--env-file",
                str(self.env_path),
                "--device-id",
                "security-agent-01",
                "--display-name",
                "Security assessment agent",
                "--source-type",
                "pentest",
                "--pentest-target-host",
                "127.0.0.1",
                "--pentest-ports",
                "80,443",
                "--skip-source-check",
                "--dry-run",
            ],
            getpass_fn=lambda _prompt: "mainflux-key",
            output_fn=lambda _message: None,
            environ={},
        )
        self.assertEqual(result, 0)
        self.assertEqual(self.manifest_path.read_text(encoding="utf-8").count("security-agent-01"), 0)

    def test_builds_tapo_camera_backed_pentest_device(self):
        device = build_device_spec(
            device_id="tapo-security-agent",
            display_name="Tapo security agent",
            source_type="rtsp",
            source={
                "adapter": "tapo",
                "host": "192.168.10.20",
                "path": "stream1",
                "username_env": "TAPO_SECURITY_USER",
                "password_env": "TAPO_SECURITY_PASS",
            },
            mainflux={"thing_key_env": "TAPO_SECURITY_THING_KEY"},
            agent_type="pentest",
            pentest={"target_from_source": True, "ports": [554]},
        )
        validate_device_spec(device)
        self.assertEqual(device["agent_type"], "pentest")
        self.assertEqual(device["source_type"], "rtsp")
        self.assertTrue(device["pentest"]["target_from_source"])

    def test_cli_adds_tapo_backed_pentest_in_dry_run_without_opening_source(self):
        result = add_device_main(
            [
                "--manifest",
                str(self.manifest_path),
                "--env-file",
                str(self.env_path),
                "--device-id",
                "tapo-security-agent",
                "--display-name",
                "Tapo security agent",
                "--agent-type",
                "pentest",
                "--source-type",
                "tapo_rtsp",
                "--host",
                "192.168.10.20",
                "--pentest-target-from-source",
                "--pentest-ports",
                "554",
                "--skip-source-check",
                "--dry-run",
            ],
            getpass_fn=lambda prompt: "secret-" + prompt.split(" ")[0],
            output_fn=lambda _message: None,
            environ={},
        )
        self.assertEqual(result, 0)
        self.assertNotIn("tapo-security-agent", self.manifest_path.read_text(encoding="utf-8"))

    def test_checker_and_provisioning_failures_leave_files_untouched(self):
        device = rtsp_device()
        original_manifest = self.manifest_path.read_bytes()
        original_env = self.env_path.read_bytes()

        def failing_checker(_device, _secrets):
            raise RuntimeError("source check failed")

        with self.assertRaisesRegex(RuntimeError, "source check failed"):
            onboard_device(
                self.registry(),
                device,
                secret_values=self.secrets(device),
                checker=failing_checker,
            )
        self.assertEqual(self.manifest_path.read_bytes(), original_manifest)
        self.assertEqual(self.env_path.read_bytes(), original_env)

        with self.assertRaisesRegex(RuntimeError, "provisioning failed"):
            onboard_device(
                self.registry(),
                device,
                secret_values=self.secrets(device),
                provisioner=FakeProvisioner(fail=True),
                provision_mainflux=True,
            )
        self.assertEqual(self.manifest_path.read_bytes(), original_manifest)
        self.assertEqual(self.env_path.read_bytes(), original_env)

    def test_preexisting_thing_key_is_passed_to_provisioner_without_disclosure(self):
        key_reference = "EXISTING_DEVICE_THING_KEY"
        secret = "existing-mainflux-secret"
        self.env_path.write_text(
            f"KEEP_ME=unchanged\n{key_reference}={secret}\n",
            encoding="utf-8",
        )
        provisioner = FakeProvisioner()
        output = StringIO()

        result = add_device_main(
            [
                "--manifest",
                str(self.manifest_path),
                "--env-file",
                str(self.env_path),
                "--device-id",
                "existing-key-device",
                "--display-name",
                "Existing key device",
                "--source-type",
                "webcam",
                "--mainflux-thing-key-env",
                key_reference,
                "--provision-mainflux",
                "--skip-source-check",
            ],
            getpass_fn=lambda _prompt: self.fail("existing key must not be prompted"),
            output_fn=lambda message: print(message, file=output),
            provisioner=provisioner,
            environ={},
        )

        self.assertEqual(result, 0)
        self.assertEqual(provisioner.calls[0]["thing_key"], secret)
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(secret, self.manifest_path.read_text(encoding="utf-8"))

    def test_second_file_commit_failure_rolls_back_manifest_and_env(self):
        device = rtsp_device()
        original_manifest = self.manifest_path.read_bytes()
        original_env = self.env_path.read_bytes()

        def fail_manifest_replace(source, destination):
            if Path(destination) == self.manifest_path:
                raise OSError("injected manifest replace failure")
            os.replace(source, destination)

        registry = self.registry(replace_func=fail_manifest_replace)
        with self.assertRaises(RegistryCommitError):
            registry.add_device(device, secrets=self.secrets(device))

        self.assertEqual(self.manifest_path.read_bytes(), original_manifest)
        self.assertEqual(self.env_path.read_bytes(), original_env)
        self.assertFalse(any(path.suffix == ".tmp" for path in self.root.iterdir()))

    def test_secret_values_never_enter_yaml_or_output(self):
        device = rtsp_device()
        secrets = self.secrets(device)
        output = StringIO()

        result = onboard_device(
            self.registry(),
            device,
            secret_values=secrets,
            output_fn=lambda message: print(message, file=output),
        )

        self.assertEqual(result.status, "added")
        manifest_text = self.manifest_path.read_text(encoding="utf-8")
        combined_output = output.getvalue()
        for secret in secrets.values():
            self.assertNotIn(secret, manifest_text)
            self.assertNotIn(secret, combined_output)
        self.assertIn("thing_key_env", manifest_text)
        self.assertIn("warehouse-01", combined_output)

    def test_validation_rejects_bad_id_bad_env_and_literal_credentials(self):
        bad_id = rtsp_device("Bad ID")
        with self.assertRaisesRegex(RegistryValidationError, "device_id"):
            self.registry().add_device(bad_id, secrets={})

        bad_env = rtsp_device(user_env="not-an-env-name")
        with self.assertRaisesRegex(RegistryValidationError, "environment variable"):
            self.registry().add_device(bad_env, secrets={})

        literal = rtsp_device()
        literal["source"]["password"] = "do-not-put-this-in-yaml"
        with self.assertRaisesRegex(RegistryValidationError, "literal secret"):
            self.registry().add_device(literal, secrets={})

    def test_validation_rejects_rule_overrides_runtime_would_reject(self):
        cases = (
            (
                {"facebook_usage": {"alarm_level": 5}},
                "unknown rule override",
            ),
            (
                {"facebook_usage": {"minimum_confidence": 2.0}},
                "minimum_confidence",
            ),
            (
                {"camera_offline": {"enabled": "yes"}},
                "must be true or false",
            ),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                device = rtsp_device()
                device["rules"]["overrides"] = overrides
                with self.assertRaisesRegex(RegistryValidationError, message):
                    validate_device_spec(device)

    def test_validation_accepts_disabled_onvif_ptz_and_rejects_auto_without_it(self):
        device = rtsp_device()
        device["ptz"] = {
            "enabled": False,
            "provider": "onvif",
            "port": 2020,
            "velocity": 0.35,
            "move_duration_seconds": 0.7,
        }
        device["auto_patrol"] = {"enabled": False}
        validate_device_spec(device)

        device["auto_patrol"] = {"enabled": True}
        with self.assertRaisesRegex(RegistryValidationError, "requires ptz.enabled"):
            validate_device_spec(device)

    def test_cli_redacts_hidden_secret_if_injected_checker_mentions_it(self):
        output = StringIO()
        secret = "never-print-this-secret"

        def checker(_device, _secrets):
            raise RuntimeError(f"checker failed around {secret}")

        result = add_device_main(
            [
                "--manifest",
                str(self.manifest_path),
                "--env-file",
                str(self.env_path),
                "--device-id",
                "virtual-01",
                "--display-name",
                "Virtual camera",
                "--source-type",
                "webcam",
            ],
            getpass_fn=lambda _prompt: secret,
            output_fn=lambda message: print(message, file=output),
            checker=checker,
            environ={},
        )

        self.assertEqual(result, 2)
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("***", output.getvalue())
        self.assertEqual(self.registry().load_manifest()["devices"], [])

    def test_generated_env_names_and_tapo_preset_are_generic(self):
        self.assertEqual(
            generated_env_name("warehouse-01", "rtsp_user"),
            "CAMERA_AGENT_WAREHOUSE_01_RTSP_USER",
        )
        device = build_device_spec(
            device_id="warehouse-01",
            display_name="Warehouse",
            source_type="tapo_rtsp",
            source={"host": "192.0.2.10"},
            mainflux={},
        )
        self.assertEqual(device["source_type"], "rtsp")
        self.assertEqual(device["source"]["adapter"], "tapo")
        self.assertEqual(device["source"]["port"], 554)
        self.assertEqual(device["source"]["path"], "stream1")
        self.assertNotIn("password", device["source"])

    def test_disable_is_isolated_idempotent_and_does_not_touch_env(self):
        first = rtsp_device()
        second = rtsp_device(
            "warehouse-02",
            user_env="CAMERA_AGENT_WAREHOUSE_02_RTSP_USER",
            password_env="CAMERA_AGENT_WAREHOUSE_02_RTSP_PASS",
            thing_key_env="CAMERA_AGENT_WAREHOUSE_02_MAINFLUX_THING_KEY",
        )
        second["mainflux"]["thing_id"] = "thing-warehouse-02"
        registry = self.registry()
        registry.add_device(first, secrets=self.secrets(first))
        registry.add_device(
            second,
            secrets={
                second["source"]["username_env"]: "user-2",
                second["source"]["password_env"]: "password-2",
                second["mainflux"]["thing_key_env"]: "key-2",
            },
        )
        env_before = self.env_path.read_bytes()

        first_result = remove_device(registry, "warehouse-01")
        first_bytes = self.manifest_path.read_bytes()
        second_result = remove_device(registry, "warehouse-01")

        payload = registry.load_manifest()
        self.assertEqual(first_result.status, "disabled")
        self.assertEqual(second_result.status, "unchanged")
        self.assertEqual(self.manifest_path.read_bytes(), first_bytes)
        self.assertFalse(payload["devices"][0]["enabled"])
        self.assertTrue(payload["devices"][1]["enabled"])
        self.assertEqual(self.env_path.read_bytes(), env_before)

    def test_remove_local_requires_confirmation_and_preserves_env(self):
        device = rtsp_device()
        registry = self.registry()
        registry.add_device(device, secrets=self.secrets(device))
        env_before = self.env_path.read_bytes()

        with self.assertRaisesRegex(DeviceConflictError, "confirmation"):
            remove_device(registry, "warehouse-01", remove_local=True)
        result = remove_device(
            registry,
            "warehouse-01",
            remove_local=True,
            confirmed=True,
        )

        self.assertEqual(result.status, "removed")
        self.assertEqual(registry.load_manifest()["devices"], [])
        self.assertEqual(self.env_path.read_bytes(), env_before)


if __name__ == "__main__":
    unittest.main()
