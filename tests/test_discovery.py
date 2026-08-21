import unittest
from unittest.mock import Mock

from camera_agent.cyber.config import DiscoverySettings, ScopeSettings
from camera_agent.cyber.discovery import NetworkDiscovery
from camera_agent.cyber.scope_guard import ScopeGuard


class DiscoveryTests(unittest.TestCase):
    def test_mock_network_is_scoped_and_deduplicated(self):
        guard = ScopeGuard(ScopeSettings(allowed_hosts=("192.168.56.20", "192.168.56.21")))
        calls = []

        def probe(ip, _timeout):
            calls.append(ip)
            return ip == "192.168.56.20"

        discovery = NetworkDiscovery(
            DiscoverySettings(methods=("tcp",), max_hosts_per_minute=100000, max_concurrency=2),
            guard,
            probe=probe,
        )
        assets = discovery.discover()
        self.assertEqual([asset.ip for asset in assets], ["192.168.56.20"])
        self.assertEqual(set(calls), {"192.168.56.20", "192.168.56.21"})

    def test_scope_guard_is_called_before_probe(self):
        guard = Mock()
        guard.settings = ScopeSettings(allowed_hosts=("192.168.56.20",))
        guard.is_allowed_ip.return_value = True
        probe = Mock(return_value=True)
        NetworkDiscovery(
            DiscoverySettings(methods=("tcp",), max_hosts_per_minute=100000),
            guard,
            probe=probe,
        ).discover()
        guard.require_allowed.assert_called()
        self.assertTrue(probe.called)


if __name__ == "__main__":
    unittest.main()
