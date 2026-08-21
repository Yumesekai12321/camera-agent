import unittest

from camera_agent.cyber.config import RiskSettings
from camera_agent.cyber.models import ValidationResult, VulnerabilityFinding
from camera_agent.cyber.risk_engine import RiskEngine


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = RiskEngine(RiskSettings())

    def finding(self, cvss, severity, kev=False):
        return VulnerabilityFinding("CVE-2099-0001", "test", cvss=cvss, severity=severity, cisa_kev=kev)

    def test_low(self):
        self.assertEqual(self.engine.assess([], []).severity, "low")

    def test_medium(self):
        result = self.engine.assess([self.finding(5.0, "medium")], [], device_class="monitor")
        self.assertEqual(result.severity, "medium")

    def test_high(self):
        result = self.engine.assess([self.finding(8.0, "high")], [ValidationResult("unauthenticated_endpoint_check", "FINDING")], device_class="router")
        self.assertEqual(result.severity, "high")

    def test_critical(self):
        result = self.engine.assess([self.finding(10.0, "critical", True)], [ValidationResult("rtsp_authentication", "FINDING")], device_class="camera")
        self.assertEqual(result.severity, "critical")


if __name__ == "__main__":
    unittest.main()
