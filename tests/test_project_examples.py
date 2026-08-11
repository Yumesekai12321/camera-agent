from pathlib import Path
import unittest

import yaml


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEVICE_MANIFESTS = (
    PROJECT_DIR / "config" / "devices.yaml",
    PROJECT_DIR / "config" / "devices.example.yaml",
    PROJECT_DIR / "config" / "devices.hvip01.yaml",
)
LEGACY_RTSP_FIELDS = {
    "id",
    "name",
    "tapo_ip",
    "tapo_port",
    "tapo_stream",
    "tapo_user_env",
    "tapo_pass_env",
}


class ShippedManifestTests(unittest.TestCase):
    def test_all_shipped_manifests_use_generic_v2_schema(self):
        for path in DEVICE_MANIFESTS:
            with self.subTest(path=path.name):
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertEqual(payload.get("version"), 2)
                for device in payload.get("devices", []):
                    self.assertIn("device_id", device)
                    self.assertIn("display_name", device)
                    self.assertIn("source_type", device)
                    self.assertIsInstance(device.get("source"), dict)
                    self.assertTrue(LEGACY_RTSP_FIELDS.isdisjoint(device))

    def test_shipped_yaml_contains_references_not_secret_values(self):
        for path in DEVICE_MANIFESTS:
            with self.subTest(path=path.name):
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
                for device in payload.get("devices", []):
                    mainflux = device.get("mainflux", {})
                    self.assertNotIn("thing_key", mainflux)
                    if "thing_key_env" in mainflux:
                        self.assertRegex(
                            str(mainflux["thing_key_env"]),
                            r"^[A-Za-z_][A-Za-z0-9_]*$",
                        )
                    source = device.get("source", {})
                    self.assertNotIn("password", source)
                    self.assertNotIn("username", source)
                    self.assertNotIn("url", source)


if __name__ == "__main__":
    unittest.main()
