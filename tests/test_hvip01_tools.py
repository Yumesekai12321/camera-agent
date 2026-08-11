import unittest

from tools.check_hvip01 import parse_adb_devices


class Hvip01ToolTests(unittest.TestCase):
    def test_parses_connected_and_unauthorized_android_devices(self):
        output = """List of devices attached
R58M1234567\tdevice product:phone model:Android transport_id:1
R58M7654321\tunauthorized usb:1-2 transport_id:2

"""

        self.assertEqual(
            parse_adb_devices(output),
            {
                "R58M1234567": "device",
                "R58M7654321": "unauthorized",
            },
        )

    def test_ignores_adb_daemon_messages_and_blank_lines(self):
        output = """* daemon not running; starting now at tcp:5037
* daemon started successfully
List of devices attached

"""

        self.assertEqual(parse_adb_devices(output), {})


if __name__ == "__main__":
    unittest.main()
