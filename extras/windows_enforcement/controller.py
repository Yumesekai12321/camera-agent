import ctypes
import hmac
import json
import os
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOST = os.getenv("CONTROLLER_HOST", "127.0.0.1")
PORT = int(os.getenv("CONTROLLER_PORT", "8765"))
CONTROLLER_TOKEN = os.getenv("CONTROLLER_TOKEN")
MAX_BODY_BYTES = 64 * 1024

BASE_DIR = Path(__file__).resolve().parent
ENFORCER = BASE_DIR / "facebook_enforcer.ps1"

HOSTS_FILE = Path(
    r"C:\Windows\System32\drivers\etc\hosts"
)

BEGIN_MARKER = "# BEGIN CAMERA-AGENT-FACEBOOK"
END_MARKER = "# END CAMERA-AGENT-FACEBOOK"


# ============================================================
# ADMIN
# ============================================================

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ============================================================
# CURRENT HOSTS STATE
# ============================================================

def is_facebook_blocked():
    try:
        content = HOSTS_FILE.read_text(
            encoding="utf-8",
            errors="ignore"
        )

        return (
            BEGIN_MARKER in content
            and END_MARKER in content
        )

    except Exception as exc:
        print(f"[WARN] Cannot read hosts file: {exc}")
        return None


# ============================================================
# POWERSHELL ENFORCER
# ============================================================

def run_enforcer(action: str):

    if action not in ("Block", "Unblock"):
        raise ValueError(f"Invalid action: {action}")

    if not ENFORCER.exists():
        raise FileNotFoundError(
            f"Cannot find {ENFORCER}"
        )

    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ENFORCER),
        "-Action",
        action,
    ]

    print()
    print(f"[ENFORCER] Running: {action}")

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.stdout.strip():
        print(result.stdout.strip())

    if result.stderr.strip():
        print("[STDERR]")
        print(result.stderr.strip())

    if result.returncode != 0:
        raise RuntimeError(
            f"facebook_enforcer.ps1 failed "
            f"with exit code {result.returncode}"
        )

    return True


# ============================================================
# PARSE AGENT STATE
# ============================================================

def find_agent_state(obj):
    """
    Chịu được nhiều format:

    [
        {"name": "agent_state", "value": 2}
    ]

    hoặc:

    {
        "agent_state": 2
    }

    hoặc Mainflux bọc payload bên ngoài.
    """

    if isinstance(obj, list):

        for item in obj:
            state = find_agent_state(item)

            if state is not None:
                return state

        return None

    if isinstance(obj, dict):

        # Format:
        # {"name":"agent_state","value":2}
        if obj.get("name") == "agent_state":

            try:
                return int(float(obj.get("value")))
            except (TypeError, ValueError):
                return None

        # Format:
        # {"agent_state":2}
        if "agent_state" in obj:

            try:
                return int(float(obj["agent_state"]))
            except (TypeError, ValueError):
                pass

        # Mainflux có thể bọc payload sâu hơn.
        for value in obj.values():

            state = find_agent_state(value)

            if state is not None:
                return state

        return None

    # Có trường hợp payload JSON nằm trong string.
    if isinstance(obj, str):

        try:
            decoded = json.loads(obj)
        except json.JSONDecodeError:
            return None

        return find_agent_state(decoded)

    return None


# ============================================================
# APPLY STATE
# ============================================================

def apply_agent_state(agent_state: int):

    current_blocked = is_facebook_blocked()

    if current_blocked is None:
        raise RuntimeError("Cannot determine current hosts-file state; refusing to act")

    print()
    print(
        f"[DECISION] agent_state={agent_state}, "
        f"currently_blocked={current_blocked}"
    )

    # ---------------------------------------------
    # 2 = FACEBOOK_DETECTED
    # ---------------------------------------------

    if agent_state == 2:

        if current_blocked is True:
            print(
                "[DECISION] Facebook already BLOCKED "
                "-> no action"
            )

            return "already_blocked"

        run_enforcer("Block")

        print(
            "[DECISION] FACEBOOK_DETECTED "
            "-> BLOCK"
        )

        return "blocked"

    # ---------------------------------------------
    # 0 = NO_COMPUTER
    # 1 = COMPUTER_NO_FACEBOOK
    # ---------------------------------------------

    if agent_state in (0, 1):

        if current_blocked is False:
            print(
                "[DECISION] Facebook already UNBLOCKED "
                "-> no action"
            )

            return "already_unblocked"

        run_enforcer("Unblock")

        print(
            f"[DECISION] agent_state={agent_state} "
            "-> UNBLOCK"
        )

        return "unblocked"

    print(
        f"[WARN] Unknown agent_state={agent_state}; "
        "doing nothing."
    )

    return "ignored"


# ============================================================
# HTTP SERVER
# ============================================================

class ControllerHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        return

    def send_json(self, status_code, data):

        body = json.dumps(
            data,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status_code)

        self.send_header(
            "Content-Type",
            "application/json"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    # --------------------------------------------------------

    def do_GET(self):

        if self.path == "/health":

            blocked = is_facebook_blocked()

            self.send_json(
                200,
                {
                    "status": "ok",
                    "service": (
                        "camera-agent-windows-controller"
                    ),
                    "facebook_blocked": blocked,
                },
            )

            return

        self.send_json(
            404,
            {
                "error": "not found"
            },
        )

    # --------------------------------------------------------

    def do_POST(self):

        if self.path != "/mainflux":

            self.send_json(
                404,
                {
                    "error": "not found"
                },
            )

            return

        supplied_token = self.headers.get("X-Controller-Token", "")
        if not CONTROLLER_TOKEN or not hmac.compare_digest(supplied_token, CONTROLLER_TOKEN):
            self.send_json(401, {"error": "unauthorized"})
            return

        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.send_json(415, {"error": "Content-Type must be application/json"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "invalid Content-Length"})
            return
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "payload too large or empty"})
            return

        raw_body = self.rfile.read(
            content_length
        )

        print()
        print("=" * 72)

        print(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            "MAINFLUX WEBHOOK"
        )

        print("=" * 72)

        try:

            body = json.loads(
                raw_body.decode("utf-8")
            )

        except Exception as exc:

            print(
                f"[ERROR] Invalid JSON: {exc}"
            )

            self.send_json(
                400,
                {
                    "error": "invalid JSON"
                },
            )

            return

        print("Body received (content is not logged for privacy).")

        # ------------------------------------------
        # FIND AGENT STATE
        # ------------------------------------------

        agent_state = find_agent_state(body)

        if agent_state is None:

            print()
            print(
                "[INFO] No agent_state in message "
                "-> ignored"
            )

            self.send_json(
                200,
                {
                    "received": True,
                    "action": "ignored",
                    "reason": "no agent_state",
                },
            )

            return

        print()
        print(
            f"[MAINFLUX] agent_state = {agent_state}"
        )

        # ------------------------------------------
        # ENFORCEMENT
        # ------------------------------------------

        try:

            action = apply_agent_state(
                agent_state
            )

        except Exception as exc:

            print()
            print(
                f"[ERROR] Enforcement failed: {exc}"
            )

            self.send_json(
                500,
                {
                    "received": True,
                    "agent_state": agent_state,
                    "error": str(exc),
                },
            )

            return

        print("=" * 72)

        self.send_json(
            200,
            {
                "received": True,
                "agent_state": agent_state,
                "action": action,
            },
        )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 72)
    print("CAMERA AGENT - WINDOWS CONTROLLER")
    print("=" * 72)

    # Controller phải có quyền sửa hosts.
    if not is_admin():

        print()
        print(
            "ERROR: Administrator privileges required."
        )

        print()
        print(
            "Open PowerShell with:"
        )

        print(
            "  Run as administrator"
        )

        print()
        print(
            "Then run:"
        )

        print(
            f"  python {Path(__file__).resolve()}"
        )

        print()

        sys.exit(1)

    if not ENFORCER.exists():

        print(
            f"ERROR: Missing enforcer file:"
        )

        print(
            f"  {ENFORCER}"
        )

        sys.exit(1)

    if not CONTROLLER_TOKEN:
        print("ERROR: Set CONTROLLER_TOKEN to a long random value.")
        sys.exit(1)

    server = ThreadingHTTPServer(
        (HOST, PORT),
        ControllerHandler,
    )

    blocked = is_facebook_blocked()

    print()
    print("Administrator : YES")
    print(f"Listening      : http://0.0.0.0:{PORT}")
    print(f"Webhook        : POST /mainflux")
    print(f"Health         : GET /health")
    print(f"Blocked now    : {blocked}")

    print()
    print("POLICY:")
    print("  agent_state = 2 -> BLOCK")
    print("  agent_state = 1 -> UNBLOCK")
    print("  agent_state = 0 -> UNBLOCK")

    print()
    print("Press Ctrl+C to stop.")
    print("=" * 72)

    try:

        server.serve_forever()

    except KeyboardInterrupt:

        print()
        print("Stopping controller...")

    finally:

        server.server_close()


if __name__ == "__main__":
    main()
