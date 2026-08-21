# Thiết lập Cyber Agent trong lab

## 1. Cài dependency

Repo hiện có Python 3.11+ và virtual environment Windows:

```powershell
cd C:\Users\G531\Desktop\Yumesekai\GitHub\camera-agent
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Không cần Scapy/zeroconf để chạy baseline. Discovery sẽ fallback khi raw socket hoặc thư viện tùy chọn không có.

## 2. Cấu hình scope

Chỉnh `config/cyber_agent.yaml` hoặc dùng profile lab:

```powershell
.\.venv\Scripts\python.exe -m camera_agent.cyber.cli --config config\cyber_agent_lab.yaml show-config
```

Chỉ đưa subnet/host thuộc lab vào `allowed_subnets`/`allowed_hosts`. `deny_public_ips: true` và `mode: safe` là mặc định. Không đưa credential-bearing URL vào YAML.

## 3. Chạy độc lập

Dry-run không phát packet:

```powershell
.\.venv\Scripts\python.exe -m camera_agent.cyber.cli run --dry-run
```

Discovery bounded:

```powershell
.\.venv\Scripts\python.exe -m camera_agent.cyber.cli discover
```

Fingerprint một target đã allowlist:

```powershell
.\.venv\Scripts\python.exe -m camera_agent.cyber.cli scan --target 192.168.56.20
```

Audit đầy đủ read-only:

```powershell
.\.venv\Scripts\python.exe -m camera_agent.cyber.cli audit --target 192.168.56.20 --device-class router
```

Target public/ngoài scope phải trả đúng:

```text
DENIED_OUTSIDE_SCOPE
```

## 4. Vulnerability data

Matcher ưu tiên `data/cve_cache/`. `allow_remote_sources: false` là mặc định để pipeline không tự gọi Internet. Khi operator đã phê duyệt outbound tới nguồn chính thức, bật cờ này trong profile lab và giữ `NVD_API_KEY` chỉ trong environment nếu có. Không hard-code CVE, không lưu response body vào log.

## 5. Mainflux

Cyber publisher dùng lại SenML/outbox của repo. Đặt secret bằng environment, không ghi vào YAML:

```powershell
$env:MAINFLUX_TOKEN = "<thing-key-cua-lab>"
$env:MAINFLUX_THING_ID = "<thing-id-cua-lab>"
$env:MAINFLUX_CHANNEL_ID = "<telemetry-channel-id-neu-deployment-co>"
```

Nếu không có channel ID, client dùng `messages_path: /http/messages`. Nếu deployment Mainflux yêu cầu `/http/channels/<id>/messages`, phải điền `MAINFLUX_CHANNEL_ID` và chạy preflight thực tế của deployment. Khi Mainflux offline, event được enqueue vào SQLite outbox trước HTTP; retry tiếp theo do publisher xử lý.

## 6. Tích hợp `agent.py`

Giữ behavior cũ bằng cách không đặt flag:

```powershell
.\.venv\Scripts\python.exe agent.py --dry-run
```

Bật bridge cyber có chủ ý:

```powershell
$env:CYBER_AGENT_ENABLED = "true"
$env:CYBER_AGENT_CONFIG = "config\cyber_agent_lab.yaml"
$env:CYBER_AGENT_DRY_RUN = "true"  # rehearsal đầu tiên
.\.venv\Scripts\python.exe agent.py --no-preview
```

Hook không suy đoán camera đang nhìn thấy router/camera/laptop. Nếu cần thử correlation với class đã biết trong lab, đặt `CYBER_DEVICE_CLASS=router` sau khi operator xác nhận bằng chứng độc lập. Bridge có cooldown và tối đa một pipeline worker.

## 7. Kiểm thử

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q camera_agent tools tests agent.py
.\.venv\Scripts\python.exe agent.py --check-config
```

Tests không cần camera, credential, Mainflux thật hoặc target ngoài lab.
