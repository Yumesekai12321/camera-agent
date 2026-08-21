import unittest

from camera_agent.cyber.config import FingerprintSettings, ScopeSettings
from camera_agent.cyber.fingerprint import ServiceFingerprinter
from camera_agent.cyber.scope_guard import ScopeGuard


class FakeSocket:
    def __init__(self, data=b"HTTP/1.1 200 OK\r\nServer: Apache/2.4\r\n\r\n"):
        self.data = data
        self.sent = b""

    def sendall(self, payload):
        self.sent += payload

    def recv(self, _size):
        return self.data

    def close(self):
        return None


class FingerprintTests(unittest.TestCase):
    def test_reports_open_and_skips_closed_port(self):
        sockets = {}

        def connector(address, _timeout):
            port = address[1]
            if port == 81:
                raise ConnectionRefusedError()
            sockets[port] = FakeSocket()
            return sockets[port]

        guard = ScopeGuard(ScopeSettings(allowed_hosts=("192.168.56.20",)))
        fingerprinter = ServiceFingerprinter(
            FingerprintSettings(ports=(80, 81), max_connections_per_minute=100000),
            guard,
            connector=connector,
        )
        results = fingerprinter.fingerprint_host("192.168.56.20")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].service, "http")
        self.assertIn("Apache", results[0].banner)
        self.assertIn(b"HEAD /", sockets[80].sent)

    def test_outside_scope_is_rejected_before_connection(self):
        called = []

        def connector(address, _timeout):
            called.append(address)
            return FakeSocket()

        fingerprinter = ServiceFingerprinter(
            FingerprintSettings(ports=(80,), max_connections_per_minute=100000),
            ScopeGuard(ScopeSettings(allowed_hosts=("192.168.56.20",))),
            connector=connector,
        )
        with self.assertRaises(ValueError):
            fingerprinter.fingerprint_host("8.8.8.8")
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
