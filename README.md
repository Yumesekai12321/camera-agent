# Camera Agent multi-device cho Windows và Mainflux

Camera Agent nhận nhiều loại nguồn hình ảnh local, phát hiện màn hình máy tính, phân loại
`facebook_active` / `facebook_mention` / `other`, xác nhận kết quả theo thời gian rồi gửi
telemetry SenML lên Mainflux. Product không phụ thuộc một hãng camera hay một `device_id` cụ thể.

> Bắt đầu session mới: đọc [docs/SESSION_HANDOFF.md](docs/SESSION_HANDOFF.md) trước. File này
> giữ tài liệu đầy đủ; handoff chỉ chứa trạng thái/lệnh cần thiết để tránh đọc lại lịch sử.

Nguồn được hỗ trợ:

- camera RTSP generic, có hoặc không có xác thực;
- Tapo qua RTSP như một adapter preset;
- màn hình Android qua ADB;
- cửa sổ Windows, gồm scrcpy khi cần fallback;
- webcam hoặc virtual webcam.

Tapo C200 và HVIP01 hiện tại chỉ còn là hai adapter/cấu hình thử nghiệm đã được migrate. Thay đổi
này không ngắt thiết bị, không sửa `.env`, không xóa model/dataset/outbox và không thay đổi hay
xóa tài nguyên Mainflux hiện hữu.

Tapo physical siren là actuator thử nghiệm riêng, không phải một phần của RTSP. Nếu bật rõ trong
manifest, agent sẽ cố gọi local Tapo API khi Facebook hoặc Person Guard tạo edge event; một số firmware C200 có
thể từ chối manual siren control.

## Mục tiêu và tiền đề

Đề tài hướng tới một hệ thống agent số đa thiết bị, trong đó mỗi camera được gắn với một agent
để thực hiện một nhiệm vụ đã được lập trình. Một agent chỉ chạy một feature tại một thời điểm,
nhưng nhiều agent có thể hoạt động đồng thời trên nhiều thiết bị.

Các bài toán lớn cần giải quyết gồm:

- **Agents – Camera:** agent có thể kết nối với camera, thực hiện nhiệm vụ, gửi trạng thái và
  điều khiển thiết bị khi được cấp quyền.
- **Nền tảng quản lý – Mainflux:** nền tảng có thể tiếp nhận dữ liệu, quản lý thiết bị và hỗ trợ
  theo dõi các sự kiện từ agent.
- **Máy tính:** máy tính có thể cung cấp môi trường, tài nguyên và kết nối cần thiết để chạy
  nhiều agent, đồng thời cho phép bổ sung feature mới khi hệ thống mở rộng.

## Trạng thái xác minh ngày 2026-08-15

- Manifest canonical đã là schema v2 generic; v1 bị từ chối rõ ràng.
- Runtime không còn nhánh theo `office-01`, `hvip01`, C200 hay ONE Home trong `agent.py` và
  `camera_agent/`.
- Onboarding, registry transaction, source preflight, provisioning idempotent, rule isolation,
  inference serialization và durable outbox đều có unit test không dùng secret/camera thật.
- Regression hiện tại: `177/177` unittest PASS, `compileall` PASS và `agent.py --check-config` PASS.
- `config/devices.yaml` hiện có một device enabled: `yume-1`.
- `yume-1` đã bật thử nghiệm physical alarm Tapo với custom audio `8196`; lệnh kiểm tra không có
  `--yes` chỉ xác minh target, không phát âm thanh.
- Control local dùng SQLite nên có thể vận hành không cần Mainflux channel control. Desired feature
  được phát lại sau khi agent khởi động lại; MQTT control là lựa chọn mở rộng khi deployment đã có
  controller Thing và control channel.
- Đã chạy ONVIF probe read-only cho `yume-1` và phát hiện Profile S; không chạy movement vật lý
  trong probe, không đăng nhập/provision Mainflux và không tạo/xóa remote resource. Movement thật
  chỉ dùng lệnh có `--yes` sau khi operator xác nhận.
- Model production vẫn là model ba lớp hiện hữu; thay đổi này không train hay thay model. Chưa
  có bằng chứng domain-independent để tuyên bố model chính xác cho camera/source mới.

Đây là tín hiệu phân loại hình ảnh có false positive/false negative, không phải bằng chứng tuyệt
đối về hành vi của một người. Mọi cảnh báo cần human review.

## Kiến trúc

```text
devices.yaml v2 + environment references
             │
             ├── RTSP / ADB / window / webcam reader
             │       chỉ giữ latest frame + reconnect riêng
             ▼
        shared YOLO + shared MobileNet
        một inference lock cho toàn fleet
             ▼
  decision history + local rule/cooldown riêng từng device
             ├── fleet dashboard compact/off, không có camera pixels
             ├── optional Tapo physical siren actuator
             └── SQLite outbox (device + destination)
                        ▼
             Mainflux SenML Thing riêng
                        ▼
        rule riêng theo device → Alarm
```

Mỗi device có reader/reconnect, decision engine, local rule engine, publisher, Thing key và
outbox partition riêng. Chỉ model nặng được dùng chung; cả detector lẫn classifier chạy tuần tự
để tối ưu tài nguyên phần cứng.

Mã state không đổi:

| Code | State | Ý nghĩa |
|---:|---|---|
| 0 | `NO_COMPUTER` | Không tìm thấy màn hình |
| 1 | `COMPUTER_NO_FACEBOOK` | Có màn hình, chưa đủ bằng chứng Facebook active |
| 2 | `FACEBOOK_DETECTED` | Đã vượt temporal/confidence gate |
| 3 | `CAMERA_OFFLINE` | Không nhận được frame trong thời gian cho phép |

`CAMERA_OFFLINE` là state riêng, không được quy thành no-computer.

## Manifest thiết bị v2

Runtime mặc định đọc `config/devices.yaml`; có thể override bằng `--devices` hoặc
`DEVICES_FILE`. File YAML chỉ chứa tên biến môi trường, không chứa password, credential-bearing
RTSP URL hoặc Thing key.

Ví dụ RTSP generic:

```yaml
version: 2

fleet:
  preview: true

devices:
  - device_id: lobby-01
    display_name: Lobby camera
    enabled: true
    source_type: rtsp
    source:
      adapter: generic
      host: 192.168.1.50
      port: 554
      path: Streaming/Channels/101
      username_env: CAMERA_AGENT_LOBBY_01_RTSP_USER
      password_env: CAMERA_AGENT_LOBBY_01_RTSP_PASS
      read_timeout: 8
      reconnect_initial: 1
      reconnect_max: 15
    mainflux:
      thing_name: camera-agent-lobby-01
      thing_key_env: CAMERA_AGENT_LOBBY_01_MAINFLUX_THING_KEY
      # thing_id và group_id là optional; onboarding có thể tự tìm/tạo Thing.
    # Optional, experimental, only for structured Tapo RTSP devices.
    physical_alarm:
      enabled: false
      provider: tapo
      duration_seconds: 3
      cooldown_seconds: 30
    monitor_roi: [0.10, 0.15, 0.70, 0.65]
    process_interval: 1.5
    rules:
      template: default
      overrides:
        facebook_usage:
          minimum_confidence: 0.75
          trigger_after_seconds: 4
          cooldown_seconds: 60
        camera_offline:
          trigger_after_seconds: 2
```

Các source-specific field đầy đủ có trong `config/devices.example.yaml`:

| `source_type` | Field chính | Ghi chú |
|---|---|---|
| `rtsp` | `host`, `port`, `path`, `username_env`, `password_env` | `adapter: generic` hoặc `tapo`; có thể dùng một `url_env` thay structured fields |
| `adb` | `serial_env`, `fps`, `command_timeout`, `require_landscape` | Bỏ `serial_env` khi chỉ có một Android device |
| `window` | `title`, `capture_method`, `fps` | `auto`, `printwindow` hoặc `screen` |
| `webcam` | `index`, `backend`, `width`, `height` | Backend `auto`, `dshow` hoặc `msmf` |
| `pentest` agent | `agent_type: pentest` + `pentest` block | Gắn lên RTSP/Tapo/ADB/window/webcam; đánh giá bounded, không khai thác |

Validator kiểm tra cả device disabled, reject unknown field, duplicate `device_id`, Thing ID,
Thing-key reference và resolved Thing key. Manifest không có device enabled vẫn hợp lệ cho thao
tác quản lý/config-check; chạy runtime thật cần ít nhất một device enabled.

## Cài đặt

Project yêu cầu Python 3.11 trở lên. Trên máy hiện tại đã có `.venv`; nếu cần tạo lại:

```powershell
cd C:\Users\Admin\camera-agent\camera-agent
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Nếu chưa có `.env`:

```powershell
Copy-Item .env.example .env
notepad .env
```

Không commit, chụp màn hình hay gửi `.env`. Không port-forward RTSP 554 ra Internet; dùng LAN
tin cậy hoặc VPN.

## Thêm thiết bị

### Interactive

Lệnh đơn giản nhất:

```powershell
.\.venv\Scripts\python.exe -m tools.add_device
```

Tool hỏi `device_id`, display name, source type và đúng thông tin source cần thiết. Camera
username/password, credential-bearing URL, Mainflux Thing key và Mainflux password đều dùng
prompt ẩn. YAML chỉ nhận tên environment variable; secret được ghi local vào `.env` cùng
transaction với manifest.

Mặc định, một device enabled phải đọc được một fresh frame trước khi provisioning/commit. Nếu
source check thất bại, YAML và `.env` không đổi. `--dry-run` chỉ validate/plan, không mở source,
không ghi file và không mutate Mainflux. Chỉ dùng `--skip-source-check` khi đã hiểu thiết bị chưa
sẵn sàng; cấu hình vẫn được validate.

### PowerShell — RTSP mới và tạo Mainflux Thing

Chạy rehearsal trước:

```powershell
.\.venv\Scripts\python.exe -m tools.add_device `
  --device-id lobby-01 `
  --display-name "Lobby camera" `
  --source-type rtsp `
  --host 192.168.1.50 `
  --rtsp-path Streaming/Channels/101 `
  --process-interval 1.5 `
  --mainflux-group-id GROUP_UUID `
  --mainflux-email operator@example.com `
  --provision-mainflux `
  --dry-run
```

Nếu plan đúng, bỏ `--dry-run` và chạy lại. Tool sẽ:

1. tạo tên environment variable ổn định từ `device_id`;
2. prompt ẩn Camera Account;
3. validate và probe một frame;
4. đăng nhập Mainflux bằng password prompt ẩn;
5. tìm hoặc tạo Thing deterministic trong Group, gán SenML profile và hai rule riêng;
6. xác minh postcondition rồi commit `.env` + manifest.

RTSP structured mặc định có xác thực. Chỉ với stream public thật sự mới thêm
`--rtsp-no-auth`. Tapo dùng cùng flow với `--source-type tapo_rtsp`; preset này dùng port 554,
path `stream1` và luôn yêu cầu Camera Account.

Nếu Thing đã được tạo thủ công, bỏ `--provision-mainflux`, thêm `--mainflux-thing-id` nếu có;
tool sẽ prompt ẩn Thing key. Không dùng chung key với device khác.

### ADB, window và webcam qua cùng CLI

Interactive flow vẫn là lựa chọn ngắn nhất. Các flag source tương ứng:

```powershell
# Android ADB; --adb-serial có thể bỏ khi chỉ một device đã authorize.
.\.venv\Scripts\python.exe -m tools.add_device `
  --device-id android-display-01 `
  --display-name "Android display" `
  --source-type adb `
  --adb-serial SERIAL_FROM_ADB_DEVICES `
  --adb-require-landscape `
  --mainflux-group-id GROUP_UUID `
  --mainflux-email operator@example.com `
  --provision-mainflux `
  --dry-run

# Cửa sổ Windows/scrcpy fallback.
.\.venv\Scripts\python.exe -m tools.add_device `
  --device-id window-01 `
  --display-name "Named window" `
  --source-type window `
  --window-title CAMERA-LIVE `
  --mainflux-group-id GROUP_UUID `
  --mainflux-email operator@example.com `
  --provision-mainflux `
  --dry-run

# Webcam/virtual webcam.
.\.venv\Scripts\python.exe -m tools.add_device `
  --device-id webcam-01 `
  --display-name "USB webcam" `
  --source-type webcam `
  --webcam-index 0 `
  --webcam-backend dshow `
  --mainflux-group-id GROUP_UUID `
  --mainflux-email operator@example.com `
  --provision-mainflux `
  --dry-run
```

Mỗi lệnh trên là rehearsal. Kết nối/authorize Android, mở đúng source window hoặc cắm webcam,
sau đó bỏ `--dry-run` để source probe và commit thật. Có thể stage một entry chưa sẵn sàng bằng
`--disabled`; device disabled không mở source.

ROI là optional. Sau khi device đã enabled và source sẵn sàng:

```powershell
.\.venv\Scripts\python.exe -m tools.calibrate_roi --device lobby-01
```

Dán `monitor_roi` được in ra vào entry tương ứng. ROI phải khoanh phần hiển thị của màn hình vật
lý, không mặc định dùng gần toàn frame chỉ để ép `SCREEN YES`.

### Tính transaction và rerun

- Rerun cùng spec là no-op; cùng `device_id` nhưng spec khác bị từ chối, không update ngầm.
- Manifest và `.env` được stage/replace với lock; lỗi file thứ hai sẽ rollback file thứ nhất.
- Source check chạy trước provisioning; lỗi source không để lại local/remote resource.
- Mainflux và filesystem không thể là một transaction chung. Nếu provisioning thành công nhưng
  local commit sau đó lỗi, remote resource có thể đã tồn tại; sửa lỗi local rồi rerun, provisioner
  sẽ reconcile idempotently thay vì tạo trùng.
- Không có secret trong YAML, dry-run output, exception đã redact hay object repr.

## Agent pentest nội bộ

`agent_type: pentest` gắn một worker pentest vào một source camera đã đăng ký. Với Tapo C200,
Windows host giữ vai trò thực thi kiểm tra còn camera cung cấp RTSP/source health và device identity;
không chạy code tùy ý bên trong firmware camera. Worker chỉ thực hiện:

- kết nối TCP tới tối đa 64 port được khai báo rõ trong manifest;
- kiểm tra một endpoint HTTP(S) bằng phương thức `HEAD`;
- tùy chọn kiểm tra HTTP `OPTIONS` để phát hiện `TRACE` được quảng bá;
- tùy chọn đánh giá TLS: xác thực certificate, version TLS và số ngày certificate còn hiệu lực;
- kiểm tra thuộc tính bảo vệ cho cookie mà không lưu giá trị cookie;
- với RTSP, gửi đúng một request `OPTIONS` read-only, không kèm credential, để kiểm tra service có yêu cầu authentication;
- kiểm tra TLS verification, trạng thái HTTP và một số security header phổ biến;
- phát hiện header lộ thông tin server mà không ghi lại giá trị header hay response body.

Worker không quét subnet/CIDR, không brute-force, không thử credential, không directory fuzzing,
không exploit và không gửi payload thay đổi trạng thái. Mỗi assessment chỉ được thực hiện với host,
port và endpoint đã được người vận hành khai báo, thuộc hệ thống đã được ủy quyền. Target public bị từ chối mặc định; chỉ bật
`allow_public_target: true` khi có phê duyệt phạm vi rõ ràng. Đây là baseline configuration review,
không phải full authenticated web application pentest.

Ví dụ Tapo C200-backed manifest an toàn:

```yaml
  - device_id: tapo-security-agent-01
    display_name: Tapo C200 security assessment agent
    enabled: false
    agent_type: pentest
    source_type: rtsp
    source:
      adapter: tapo
      host: 192.0.2.12
      port: 554
      path: stream1
      username_env: CAMERA_AGENT_TAPO_SECURITY_AGENT_01_RTSP_USER
      password_env: CAMERA_AGENT_TAPO_SECURITY_AGENT_01_RTSP_PASS
    pentest:
      target_from_source: true
      ports: [554]
      connect_timeout: 3
      request_timeout: 5
      verify_tls: true
      allow_public_target: false
      rtsp_probe: true
    mainflux:
      thing_name: camera-agent-tapo-security-agent-01
      thing_key_env: CAMERA_AGENT_TAPO_SECURITY_AGENT_01_MAINFLUX_THING_KEY
    process_interval: 300
    rules:
      template: default
      overrides: {}
```

Telemetry dùng các measurement `security_state`, `pentest_finding_count`,
`pentest_high_count`, `pentest_medium_count`, `pentest_low_count`,
`pentest_open_port_count`, `pentest_http_status`, `pentest_tls_valid`,
`pentest_rtsp_status`, `pentest_rtsp_auth_required`, `pentest_rtsp_assessed`,
`pentest_http_options_status`, `pentest_http_options_assessed`, `pentest_tls_assessed`,
`pentest_tls_days_remaining`,
`pentest_finding_event`, `pentest_finding_cleared` và `pentest_scan_error_event`.
Onboarding với `--provision-mainflux` tạo rule riêng cho finding và scan error; assignment vẫn
được kiểm tra exact, idempotent và fail closed khi có legacy/foreign rule.
Nếu Thing đã tồn tại và không dùng onboarding tích hợp, chạy
`tools.setup_mainflux_rules` với thêm `--rule-set pentest` để reconcile đúng hai rule bảo mật.

### Cách chạy vulnerability assessment

1. Xác nhận bằng văn bản phạm vi gồm hostname/IP, port và endpoint được phép đánh giá. Chỉ dùng LAN hoặc VPN; không mở RTSP ra Internet.

2. Kiểm tra manifest hiện tại:

   ```powershell
   .\.venv\Scripts\python.exe agent.py --check-config
   ```

3. Với Tapo C200 hiện tại, `rtsp_probe: true` sẽ đánh giá port 554 bằng TCP và một request RTSP `OPTIONS` không kèm password. Chạy agent:

   ```powershell
   .\.venv\Scripts\python.exe agent.py --no-preview
   ```

   Kết quả điển hình tốt là `rtsp_status=401` hoặc `403` và `rtsp_auth_required=True`. Nếu `rtsp_status=200` và `rtsp_auth_required=False`, agent tạo finding `rtsp_auth_not_required` để đội vận hành review.

4. Để đánh giá một HTTPS service nội bộ đã được ủy quyền, tạo một agent mới có source Tapo nhưng target là service đó. Ví dụ thay các placeholder bằng giá trị của hệ thống thuộc phạm vi:

   ```powershell
   .\.venv\Scripts\python.exe -m tools.add_device `
     --device-id internal-web-security-01 `
     --display-name "Internal HTTPS security assessment" `
     --agent-type pentest `
     --source-type tapo_rtsp `
     --host TAPO_IP `
     --pentest-target-host INTERNAL_SERVICE_IP `
     --pentest-ports 443 `
     --pentest-http-scheme https `
     --pentest-http-port 443 `
     --pentest-http-path /health `
     --pentest-http-options-probe `
     --pentest-tls-assessment `
     --disabled `
     --dry-run
   ```

   Dry-run có thể hỏi RTSP credential và Mainflux Thing key ở chế độ hidden; không ghi chúng ra màn hình hoặc vào file. Khi scope/config đúng, chạy lại cùng lệnh nhưng bỏ `--dry-run` để thêm entry ở trạng thái disabled. Review entry vừa tạo trong `config/devices.yaml`, đổi đúng trường `enabled: false` của `internal-web-security-01` thành `enabled: true`, rồi chạy lại:

   ```powershell
   .\.venv\Scripts\python.exe agent.py --check-config
   .\.venv\Scripts\python.exe agent.py --no-preview
   ```

5. Đọc log của mỗi chu kỳ. Ví dụ:

   ```text
   Pentest scan completed: status=FINDINGS open_ports=1 findings=2 finding_codes=http_trace_enabled,cookie_missing_httponly http_status=200 http_options=204 tls_days=87 ...
   ```

   `status=PASS` nghĩa là các kiểm tra đã bật không tạo finding. `status=FINDINGS` không đồng nghĩa hệ thống đã bị xâm nhập; nó là danh sách cấu hình cần human review. Mã finding an toàn xuất hiện trong log; Mainflux chỉ nhận số liệu, trạng thái và event flag — không nhận frame, response body, header/cookie value, certificate hay secret.

## Mainflux provisioning và rules

Onboarding tích hợp là đường khuyến nghị vì nó có nơi lưu Thing key vừa sinh một cách an toàn.
Mỗi camera device nhận:

- một Thing/key riêng trong đúng Group;
- shared profile `Camera Agent - SenML`;
- `Camera Agent - <device_id> - Feature violation`, Alarm level 4 (migrate in-place từ tên Facebook cũ);
- `Camera Agent - <device_id> - Camera offline`, Alarm level 3.

Pentest device dùng cùng SenML profile nhưng nhận hai rule riêng:
`Security finding` (level 4) và `Pentest scan error` (level 3).

Local camera agent giữ temporal confirmation/cooldown; pentest agent chỉ phát event khi finding
hoặc lỗi scan chuyển trạng thái. Server rules chỉ xử lý các event pulse tương ứng. Provisioner phân trang collection,
reconcile drift, kiểm tra exact assignment và chạy lại không tạo Thing/profile/rule trùng.

Mainflux Alarm là bản ghi/cảnh báo server. Nếu muốn C200 phát âm thanh vật lý, bật
`physical_alarm` riêng cho device Tapo RTSP. C200 firmware của device hiện tại không cung cấp
`setSirenStatus`/`play_alarm`, nên adapter dùng custom audio `testUsrDefAudio` qua optional package
không chính thức `pytapo`; không đặt password trong YAML và không log credential:

```powershell
.\.venv\Scripts\python.exe -m pip install pytapo
.\.venv\Scripts\python.exe -m tools.check_tapo_alarm --device-id yume-1
.\.venv\Scripts\python.exe -m tools.check_tapo_alarm --device-id yume-1 --duration 3 --yes
```

Lệnh đầu của `check_tapo_alarm` chỉ validate target. Lệnh có `--yes` sẽ làm camera phát audio thật.
`physical_alarm.audio_id` là ID custom audio đã có trên camera, mặc định `8196`. Nếu firmware hoặc
audio ID không hỗ trợ, agent vẫn giữ Mainflux Alarm và log lỗi physical alarm thay vì dừng runtime.

CLI độc lập chỉ reconcile Thing đã tồn tại; nó không tạo Thing mới vì không có safe sink để lưu
key vừa sinh:

```powershell
.\.venv\Scripts\python.exe -m tools.setup_mainflux_rules `
  --group-id GROUP_UUID `
  --device lobby-01=THING_UUID `
  --email operator@example.com `
  --dry-run
```

Có thể lặp `--device`; mỗi Thing ID chỉ được map một device. Nếu bỏ `=THING_UUID`, CLI chỉ tìm
deterministic managed Thing đã tồn tại. Nếu Thing chưa có, dùng `tools.add_device
--provision-mainflux` để tạo và lưu key mà không in ra terminal.

Provisioner fail closed khi Thing đang gắn legacy/foreign rule có thể nhận cùng event. Nó không
tự unassign/delete để tránh mất alarm hoặc ảnh hưởng device khác. Khi gặp lỗi overlap:

1. đọc tên rule/Thing được báo;
2. trong Mainflux UI, review alarm/history và assignment;
3. unassign riêng target Thing khỏi legacy shared rule; không xóa rule nếu Thing khác còn dùng;
4. rerun lệnh `--dry-run`, rồi bỏ `--dry-run` khi plan đúng.

Profile metadata/config không do Camera Agent quản lý được preserve khi reconcile.

### Health và publish test đúng device

Health check chỉ đọc:

```powershell
.\.venv\Scripts\python.exe -m tools.check_mainflux
```

Mọi publish test bắt buộc chọn device để không dùng nhầm Thing key:

```powershell
.\.venv\Scripts\python.exe -m tools.check_mainflux `
  --device-id lobby-01 `
  --publish-test
```

Hai lệnh sau cố ý tạo Alarm; chỉ chạy khi muốn test rule thật:

```powershell
.\.venv\Scripts\python.exe -m tools.check_mainflux `
  --device-id lobby-01 `
  --publish-rule-event

.\.venv\Scripts\python.exe -m tools.check_mainflux `
  --device-id lobby-01 `
  --publish-camera-offline-event
```

## Disable và remove an toàn

Mặc định chỉ disable entry local:

```powershell
.\.venv\Scripts\python.exe -m tools.remove_device `
  --device-id lobby-01 `
  --dry-run

.\.venv\Scripts\python.exe -m tools.remove_device `
  --device-id lobby-01
```

Xóa riêng entry YAML cần flag và xác nhận rõ:

```powershell
.\.venv\Scripts\python.exe -m tools.remove_device `
  --device-id lobby-01 `
  --remove-local `
  --dry-run

.\.venv\Scripts\python.exe -m tools.remove_device `
  --device-id lobby-01 `
  --remove-local `
  --yes
```

Cả hai mode không xóa `.env`, Mainflux Thing/rule/alarm, SQLite outbox, telemetry lịch sử,
model hay dataset. Project cố ý chưa có remote-delete workflow.

## Reset plan cho hai adapter hiện tại — chưa thực hiện

Đường an toàn nhất để thử onboarding từ đầu là dùng `device_id` và Thing mới, giữ hai entry cũ
disabled làm rollback reference.

1. Dừng agent và backup riêng manifest không chứa secret:

   ```powershell
   $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
   Copy-Item config\devices.yaml "config\devices.before-reset.$stamp.yaml"
   ```

2. Xem plan rồi disable hai entry hiện tại. Việc này không chạm `.env`/Mainflux:

   ```powershell
   .\.venv\Scripts\python.exe -m tools.remove_device --device-id office-01 --dry-run
   .\.venv\Scripts\python.exe -m tools.remove_device --device-id hvip01 --dry-run
   .\.venv\Scripts\python.exe -m tools.remove_device --device-id office-01
   .\.venv\Scripts\python.exe -m tools.remove_device --device-id hvip01
   .\.venv\Scripts\python.exe agent.py --check-config
   ```

3. Sau khi đã disabled, người vận hành mới ngắt camera RTSP hoặc Android USB. Zero-enabled
   manifest config-check được; không chạy `agent.py` production cho tới khi có device mới enabled.

4. Rehearsal onboarding với ID mới, ví dụ `rtsp-retest-01` và `android-retest-01`, dùng các lệnh
   ở phần Thêm thiết bị. Kết nối lại phần cứng trước khi bỏ `--dry-run`. Tạo Thing mới để kiểm tra
   trọn vẹn luồng và không trộn outbox/history cũ.

5. Chỉ khi bắt buộc tái sử dụng đúng `office-01`/`hvip01`, remove local entry sau dry-run:

   ```powershell
   .\.venv\Scripts\python.exe -m tools.remove_device --device-id office-01 --remove-local --dry-run
   .\.venv\Scripts\python.exe -m tools.remove_device --device-id office-01 --remove-local --yes
   ```

   Lặp cho ID còn lại. `.env` và remote history vẫn còn; không dùng lại old Thing key cho Thing
   mới. Outbox bind theo destination nên event của destination cũ không được phát sang key/Thing
   mới.

6. Nếu tái sử dụng old Thing, provisioner có thể dừng vì legacy shared-rule overlap. Review rồi
   unassign riêng Thing theo hướng dẫn ở trên; không xóa rule/alarm tự động.

7. Gate cuối cho từng device mới:

   ```powershell
   .\.venv\Scripts\python.exe agent.py --check-config
   .\.venv\Scripts\python.exe -m tools.check_mainflux --device-id NEW_DEVICE_ID --publish-test
   .\.venv\Scripts\python.exe agent.py --dry-run
   .\.venv\Scripts\python.exe agent.py
   ```

Reset plan này chỉ là hướng dẫn. Không bước xóa/ngắt/re-provision nào đã được thực hiện trong đợt
thay đổi hiện tại.

## Runtime, preview và telemetry

### Control trung tâm Mainflux và Person Guard

Mỗi camera có đúng một feature runtime: `facebook_monitor`, `person_guard` hoặc `none`.
`none` chỉ deactivate feature an toàn; không xóa model, outbox, lịch sử, Thing, rule hay Alarm.
`runtime_enabled: false` đưa device vào **standby**: reader, inference, preview và PTZ dừng, nhưng
worker MQTT/control vẫn sống để nhận lệnh bật lại. Standby không tạo `CAMERA_OFFLINE` giả.

`person_guard` dùng model COCO `yolo11n` local đã kiểm SHA-256, không train và không tự download
khi runtime. Nó chỉ chọn box `person` lớn nhất đạt confidence `0.45`, cho phép box người bị che một
phần, không nhận diện mặt/danh tính và không lưu hoặc gửi frame. Alert cần 2 frame liên tiếp và chỉ
re-arm sau 3 giây không thấy người. Alarm event luôn đi vào SQLite outbox trước HTTP Mainflux; còi
vật lý chỉ chạy nếu `physical_alarm` của chính camera đã opt-in.

Khi Auto ON trong Person Guard, đây là **tracking**, không phải patrol đi tìm người. Camera chỉ theo
tâm của box lớn nhất: dead-zone 15%, ưu tiên trục
lệch lớn hơn, segment tối đa 0.25 s và cách nhau tối thiểu 0.5 s. Không thấy người thì dừng motion;
không dùng patrol để tìm người. PTZ arbiter tuần tự hóa manual move, Facebook patrol và Person
Guard; Stop, standby hoặc switch feature sẽ hủy motion cũ trước.

Manifest khai báo các ID Mainflux không bí mật và catalog feature; key/model/token vẫn là reference
environment. Điền UUID thật sau provisioning, không dùng placeholder trong production:

```yaml
mainflux:
  thing_id: DEVICE_THING_UUID
  channel_id: TELEMETRY_CHANNEL_UUID
  control_channel_id: PRIVATE_CONTROL_CHANNEL_UUID
  thing_key_env: CAMERA_AGENT_LOBBY_01_MAINFLUX_THING_KEY
features:
  available: [facebook_monitor, person_guard]
  default: facebook_monitor
  person_guard:
    model_path_env: CAMERA_AGENT_LOBBY_01_PERSON_MODEL
    model_sha256_env: CAMERA_AGENT_LOBBY_01_PERSON_MODEL_SHA256
control: # chỉ cần ở hub khi preview của agent khác host
  preview_url_env: CAMERA_AGENT_LOBBY_01_PREVIEW_URL
  preview_token_env: CAMERA_AGENT_LOBBY_01_PREVIEW_TOKEN
```

Trong deployment có MQTT, MQTT dùng channel riêng cho từng device và topic chuẩn Mainflux
`channels/<control_channel_id>/messages/camera-agent/{desired,status,command}/<device_id>`.
Controller Thing và agent Thing là hai member duy nhất của channel. MQTT username là Thing ID,
password là Thing key; bắt buộc TLS qua VPN tin cậy. Desired state được hub ghi SQLite kèm
`generation` trước khi publish; agent bỏ generation cũ/trùng, ACK state thực sự áp dụng và hub
resend state mới nhất sau reconnect. Mainflux không nhận pixel; hub chỉ proxy JPEG opt-in qua
HTTPS/VPN với token nội bộ riêng, token browser không đi qua URL, log, Mainflux hay localStorage.

Browser UI bắt nhập `CONTROL_SERVER_TOKEN` trước khi tải inventory. Token nằm trong `sessionStorage`
đến khi đóng tab và dùng chung cho toàn bộ device. `GET /v1/devices` trả display name, online,
standby, desired/applied feature và ACK; `POST /v1/devices/<id>/desired-state` chỉ nhận
`{"runtime_enabled": bool, "feature": "facebook_monitor"|"person_guard"|"none"}`.

### Rollout trung tâm: PowerShell theo từng bước

1. Cài dependency, stage artifact `yolo11n.pt` từ nguồn đã được đội vận hành phê duyệt và lấy hash.
   Lệnh này chỉ đọc artifact local; không download hay thay đổi model.

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   $hash = (Get-FileHash .\models\yolo11n.pt -Algorithm SHA256).Hash.ToLower()
   .\.venv\Scripts\python.exe -m tools.verify_person_model `
     --model .\models\yolo11n.pt --sha256 $hash --load-check
   ```

2. Trong `.env` trên **agent host**, đặt đường dẫn và `$hash` vào hai biến Person Guard của device;
   đặt Thing key riêng của device. Trong `config/devices.yaml`, điền `thing_id`, `channel_id` và
   `control_channel_id` thật cho device. `channel_id` làm publisher dùng chính xác route
   `/http/channels/<channel_id>/messages`, thay cho `/http/messages` cũ vốn trả 404. Không in hoặc
   commit `.env`.

3. Tạo trước controller Thing trong Mainflux UI/onboarding, lưu ID không bí mật và key trong `.env`
   của **central hub** là `MAINFLUX_CONTROL_THING_ID` và `MAINFLUX_CONTROL_THING_KEY`. Chỉ controller
   cần hai biến này. Cấu hình TLS/VPN:

   ```powershell
   $env:MAINFLUX_MQTT_HOST = "mqtt.vpn.example"
   $env:MAINFLUX_MQTT_PORT = "8883"
   $env:MAINFLUX_MQTT_TLS = "true"
   $env:MAINFLUX_MQTT_CA_FILE = "C:\vpn\ca.pem"
   $env:MAINFLUX_CONTROL_SUBTOPIC_PREFIX = "camera-agent"
   ```

4. Xem plan trước, sau đó provision các control channel riêng. Tool không tạo controller Thing vì
   không có secret sink an toàn; nó không in Thing key, không unassign/delete resource và fail-closed
   khi thấy Thing foreign. Chép `control_channel` mà tool in ra vào `mainflux.control_channel_id` nếu
   manifest chưa có ID đó, rồi chạy lại `--dry-run` để xác minh idempotent.

   ```powershell
   .\.venv\Scripts\python.exe -m tools.setup_mainflux_control `
     --group-id GROUP_UUID --device-id yume-1 --email operator@example.com --dry-run
   .\.venv\Scripts\python.exe -m tools.setup_mainflux_control `
     --group-id GROUP_UUID --device-id yume-1 --email operator@example.com
   ```

5. Preflight bắt buộc trước production. Publish-test HTTP không tạo Alarm; chỉ thêm
   `--publish-rule-event`/`--publish-camera-offline-event` khi operator thật sự muốn test Alarm.
   MQTT preflight chỉ connect/subscribe, không publish desired/PTZ/alarm.

   ```powershell
   .\.venv\Scripts\python.exe agent.py --check-config
   .\.venv\Scripts\python.exe -m tools.check_mainflux --device-id yume-1 --publish-test
   .\.venv\Scripts\python.exe -m tools.check_mainflux_control --device-id yume-1 --role agent
   .\.venv\Scripts\python.exe -m tools.check_mainflux_control --device-id yume-1 --role controller
   .\.venv\Scripts\python.exe -m tools.check_onvif_ptz --device-id yume-1
   ```

6. Chạy `agent.py` trên từng agent host. Trên hub, chạy control server với controller Thing và một
   token browser mạnh. Nếu preview ở host khác, chạy thêm control server loopback trên agent host,
   đặt nó sau reverse proxy HTTPS/VPN, điền `preview_url_env`/`preview_token_env` ở hub. Browser chỉ
   gọi hub; token nội bộ preview không lộ ra browser.

   ```powershell
   # Agent host
   .\.venv\Scripts\python.exe agent.py --web-preview --no-preview

   # Central hub
   $env:CONTROL_SERVER_TOKEN = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }))
   .\.venv\Scripts\python.exe -m tools.control_server --devices .\config\devices.yaml
   ```

   Mở `http://127.0.0.1:8766/ui`, nhập token một lần, chọn device theo tên, chọn Person Guard rồi
   bấm áp dụng. UI hiển thị generation/ACK; chỉ bật Auto sau khi ONVIF preflight pass. Dùng `none` để
   erase/deactivate feature hoặc nút Standby để tắt runtime camera an toàn.

### Handoff nhanh cho runtime hiện tại

- Control server không tự chạy camera: luôn cần một `agent.py` và chỉ một control server.
- `agent.py --web-preview --no-preview` bật preview local; chỉ giữ JPEG mới nhất trong
  `runtime/previews`, không ghi recording và không gửi frame lên Mainflux.
- Agent heartbeat khiến command trả `503 agent offline` nếu agent chưa chạy; command pending quá
  120 giây sẽ hết hạn. Nếu preview browser khóa file trên Windows, writer retry/bỏ frame thay vì
  làm chết agent.
- Mainflux hiện vẫn trả `404` tại `/http/messages` trên deployment đang dùng. Đây là route mismatch;
  cần xác nhận base URL, adapter path và channel ID trước khi sửa publisher, không thay secret tùy ý.

## Điều khiển PTZ và auto patrol thử nghiệm

Runtime có một control plane local chạy trên chính PC server. Nó không nhận lệnh từ ứng dụng
điện thoại: server ghi command typed vào SQLite và mỗi agent chỉ claim command có đúng
`device_id`. Command duy nhất hiện có là `set_auto`, `move` (`left`, `right`, `up`, `down`) và
`stop`; không có remote shell, URL tùy ý hay command PowerShell.

Với camera Tapo wired, dùng ONVIF Profile S qua Camera Account hiện có. TP-Link công bố ONVIF
service port `2020`, RTSP port `554` và PTZ là capability của ONVIF trên LAN tin cậy. Không
port-forward hai port này ra Internet; nếu server khác mạng hãy dùng VPN. ONVIF/PTZ chỉ được bật
sau preflight thành công trên cùng LAN.

1. Cài dependency ONVIF vào virtual environment:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. Trong Tapo app, tạo/kiểm tra **Camera Account** và bật Third-Party Compatibility/ONVIF. Đây là
thao tác trong app duy nhất bắt buộc; username/password Camera Account đã dùng cho RTSP được tái
sử dụng, không thêm chúng vào YAML.

3. Sửa entry camera RTSP structured trong `config/devices.yaml`: đặt `ptz.enabled: true` nhưng
giữ `auto_patrol.enabled: false`. Sau đó probe không di chuyển camera:

   ```powershell
   .\.venv\Scripts\python.exe -m tools.check_onvif_ptz --device-id yume-1
   ```

4. Nếu probe thành công, chỉ thử đúng một movement ngắn sau khi đã nhìn quanh camera:

   ```powershell
   .\.venv\Scripts\python.exe -m tools.check_onvif_ptz --device-id yume-1 --move right --duration 0.5 --yes
   ```

5. Chạy control server chỉ trên PC này. Token không ghi vào manifest hay output:

   ```powershell
   $env:CONTROL_SERVER_TOKEN = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }))
   .\.venv\Scripts\python.exe -m tools.control_server
   ```

   Mở `http://127.0.0.1:8766/ui` trên **PC server**. Nhập token và agent ID để bấm Auto ON/OFF,
   Up/Down/Left/Right hoặc Stop. Server mặc định từ chối bind ra LAN/Internet.

   Muốn xem live frame trong dashboard, chạy agent với cờ opt-in sau (không ghi frame lên Mainflux,
   chỉ giữ một JPEG mới nhất trong `runtime/previews` và xóa khi agent dừng):

   ```powershell
   .\.venv\Scripts\python.exe agent.py --web-preview --no-preview
   ```

   Control UI tự làm mới preview khoảng hai lần mỗi giây. Nếu agent không chạy với `--web-preview`,
   dashboard vẫn điều khiển PTZ được nhưng vùng camera sẽ báo `Preview is waiting`.

   Control server chỉ ghi lệnh vào SQLite; nó không tự chạy camera. Phải có một tiến trình
   `agent.py` đang chạy và dùng chung thư mục project thì lệnh mới chuyển từ trạng thái `pending`
   sang `executed`.

6. Khi Auto ON với `facebook_monitor`, agent di chuyển theo các bước ONVIF hữu hạn đến khi phát hiện một màn hình. Nó
dừng và quan sát 5 giây. Nếu không có Facebook rule event, nó chuyển góc nhìn. Nếu Facebook còn
active, rule local phải có cooldown đúng 3 giây; agent phát tối đa ba event/alarm rồi chuyển góc
nhìn bất kể kết quả tiếp theo. Auto không đảm bảo đây là một màn hình vật lý khác vì camera chỉ
có tín hiệu hình ảnh, không có định vị không gian.

Với `person_guard`, Auto chỉ theo dõi người đã được phát hiện; nó không tự quay tuần tra để tìm người.

Patrol hiện dùng sweep theo hàng `RIGHT ×3 -> DOWN -> LEFT ×3 -> DOWN`, với manifest `yume-1`
đặt `velocity: 0.55`, `move_duration_seconds: 2.0` và `search_move_interval_seconds: 2.2`.

Từ bản runtime hiện tại, lệnh `move` và từng đoạn auto được chạy trong worker PTZ riêng:
vòng đọc RTSP/inference không phải chờ hết `move_duration_seconds`. Nút `Stop` sẽ ngắt đoạn
đang chạy; `velocity` và `move_duration_seconds` nên được hiệu chỉnh theo từng model camera.

`POST /v1/mainflux/commands` của control server nhận JSON `{ "device_id", "action", "payload" }`
với header `X-Control-Server-Token`. Đây là endpoint bridge để Mainflux rule/webhook gọi vào
server sau này. Provisioning hiện tại vẫn chỉ tạo rule Alarm; chưa tự mở webhook remote vì cần
operator cung cấp URL VPN/TLS và xác nhận Mainflux deployment có webhook action. Không expose
endpoint này trực tiếp lên public Internet.

Chạy config check, dry-run hoặc production:

```powershell
.\.venv\Scripts\python.exe agent.py --check-config
.\.venv\Scripts\python.exe agent.py --dry-run
.\.venv\Scripts\python.exe agent.py
```

Chọn manifest khác:

```powershell
.\.venv\Scripts\python.exe agent.py `
  --devices config\devices.example.yaml `
  --dry-run `
  --check-config
```

Fleet chỉ cho `compact` hoặc `off`; cả hai không chứa camera pixels. `--preview-mode full` bị từ
chối trong fleet để giữ hàng rào mirror reflection. Legacy single-source compatibility vẫn có
full preview: frame được fit toàn bộ đúng tỉ lệ, không crop/stretch.

Payload Mainflux giữ SenML `n/v/u` và `Content-Type: application/senml+json`. Measurement chính:

`agent_state`, `camera_online`, `computer_detected`, `facebook_active`,
`facebook_confidence`, `facebook_raw_confidence`, `classifier_margin`,
`facebook_rule_status`, `rule_violation_event`, `rule_event_cleared`,
`rule_violation_count`, `camera_offline_event`, `camera_recovered_event`,
`camera_offline_seconds`, `self_preview_suppressed`, `frame_age_seconds`, `inference_ms`,
`rtsp_reconnect_count`.

Edge event phải enqueue SQLite thành công trước HTTP publish. Nếu SQLite lỗi, event không được
gửi bypass. Database chỉ chứa telemetry; không chứa Thing key, credential hay frame. Khi một
`device_id` được bind sang Thing/key khác, row của destination cũ được giữ/quarantine thay vì gửi
sang destination mới.

## Model, dữ liệu và quyền riêng tư

- Cấu hình `PROCESS_INTERVAL=1.0`, `DETECTOR_IMAGE_SIZE=416`, `TORCH_THREADS=2` có thể điều chỉnh
  linh hoạt theo năng lực phần cứng máy chạy; bắt đầu với tối đa hai nguồn rồi theo dõi CPU/RAM/`inference_ms`.
- Static ROI giúp giảm YOLO cost nhưng phải được calibrate từ full frame thật.
- Production classifier phải có ba lớp. Historical validation của một device/session không chứng
  minh khả năng tổng quát cho camera mới.
- Thu train và validation tách theo `device_id` + capture session; không random-split burst frame
  gần giống nhau. Giữ production model, xuất candidate riêng và chỉ thay sau independent gates.
- `SAVE_EVIDENCE=false` là mặc định. Mainflux không nhận ảnh/video.
- Giám sát cần thông báo, mục đích rõ, retention tối thiểu, phân quyền và human review.
- `extras/windows_enforcement` tách khỏi monitoring core, cần Administrator và không bao giờ được
  agent tự bật.

## Cấu trúc project

```text
agent.py                         runtime CLI, mặc định manifest v2
camera_agent/config.py          Settings, URL redaction, destination fingerprint
camera_agent/fleet_config.py    schema v2 runtime parser
camera_agent/device_registry.py transactional add/disable/remove local registry
camera_agent/camera.py          latest-frame RTSP/ADB/window/webcam readers
camera_agent/fleet.py           per-device workers, shared models/lock, pixel-free dashboard
camera_agent/ptz.py             ONVIF PTZ worker, bounded move/stop
camera_agent/auto_patrol.py      local patrol state machine and cooldown boundaries
camera_agent/control.py          typed SQLite command queue + stale-command expiry
camera_agent/local_preview.py    opt-in latest JPEG for authenticated loopback UI
camera_agent/presence.py         agent heartbeat used to reject commands while offline
camera_agent/provisioning.py    Mainflux Thing/profile/per-device-rule reconciliation
camera_agent/mainflux.py        SenML publisher và retry
camera_agent/outbox.py          SQLite edge-event queue theo device + destination
camera_agent/physical_alarm.py  optional Tapo custom-audio alarm actuator
config/devices.yaml             hai adapter thử nghiệm đã migrate, không chứa secret
config/devices.example.yaml     catalog source generic v2
mainflux/*.template.json        shared profile + parameterized per-device rules
tools/add_device.py             onboarding và optional provisioning
tools/check_tapo_alarm.py       validate/trigger thử còi vật lý Tapo, cần --yes để kêu
tools/check_device.py           one-frame preflight dùng nội bộ bởi onboarding
tools/remove_device.py          disable/remove-local an toàn
tools/setup_mainflux_rules.py   reconcile Thing đã tồn tại
tools/control_server.py          loopback dashboard/API; does not run the agent
tools/check_onvif_ptz.py         ONVIF probe; physical move requires --yes
tests/                           hermetic unit tests, không cần camera/Mainflux thật
models/ và data/                 tài sản production/training, không bị thay trong migration
```

## Regression gate

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q camera_agent tools tests agent.py
.\.venv\Scripts\python.exe agent.py --check-config
```

Gate trước rollout Person Guard: **177/177 test PASS**, `compileall` PASS và `agent.py --check-config`
PASS. Kiểm tra Mainflux publish thực tế vẫn phụ thuộc route của deployment đang dùng.
1/1 entry hiện hữu enabled. Các gate này không mở camera và không mutate Mainflux.

## Monitor Windows và C200 custom audio

Project có executable status-only tại `dist/CameraAgentMonitor.exe`. Monitor tự tìm project root,
chạy agent bằng `.venv` và truyền manifest bằng đường dẫn tuyệt đối, nên có thể mở bằng
PowerShell hoặc double-click:

```powershell
Start-Process .\dist\CameraAgentMonitor.exe
```

Monitor chỉ hiển thị `State`, `Score`, `Rule` và trạng thái Mainflux; không hiển thị pixel camera.
Khi state là `FACEBOOK_DETECTED` hoặc Person Guard xác nhận có người, local rule gửi event,
Mainflux có thể tạo Alarm và physical alarm gọi custom audio trên C200. Rule Facebook và physical
alarm của `yume-1` hiện cùng dùng cooldown 3 giây; `audio_id: 8196` phải tồn tại trong Tapo app.

Nếu cần rebuild executable sau khi sửa `tools/monitor.py`:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm CameraAgentMonitor.spec
```
