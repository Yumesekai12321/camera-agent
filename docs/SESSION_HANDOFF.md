# Session handoff — Camera Agent

Đọc file này trước khi bắt đầu một session. Đây là bản tóm tắt vận hành; `AGENTS.md` vẫn là
quy tắc bắt buộc, còn `README.md` là tài liệu đầy đủ khi cần sửa runtime/provisioning.

## Trạng thái đã xác minh (2026-08-15)

- Windows project: `C:\Users\G531\Desktop\Yumesekai\GitHub\camera-agent`.
- Manifest canonical: `config/devices.yaml`, schema v2, hiện có `yume-1` enabled.
- `yume-1`: RTSP `172.16.7.62:554/stream1`; ONVIF/PTZ `172.16.7.62:2020`.
- Camera Account, RTSP credentials và Mainflux Thing key chỉ nằm trong `.env`; không in/commit.
- ONVIF probe đã thành công bằng lệnh read-only:

  ```powershell
  .\.venv\Scripts\python.exe -m tools.check_onvif_ptz --device-id yume-1
  ```

- Regression gate hiện tại: `177` unittest PASS, `compileall` PASS và `agent.py --check-config` PASS.
- `yume-1` đang bật thử nghiệm `physical_alarm` Tapo với custom audio ID `8196`; kiểm tra target
  không có `--yes` không phát âm thanh.

## Central control + Person Guard

- Feature runtime độc quyền: `facebook_monitor`, `person_guard`, hoặc `none`; standby dừng reader,
  inference, preview/PTZ nhưng giữ MQTT control để bật lại, không phát offline giả.
- `person_guard` cần `yolo11n.pt` local đã verify SHA-256 qua `python -m tools.verify_person_model`.
  Không train/download lúc runtime, không identity/face/frame persistence. Alert: 2 frame; re-arm:
  3 giây không người; alarm outbox-first, còi chỉ opt-in.
- Local control dùng desired-state SQLite và command queue. Mainflux MQTT dùng
  `channels/<control_channel_id>/messages/camera-agent/...` khi deployment đã có control channel;
  MQTT username=Thing ID/password=Thing key, TLS/VPN. Browser token chỉ sessionStorage. Hub desired-state SQLite có
  generation + ACK/reconnect resend.
- `mainflux.channel_id` nếu deployment có telemetry channel sẽ làm HTTP publisher dùng
  `/http/channels/<id>/messages`; `control_channel_id` là channel private controller+agent.
  Deployment Mainflux hiện tại không có menu Channels và route `/http/messages` đã trả 404, nên
  telemetry live vẫn phải chờ operator xác nhận adapter path; không tự đoán hay sửa secret.

## Chạy thử đúng cách

Control server chỉ ghi command vào SQLite; nó không đọc camera và không thay thế `agent.py`.
Phải chạy một agent và một control server, không chạy hai control server cùng lúc.

PowerShell 1 (agent + live preview opt-in):

```powershell
.\.venv\Scripts\python.exe agent.py --web-preview --no-preview
```

PowerShell 2 (control UI):

```powershell
$controlToken = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }))
$env:CONTROL_SERVER_TOKEN = $controlToken
$controlToken
.\.venv\Scripts\python.exe -m tools.control_server
```

Mở `http://127.0.0.1:8766/ui`, nhập `yume-1` và token. `CONTROL_SERVER_TOKEN` chỉ dành cho
control UI; nó khác Mainflux Thing key.

Agent phải log `Control worker ready`, `Camera connected`. Khi agent không chạy, command mới
trả `503 agent offline` thay vì treo lâu ở `pending`; command cũ quá 120 giây tự hết hạn.

## Luồng runtime

```text
RTSP reader (latest frame + reconnect)
  -> detector/classifier (shared model + inference lock)
  -> decision/rule/cooldown per device
  -> SenML + SQLite outbox -> Mainflux

Control UI -> control server -> control-commands.sqlite3 -> agent command worker -> ONVIF PTZ
Agent --web-preview -> runtime/previews/<device>.jpg -> authenticated loopback UI
```

Preview chỉ giữ một JPEG mới nhất, không ghi recording và không upload Mainflux. Trên Windows,
trình duyệt có thể giữ JPEG mở; `camera_agent/local_preview.py` phải retry `os.replace` và bỏ qua
frame lỗi, tuyệt đối không để preview làm chết agent.

## PTZ/auto hiện tại

- `config/devices.yaml`: `velocity: 0.55`, `move_duration_seconds: 2.0`.
- Auto mặc định trong manifest là `enabled: false`; bật bằng nút `Auto ON` sau khi đã thử manual.
- Patrol quét theo hàng: `RIGHT ×3 -> DOWN -> LEFT ×3 -> DOWN`, mỗi đoạn khoảng 2.0 giây,
  khoảng chuyển bước 2.2 giây; gặp màn hình thì dừng quan sát 5 giây.
- Đây là Facebook Monitor patrol. Với Person Guard, Auto chỉ tracking người đã phát hiện và không
  tự tuần tra để tìm người.
- Manual move chạy trong PTZ worker riêng; không block inference. `STOP` phải dừng đoạn hiện tại.
- ONVIF chỉ dùng Camera Account/Profile S đã công bố; không reverse-engineer P2P.

## Mainflux hiện còn cần operator xác nhận

Runtime hiện đang gửi tới mặc định `/http/messages` và nhận `404` ở deployment hiện tại. Đây là
lỗi route, không phải token (`401/403` mới là lỗi credential). Mainflux HTTP deployments có thể dùng
endpoint có channel, ví dụ `/http/channels/<channel_id>/messages`, nhưng không phải deployment nào
cũng hiển thị Channels. Không sửa bừa secret. Cần xác nhận Mainflux base URL, adapter path và Thing
key env reference trước khi sửa publisher/schema.

## Quy tắc an toàn ngắn gọn

- Không đọc/in `.env` thật khi không cần; YAML chỉ tham chiếu `*_env`.
- Không bật `SAVE_EVIDENCE`, không upload frame, không log credential.
- Giữ state `0 NO_COMPUTER`, `1 COMPUTER_NO_FACEBOOK`, `2 FACEBOOK_DETECTED`, `3 CAMERA_OFFLINE`.
- Camera offline không phải no-computer; không tự mở block.
- Mọi device có state/publisher/outbox riêng; không dùng chung Thing key/Thing ID.
- Chỉ probe pentest read-only trên target/port/path được ủy quyền; không brute-force/exploit.
- ONVIF/RTSP chỉ trong LAN tin cậy hoặc VPN; không port-forward Internet.

## Sau mỗi thay đổi code

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q camera_agent tools tests agent.py
.\.venv\Scripts\python.exe agent.py --check-config
```

Nếu sửa RTSP/ONVIF, operator phải chạy probe trên đúng LAN. Nếu sửa Mainflux, phải giữ SenML
`n/v/u`, Content-Type `application/senml+json`, outbox và test đúng `--device-id`.
