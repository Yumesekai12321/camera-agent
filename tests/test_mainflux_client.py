import os
import unittest
from unittest.mock import patch

from camera_agent.cyber.config import CyberConfig, MainfluxSettings
from camera_agent.cyber.mainflux_client import CyberMainfluxClient, event_to_senml
from camera_agent.cyber.models import CyberEvent, RiskAssessment


class FakePublisher:
    enabled = True

    def __init__(self):
        self.calls = []

    def publish_payload(self, payload, **kwargs):
        self.calls.append((payload, kwargs))
        return True


class MainfluxClientTests(unittest.TestCase):
    def test_senml_shape_and_publish_success(self):
        event = CyberEvent("agent", "camera", "192.168.56.20", "router", service="http", port=80, risk=RiskAssessment(7.5, "high"))
        payload = event_to_senml(event)
        self.assertTrue(all(set(item) == {"n", "v", "u"} for item in payload))
        with patch.dict(os.environ, {"MAINFLUX_TOKEN": "token", "MAINFLUX_THING_ID": "thing"}, clear=True):
            fake = FakePublisher()
            client = CyberMainfluxClient(
                CyberConfig(mainflux=MainfluxSettings()),
                publisher=fake,
            )
            self.assertTrue(client.publish(event))
            self.assertTrue(fake.calls[0][1]["event"])

    def test_missing_token_is_reported_without_logging_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            client = CyberMainfluxClient(CyberConfig(mainflux=MainfluxSettings()), publisher=FakePublisher())
            with self.assertRaisesRegex(RuntimeError, "token env"):
                client.publish(CyberEvent("agent", "camera", "192.168.56.20", "unknown"))


if __name__ == "__main__":
    unittest.main()
