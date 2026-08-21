import unittest

from camera_agent.cyber.config import ScopeSettings, ValidationSettings
from camera_agent.cyber.models import ServiceFingerprint
from camera_agent.cyber.safe_validator import SafeValidator
from camera_agent.cyber.scope_guard import ScopeGuard


class FakeResponse:
    status_code = 200
    headers = {"Server": "hidden-from-evidence", "Content-Type": "text/html"}

    def iter_content(self, chunk_size=4096):
        del chunk_size
        yield b"<html><body>safe</body></html>"

    def close(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = []

    def head(self, url, **kwargs):
        self.calls.append(("HEAD", url, kwargs))
        return FakeResponse()

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse()


class SafeValidatorTests(unittest.TestCase):
    def test_http_headers_and_unauthenticated_endpoint_are_metadata_only(self):
        validator = SafeValidator(
            ValidationSettings(paths=("/", "/health", "/admin"), max_requests_per_minute=100000),
            ScopeGuard(ScopeSettings(allowed_hosts=("192.168.56.20",))),
            session=FakeSession(),
        )
        results = validator.validate([
            ServiceFingerprint("192.168.56.20", 80, "http", "http", "2.4")
        ])
        by_type = {item.validation_type: item for item in results}
        self.assertEqual(by_type["http_security_headers"].status, "FINDING")
        self.assertEqual(by_type["admin_panel_exposure"].status, "FINDING")
        self.assertNotIn("hidden-from-evidence", str(by_type["server_banner_disclosure"].evidence))
        self.assertTrue(all(item.safe for item in results))


if __name__ == "__main__":
    unittest.main()
