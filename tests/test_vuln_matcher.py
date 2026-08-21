import tempfile
import unittest
from pathlib import Path

from camera_agent.cyber.config import VulnerabilitySettings
from camera_agent.cyber.models import ServiceFingerprint
from camera_agent.cyber.vuln_matcher import VulnerabilityMatcher


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "cisa.gov" in url:
            return FakeResponse({"vulnerabilities": [{"cveID": "CVE-2024-1234"}]})
        return FakeResponse(
            {
                "vulnerabilities": [
                    {
                        "cve": {
                            "id": "CVE-2024-1234",
                            "descriptions": [{"lang": "en", "value": "Apache HTTP Server 2.4 issue"}],
                            "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 9.1}}]},
                            "configurations": [{"criteria": "cpe:2.3:a:apache:http_server:2.4.57:*"}],
                        }
                    }
                ]
            }
        )


class VulnerabilityMatcherTests(unittest.TestCase):
    def test_matches_mock_nvd_and_cisa_kev_without_fake_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = VulnerabilitySettings(
                sources=("nvd", "cisa_kev"),
                cache_dir=Path(directory),
                allow_remote_sources=True,
                max_remote_requests_per_minute=100000,
            )
            matcher = VulnerabilityMatcher(settings, session=FakeSession())
            findings = matcher.match([
                ServiceFingerprint("192.168.56.20", 80, "http", "http", "2.4.57")
            ])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].cve_id, "CVE-2024-1234")
        self.assertTrue(findings[0].cisa_kev)
        self.assertEqual(findings[0].severity, "critical")

    def test_cache_and_network_failure_fail_gracefully(self):
        with tempfile.TemporaryDirectory() as directory:
            matcher = VulnerabilityMatcher(
                VulnerabilitySettings(cache_dir=Path(directory), allow_remote_sources=False),
            )
            self.assertEqual(matcher.match([ServiceFingerprint("192.168.56.20", 80, "http", "http", "unknown")]), [])


if __name__ == "__main__":
    unittest.main()
