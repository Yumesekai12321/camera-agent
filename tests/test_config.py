import os
from pathlib import Path
import unittest
from unittest.mock import patch

from camera_agent.config import ConfigurationError, Settings, parse_normalized_roi, redact_url


class ConfigTests(unittest.TestCase):
    def test_generic_rtsp_environment_builds_vendor_neutral_url(self):
        with patch.dict(
            os.environ,
            {
                "RTSP_ADAPTER": "generic",
                "RTSP_HOST": "camera.example.test",
                "RTSP_USERNAME": "camera user",
                "RTSP_PASSWORD": "p@ss/word",
                "RTSP_PATH": "Streaming/Channels/101",
                "RTSP_PORT": "8554",
                "MAINFLUX_ENABLED": "false",
            },
            clear=True,
        ):
            settings = Settings.from_env(Path("does-not-exist.env"))

        self.assertEqual(
            settings.rtsp_url,
            "rtsp://camera%20user:p%40ss%2Fword@camera.example.test:8554/"
            "Streaming/Channels/101",
        )

    def test_rtsp_url_escapes_credentials_and_is_redacted(self):
        with patch.dict(
            os.environ,
            {
                "TAPO_IP": "192.168.1.50",
                "TAPO_USER": "camera user",
                "TAPO_PASS": "p@ss/word",
                "MAINFLUX_ENABLED": "false",
            },
            clear=True,
        ):
            settings = Settings.from_env(Path("does-not-exist.env"))
        self.assertEqual(
            settings.rtsp_url,
            "rtsp://camera%20user:p%40ss%2Fword@192.168.1.50:554/stream1",
        )
        self.assertEqual(
            settings.redacted_rtsp_url,
            "rtsp://***:***@192.168.1.50:554/stream1",
        )

    def test_redact_override_url(self):
        self.assertEqual(
            redact_url("rtsp://admin:secret@camera.local:554/stream1"),
            "rtsp://***:***@camera.local:554/stream1",
        )

    def test_redact_override_url_hides_sensitive_query_values(self):
        redacted = redact_url(
            "rtsp://camera.local/live?token=query-secret&password=also-secret&quality=high"
        )
        self.assertNotIn("query-secret", redacted)
        self.assertNotIn("also-secret", redacted)
        self.assertIn("quality=high", redacted)

    def test_settings_repr_never_contains_source_or_mainflux_secrets(self):
        with patch.dict(
            os.environ,
            {
                "RTSP_URL": "rtsp://user:source-secret@camera.local/live",
                "MAINFLUX_ENABLED": "true",
                "MAINFLUX_THING_KEY": "mainflux-secret",
            },
            clear=True,
        ):
            settings = Settings.from_env(Path("does-not-exist.env"))

        rendered = repr(settings)
        self.assertNotIn("source-secret", rendered)
        self.assertNotIn("mainflux-secret", rendered)

    def test_mainflux_destination_fingerprints_rotated_key_without_exposing_it(self):
        first_key = "first-high-entropy-thing-key"
        second_key = "rotated-high-entropy-thing-key"
        common = {
            "RTSP_URL": "rtsp://camera.local/live",
            "MAINFLUX_ENABLED": "true",
        }
        with patch.dict(
            os.environ,
            {**common, "MAINFLUX_THING_KEY": first_key},
            clear=True,
        ):
            first = Settings.from_env(Path("does-not-exist.env"))
        with patch.dict(
            os.environ,
            {**common, "MAINFLUX_THING_KEY": second_key},
            clear=True,
        ):
            second = Settings.from_env(Path("does-not-exist.env"))

        self.assertTrue(first.mainflux_destination_id.startswith("key-sha256:"))
        self.assertNotEqual(first.mainflux_destination_id, second.mainflux_destination_id)
        self.assertNotIn(first_key, first.mainflux_destination_id)
        self.assertNotIn(second_key, second.mainflux_destination_id)
        self.assertNotIn(first_key, repr(first))

    def test_roi_validation(self):
        self.assertEqual(parse_normalized_roi("0.1,0.2,0.3,0.4"), (0.1, 0.2, 0.3, 0.4))
        with self.assertRaises(ConfigurationError):
            parse_normalized_roi("0.8,0.2,0.3,0.4")

    def test_mainflux_requires_key_when_enabled(self):
        with patch.dict(
            os.environ,
            {"MAINFLUX_ENABLED": "true", "RTSP_URL": "rtsp://camera/stream1"},
            clear=True,
        ):
            settings = Settings.from_env(Path("does-not-exist.env"))
        with self.assertRaisesRegex(ConfigurationError, "MAINFLUX_THING_KEY"):
            settings.validate(require_models=False)


if __name__ == "__main__":
    unittest.main()
