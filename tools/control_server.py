"""Local PC control server for typed camera-agent commands.

It deliberately binds to loopback by default.  A Mainflux rule can POST the
same constrained command payload through a VPN/reverse proxy after the
operator explicitly configures a token; this tool never exposes a generic
remote-execution endpoint.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

from camera_agent.config import PROJECT_DIR, Settings
from camera_agent.control import CommandStore, ControlCommandError
from camera_agent.control_ui import render_control_ui
from camera_agent.features import FeatureId
from camera_agent.fleet_config import DeviceDefinition, FleetConfig
from camera_agent.local_preview import LocalPreviewStore
from camera_agent.mainflux_control import (
    DesiredStateStore,
    MainfluxControlHub,
    MainfluxControlError,
    MQTTControlSettings,
)
from camera_agent.presence import AgentPresence


MAX_BODY_BYTES = 4096


class ControlHandler(BaseHTTPRequestHandler):
    server: "ControlHTTPServer"

    def log_message(self, _format: str, *_args) -> None:
        # Payloads and credentials must not become HTTP access logs.
        return

    def _send(self, status: int, body: dict[str, object]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_preview(self, device_id: str) -> None:
        device = self.server.devices.get(device_id)
        if device is not None and device.control_preview_url:
            if not device.control_preview_token:
                self._send(404, {"error": "preview unavailable"})
                return
            try:
                response = requests.get(
                    device.control_preview_url,
                    headers={"X-Control-Server-Token": device.control_preview_token},
                    timeout=3,
                    verify=True,
                    stream=True,
                )
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                raw_length = response.headers.get("Content-Length")
                content_length = int(raw_length) if raw_length else None
                if content_type != "image/jpeg" or (content_length is not None and not 0 < content_length <= 5 * 1024 * 1024):
                    raise ValueError("invalid preview response")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > 5 * 1024 * 1024:
                        raise ValueError("preview response too large")
                    chunks.append(chunk)
                data = b"".join(chunks)
                if not data:
                    raise ValueError("empty preview response")
            except (requests.RequestException, ValueError, OSError):
                self._send(404, {"error": "preview unavailable"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        try:
            data = self.server.preview_store.read(device_id)
        except ValueError:
            self._send(404, {"error": "preview unavailable"})
            return
        if data is None:
            self._send(404, {"error": "preview unavailable"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _device_payload(self, device: DeviceDefinition) -> dict[str, object]:
        record = self.server.desired_store.get(device.device_id)
        presence = self.server.desired_store.presence(device.device_id)
        desired = record.state if record is not None else None
        return {
            "device_id": device.device_id,
            "display_name": device.display_name,
            "features": [feature.value for feature in sorted(device.feature_allowed, key=lambda item: item.value)],
            "default_feature": device.feature_default.value,
            "desired_feature": desired.feature.value if desired is not None else device.feature_default.value,
            "runtime_enabled": desired.runtime_enabled if desired is not None else True,
            "generation": desired.generation if desired is not None else 0,
            "applied_feature": record.state.feature.value if record and record.applied_status == "applied" else None,
            "applied_generation": record.applied_generation if record else None,
            "applied_status": record.applied_status if record else None,
            "applied_detail": record.applied_detail if record else None,
            "online": bool(presence and time.time() - presence.last_seen_at <= 45),
            "last_seen_at": presence.last_seen_at if presence else None,
            "mqtt_ready": self.server.control_hub.ready,
        }

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Control-Server-Token", "")
        return bool(self.server.token) and hmac.compare_digest(supplied, self.server.token)

    def _read_json(self) -> dict[str, object] | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send(415, {"error": "Content-Type must be application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if not 0 < length <= MAX_BODY_BYTES:
            self._send(413, {"error": "payload too large or empty"})
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "invalid JSON"})
            return None
        if not isinstance(payload, dict):
            self._send(400, {"error": "JSON body must be an object"})
            return None
        return payload

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"status": "ok", "service": "camera-agent-control-server"})
            return
        if path == "/":
            self._send(200, {"service": "camera-agent-control-server", "ui": "use /ui"})
            return
        if path == "/ui":
            self._serve_ui()
            return
        if path == "/v1/devices":
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            self._send(200, {"devices": [self._device_payload(device) for device in self.server.devices.values()]})
            return
        if path.startswith("/v1/devices/") and path.endswith("/preview"):
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            parts = path.split("/")
            if len(parts) != 5 or not parts[3]:
                self._send(404, {"error": "not found"})
                return
            self._send_preview(parts[3])
            return
        if path.startswith("/v1/commands/"):
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            command = self.server.store.get(path.rsplit("/", 1)[-1])
            if command is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, command.__dict__)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        is_device_command = path.startswith("/v1/devices/") and path.endswith("/commands")
        is_desired = path.startswith("/v1/devices/") and path.endswith("/desired-state")
        if not is_device_command and not is_desired and path != "/v1/mainflux/commands":
            self._send(404, {"error": "not found"})
            return
        payload = self._read_json()
        if payload is None:
            return
        if is_desired:
            parts = path.split("/")
            device_id = parts[3] if len(parts) == 5 else ""
            device = self.server.devices.get(device_id)
            if self.server.devices and device is None:
                self._send(404, {"error": "device not found"})
                return
            try:
                if set(payload) != {"runtime_enabled", "feature"}:
                    raise MainfluxControlError("desired state requires runtime_enabled and feature")
                if not isinstance(payload["runtime_enabled"], bool):
                    raise MainfluxControlError("runtime_enabled must be boolean")
                feature = FeatureId(str(payload["feature"]))
                if device is not None and feature != FeatureId.NONE and feature not in device.feature_allowed:
                    raise MainfluxControlError("feature is not installed for this device")
                record = self.server.control_hub.set_desired(
                    device_id,
                    runtime_enabled=payload["runtime_enabled"],
                    feature=feature,
                )
                if self.server.control_hub.ready:
                    delivery = "sent"
                    command_id = None
                elif self.server.control_hub.configured:
                    # MQTT is explicitly configured, so keep its durable
                    # desired-state delivery as the single authority instead
                    # of also injecting a local command.
                    delivery = "pending_mqtt"
                    command_id = None
                else:
                    # A no-MQTT deployment still needs feature switching to
                    # reach the local agent.  Reuse the durable, typed local
                    # command queue used for PTZ; the agent will reject a
                    # stale or unverified feature safely and ACK the result.
                    command_id = self.server.store.enqueue(
                        device_id,
                        "set_desired_state",
                        {
                            "runtime_enabled": record.state.runtime_enabled,
                            "feature": record.state.feature.value,
                            "generation": record.state.generation,
                        },
                    )
                    delivery = "pending_local"
            except (MainfluxControlError, ValueError) as exc:
                self._send(400, {"error": str(exc)})
                return
            response: dict[str, object] = {
                "generation": record.state.generation,
                "status": delivery,
            }
            if command_id is not None:
                response["command_id"] = command_id
            self._send(202, response)
            return
        if path == "/v1/mainflux/commands":
            device_id = payload.get("device_id")
        else:
            parts = path.split("/")
            device_id = parts[3] if len(parts) == 5 else None
        if self.server.devices and str(device_id or "") not in self.server.devices:
            self._send(404, {"error": "device not found"})
            return
        if self.server.control_hub.configured:
            try:
                sent = self.server.control_hub.publish_command(
                    str(device_id or ""), str(payload.get("action", "")), payload.get("payload", {})
                )
            except (ControlCommandError, MainfluxControlError) as exc:
                self._send(400, {"error": str(exc)})
                return
            if not sent:
                self._send(503, {"error": "Mainflux MQTT control is offline; motion was not queued"})
                return
            self._send(202, {"status": "sent"})
            return
        if self.server.require_presence and not self.server.presence.is_online(str(device_id or "")):
            self._send(503, {"error": "agent offline; start agent.py first"})
            return
        try:
            command_id = self.server.store.enqueue(
                str(device_id or ""),
                str(payload.get("action", "")),
                payload.get("payload", {}),
            )
        except ControlCommandError as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(202, {"command_id": command_id, "status": "pending"})

    def _serve_ui(self) -> None:
        page = render_control_ui()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)
        return

        page = """<!doctype html>
<html lang=\"en\"><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">
<title>Camera Agent Control</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#131b30;--panel2:#192440;--line:#2b3b61;--text:#edf3ff;--muted:#9eaccb;--accent:#67e8f9;--good:#56e39f;--danger:#fb7185;--shadow:0 18px 50px #0006}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#24345d 0,#0b1020 45%);font:15px/1.45 Inter,Segoe UI,system-ui,sans-serif;color:var(--text);min-height:100vh}
.shell{max-width:1180px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin-bottom:22px}.eyebrow{color:var(--accent);font-size:12px;letter-spacing:.16em;text-transform:uppercase;font-weight:700}.title{font-size:clamp(28px,4vw,48px);line-height:1.05;margin:7px 0}.subtitle{color:var(--muted);margin:0}.connection{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:var(--good);box-shadow:0 0 14px var(--good)}
.grid{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(310px,.8fr);gap:18px}.card{background:linear-gradient(145deg,#17213aee,#10182cee);border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);padding:20px}.card h2{font-size:16px;margin:0 0 14px}.preview{background:#070b14;border:1px solid #314363;border-radius:14px;min-height:360px;display:grid;place-items:center;overflow:hidden}.preview img{display:block;max-width:100%;max-height:68vh;width:auto;height:auto;object-fit:contain}.placeholder{text-align:center;color:var(--muted);padding:32px}.placeholder strong{display:block;color:var(--text);font-size:18px;margin-bottom:6px}.fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}.field label{display:block;color:var(--muted);font-size:12px;margin-bottom:5px}.field input{width:100%;background:#0b1224;border:1px solid var(--line);border-radius:10px;color:var(--text);padding:11px 12px;font:inherit;outline:none}.field input:focus{border-color:var(--accent);box-shadow:0 0 0 3px #67e8f922}.field.full{grid-column:1/-1}.range{display:flex;gap:10px;align-items:center}.range input[type=range]{accent-color:var(--accent);flex:1}.range output{min-width:42px;color:var(--accent);font-variant-numeric:tabular-nums}
.actions{display:grid;grid-template-columns:1fr 1fr 1fr;gap:9px;margin-top:16px}.btn{border:1px solid var(--line);border-radius:11px;background:var(--panel2);color:var(--text);padding:11px 12px;font:600 14px inherit;cursor:pointer;transition:.15s transform,.15s background,.15s border-color}.btn:hover{transform:translateY(-1px);border-color:#6e86b8;background:#24345b}.btn:active{transform:translateY(1px)}.btn.primary{background:#164e63;border-color:#2aa5bb;color:#d9fbff}.btn.danger{background:#5b1d35;border-color:#bd4967;color:#ffe5eb}.btn.active{background:#165b49;border-color:#55d9ad;color:#d9fff0}.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;max-width:310px;margin:18px auto 4px}.pad .btn{height:54px}.pad .empty{visibility:hidden}.status{min-height:22px;color:var(--muted);margin:14px 0 0;white-space:pre-wrap}.status.good{color:var(--good)}.status.bad{color:var(--danger)}.hint{color:var(--muted);font-size:12px;margin-top:14px}.badge{display:inline-flex;border-radius:999px;padding:4px 9px;background:#22304d;color:var(--muted);font-size:12px}.badge.live{color:#bafde4;background:#164e3d}.footer{color:#7382a5;font-size:12px;margin-top:18px}@media(max-width:850px){.shell{padding:16px}.top{display:block}.connection{margin-top:12px}.grid{grid-template-columns:1fr}.preview{min-height:240px}}
</style></head><body><main class=shell>
<header class=top><div><div class=eyebrow>Camera Agent / Local Control</div><h1 class=title>Operations console</h1><p class=subtitle>Live local preview, patrol mode and precise PTZ control.</p></div><div class=connection><span class=dot></span>Loopback only · authenticated</div></header>
<div class=grid><section class=card><h2>Live camera <span id=previewBadge class=badge>waiting</span></h2><div class=preview><img id=preview alt=\"Latest camera frame\" hidden><div id=placeholder class=placeholder><strong>Preview is waiting</strong><span>Start the agent with <code>--web-preview</code> to enable pixels here.</span></div></div><p class=footer>Latest frame only · no recording · full aspect ratio preserved.</p></section>
<section class=card><h2>Agent control</h2><div class=fields><div class=field><label for=device>Agent ID</label><input id=device value=\"yume-1\" autocomplete=off></div><div class=field><label for=token>Server token</label><input id=token type=password autocomplete=current-password></div><div class=field full><label for=duration>Move segment duration</label><div class=range><input id=duration type=range min=.2 max=5 step=.1 value=1.8><output id=durationValue>1.8s</output></div></div></div>
<div class=actions><button id=autoOn class=\"btn primary\" onclick=send('set_auto',{enabled:true})>◎ Auto ON</button><button id=autoOff class=btn onclick=send('set_auto',{enabled:false})>○ Auto OFF</button><button class=\"btn danger\" onclick=send('stop',{})>■ STOP</button></div>
<div class=pad><span class=empty></span><button class=btn onclick=move('up')>↑<br><small>Up</small></button><span class=empty></span><button class=btn onclick=move('left')>←<br><small>Left</small></button><button class=\"btn danger\" onclick=send('stop',{})>●</button><button class=btn onclick=move('right')>→<br><small>Right</small></button><span class=empty></span><button class=btn onclick=move('down')>↓<br><small>Down</small></button><span class=empty></span></div>
<div id=status class=status>Enter the token, then choose an action.</div><p class=hint>Arrow keys control PTZ when the cursor is not in a text field. Manual moves are rejected while Auto is ON.</p></section></div></main>
<script>
const $=id=>document.getElementById(id), id=()=>$('device').value.trim(), token=()=>$('token').value, auth=()=>({'X-Control-Server-Token':token()}), status=(text,good=false)=>{$('status').textContent=text;$('status').className='status '+(good?'good':'bad')};
$('duration').oninput=()=>{$('durationValue').textContent=Number($('duration').value).toFixed(1)+'s'};
async function send(action,payload){try{let r=await fetch('/v1/devices/'+encodeURIComponent(id())+'/commands',{method:'POST',headers:{...auth(),'Content-Type':'application/json'},body:JSON.stringify({action,payload})});let b=await r.json();if(!r.ok){status(b.error||'Request failed');return}status('Command '+b.command_id.slice(0,8)+' queued…');poll(b.command_id)}catch(e){status('Control server unavailable')}}
function move(direction){send('move',{direction,duration_seconds:Number($('duration').value)})}
async function poll(command){for(let i=0;i<35;i++){await new Promise(r=>setTimeout(r,100));let r=await fetch('/v1/commands/'+command,{headers:auth()});if(!r.ok)return;let b=await r.json();if(b.status!=='pending'){status(b.status+(b.detail?' · '+b.detail:''),b.status==='executed');return}}}
async function refresh(){if(!token()||!id())return;try{let r=await fetch('/v1/devices/'+encodeURIComponent(id())+'/preview',{headers:auth(),cache:'no-store'});if(!r.ok){$('preview').hidden=true;$('placeholder').hidden=false;$('previewBadge').textContent='offline';return}let url=URL.createObjectURL(await r.blob()),old=$('preview').src;$('preview').src=url;$('preview').hidden=false;$('placeholder').hidden=true;$('previewBadge').textContent='live';$('previewBadge').className='badge live';if(old&&old.startsWith('blob:'))URL.revokeObjectURL(old)}catch(e){}}
setInterval(refresh,500);refresh();document.addEventListener('keydown',e=>{if(['INPUT','TEXTAREA'].includes(document.activeElement.tagName))return;let d={ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right'}[e.key];if(d){e.preventDefault();move(d)}if(e.key==='Escape')send('stop',{})});
</script></body></html>""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)


class ControlHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        store: CommandStore,
        token: str,
        preview_root: Path | None = None,
        require_presence: bool = True,
        devices: tuple[DeviceDefinition, ...] = (),
        desired_store: DesiredStateStore | None = None,
        control_hub: MainfluxControlHub | None = None,
    ) -> None:
        super().__init__(address, ControlHandler)
        self.store = store
        self.token = token
        # The server reads only the current JPEG; it never captures RTSP itself.
        self.preview_store = LocalPreviewStore(preview_root, enabled=True)
        presence_root = preview_root.parent / "agent-presence" if preview_root is not None else None
        self.presence = AgentPresence(presence_root)
        self.require_presence = require_presence
        self.devices = {device.device_id: device for device in devices}
        self.desired_store = desired_store or DesiredStateStore(
            store.path.with_name("control-desired-state.sqlite3")
        )
        self.control_hub = control_hub or MainfluxControlHub(None, self.desired_store)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Loopback control server for Camera Agent PTZ commands.")
    parser.add_argument("--host", default="127.0.0.1", help="Use loopback unless a secured VPN/reverse proxy is configured.")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--store", type=Path, default=PROJECT_DIR / "runtime" / "control-commands.sqlite3")
    parser.add_argument("--devices", type=Path, default=PROJECT_DIR / "config" / "devices.yaml")
    parser.add_argument(
        "--state-store",
        type=Path,
        default=PROJECT_DIR / "runtime" / "control-desired-state.sqlite3",
        help="SQLite durable desired-state store for the central hub.",
    )
    parser.add_argument("--token-env", default="CONTROL_SERVER_TOKEN")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be 1..65535")
    token = os.environ.get(args.token_env, "")
    if not token:
        raise SystemExit(f"Set a long random token in environment variable {args.token_env}.")
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("Refusing non-loopback bind. Put a TLS reverse proxy or VPN in front of this server first.")
    settings = Settings.from_env()
    fleet_config = FleetConfig.from_yaml(args.devices, settings, require_mainflux=False)
    desired_store = DesiredStateStore(args.state_store)
    control_hub = MainfluxControlHub(
        MQTTControlSettings.from_environment(),
        desired_store,
        controller_thing_id=os.environ.get("MAINFLUX_CONTROL_THING_ID"),
        controller_thing_key=os.environ.get("MAINFLUX_CONTROL_THING_KEY"),
        channel_by_device={
            device.device_id: device.mainflux_control_channel_id
            for device in fleet_config.configured_devices
            if device.mainflux_control_channel_id
        },
    )
    control_hub.start()
    server = ControlHTTPServer(
        (args.host, args.port),
        CommandStore(args.store),
        token,
        devices=fleet_config.devices,
        desired_store=desired_store,
        control_hub=control_hub,
    )
    print(f"Camera Agent control server listening at http://{args.host}:{args.port}/ui")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
        control_hub.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
