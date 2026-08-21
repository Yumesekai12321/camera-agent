import unittest

from camera_agent.cyber.config import ScopeSettings
from camera_agent.cyber.scope_guard import OutOfScopeTargetError, ScopeGuard


class ScopeGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = ScopeGuard(
            ScopeSettings(
                allowed_subnets=("192.168.56.0/24",),
                allowed_hosts=("10.0.0.8",),
                denied_hosts=("192.168.56.21",),
                deny_public_ips=True,
            )
        )

    def test_allows_private_allowlisted_subnet(self):
        self.assertEqual(self.guard.require_allowed("192.168.56.20"), "192.168.56.20")

    def test_denies_public_ip(self):
        self.assertFalse(self.guard.is_allowed_ip("8.8.8.8"))
        with self.assertRaisesRegex(OutOfScopeTargetError, "DENIED_OUTSIDE_SCOPE"):
            self.guard.require_allowed("8.8.8.8")

    def test_denies_non_whitelisted_private_ip(self):
        with self.assertRaises(OutOfScopeTargetError):
            self.guard.require_allowed("192.168.57.20")

    def test_denies_explicit_deny_and_malformed_ip(self):
        for value in ("192.168.56.21", "not-an-ip"):
            with self.subTest(value=value):
                with self.assertRaises(OutOfScopeTargetError):
                    self.guard.require_allowed(value)


if __name__ == "__main__":
    unittest.main()
