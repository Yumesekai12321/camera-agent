from pathlib import Path
import tempfile
import unittest

from agent import resolve_devices_path


class AgentCliTests(unittest.TestCase):
    def test_explicit_manifest_wins_over_environment_and_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "config" / "devices.yaml"
            canonical.parent.mkdir()
            canonical.write_text("version: 2\n", encoding="utf-8")

            selected = resolve_devices_path(
                Path("chosen.yaml"),
                {"DEVICES_FILE": "from-env.yaml"},
                project_dir=root,
            )

            self.assertEqual(selected, Path("chosen.yaml"))

    def test_canonical_manifest_is_default_without_env_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "config" / "devices.yaml"
            canonical.parent.mkdir()
            canonical.write_text("version: 2\n", encoding="utf-8")

            selected = resolve_devices_path(None, {}, project_dir=root)

            self.assertEqual(selected, canonical)

    def test_legacy_single_source_remains_available_when_manifest_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = resolve_devices_path(None, {}, project_dir=Path(directory))

            self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
