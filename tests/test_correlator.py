import unittest

from camera_agent.cyber.correlator import AssetCorrelator
from camera_agent.cyber.models import DiscoveredAsset, ServiceFingerprint


class CorrelatorTests(unittest.TestCase):
    def test_vendor_and_service_can_confirm_but_unknown_stays_unconfirmed(self):
        asset = DiscoveredAsset("192.168.56.20", vendor="TP-Link", hostname="router-lab")
        fingerprints = [ServiceFingerprint("192.168.56.20", 80, "http", "http")]
        result = AssetCorrelator().correlate("router", [asset], fingerprints)[0]
        self.assertEqual(result.status, "CONFIRMED")
        self.assertGreaterEqual(result.confidence, 0.8)

        unknown = AssetCorrelator().correlate("router", [DiscoveredAsset("192.168.56.21")], [])
        self.assertEqual(unknown[0].status, "UNCONFIRMED")


if __name__ == "__main__":
    unittest.main()
