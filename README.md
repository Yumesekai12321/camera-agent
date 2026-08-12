# Camera Agent multi-device cho Windows và Mainflux

Camera Agent nhận nhiều loại nguồn hình ảnh local, phát hiện màn hình máy tính, phân loại
`facebook_active` / `facebook_mention` / `other`, xác nhận kết quả theo thời gian rồi gửi
telemetry SenML lên Mainflux. Product không phụ thuộc một hãng camera hay một `device_id` cụ thể.

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
manifest, agent sẽ cố gọi local Tapo API khi rule Facebook tạo edge event; một số firmware C200 có
thể từ chối manual siren control.

## Trạng thái xác minh ngày 2026-08-10

- Manifest canonical đã là schema v2 generic; v1 bị từ chối rõ ràng.
- Runtime không còn nhánh theo `office-01`, `hvip01`, C200 hay ONE Home trong `agent.py` và
  `camera_agent/`.
- Onboarding, registry transaction, source preflight, provisioning idempotent, rule isolation,
  inference serialization và durable outbox đều có unit test không dùng secret/camera thật.
- `unittest` PASS 120/120; `compileall` và `agent.py --check-config` đều PASS.
- `config/devices.yaml` hiện có một device enabled: `yume-1`.
- Không chạy source probe thật, không đăng nhập/provision Mainflux thật và không tạo/xóa remote
  resource trong đợt thay đổi này. Các bước cần phần cứng/account nằm ở phần vận hành bên dưới.
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
để phù hợp i5-8250U/8 GB.

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

## Mainflux provisioning và rules

Onboarding tích hợp là đường khuyến nghị vì nó có nơi lưu Thing key vừa sinh một cách an toàn.
Mỗi device nhận:

- một Thing/key riêng trong đúng Group;
- shared profile `Camera Agent - SenML`;
- `Camera Agent - <device_id> - Facebook violation`, Alarm level 4;
- `Camera Agent - <device_id> - Camera offline`, Alarm level 3.

Local agent giữ temporal confirmation/cooldown; server rules chỉ xử lý
`rule_violation_event == 1` và `camera_offline_event == 1`. Provisioner phân trang collection,
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

- Default `PROCESS_INTERVAL=1.0`, `DETECTOR_IMAGE_SIZE=416`, `TORCH_THREADS=2` phù hợp máy mục
  tiêu; bắt đầu với tối đa hai nguồn rồi theo dõi CPU/RAM/`inference_ms`.
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
tests/                           hermetic unit tests, không cần camera/Mainflux thật
models/ và data/                 tài sản production/training, không bị thay trong migration
```

## Regression gate

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q camera_agent tools tests agent.py
.\.venv\Scripts\python.exe agent.py --check-config
```

Gate cuối của đợt thay đổi này: **120/120 test PASS**, `compileall` PASS và config-check PASS với
1/1 entry hiện hữu enabled. Các gate này không mở camera và không mutate Mainflux.

## Monitor Windows và C200 custom audio

Project có executable status-only tại `dist/CameraAgentMonitor.exe`. Monitor tự tìm project root,
chạy agent bằng `.venv` và truyền manifest bằng đường dẫn tuyệt đối, nên có thể mở bằng
PowerShell hoặc double-click:

```powershell
Start-Process .\dist\CameraAgentMonitor.exe
```

Monitor chỉ hiển thị `State`, `Score`, `Rule` và trạng thái Mainflux; không hiển thị pixel camera.
Khi state là `FACEBOOK_DETECTED`, local rule gửi `rule_violation_event`, Mainflux tạo Alarm và
physical alarm gọi custom audio trên C200. Rule Facebook và physical alarm của `yume-1` hiện cùng
dùng cooldown 3 giây; `audio_id: 8196` phải tồn tại trong Tapo app.

Nếu cần rebuild executable sau khi sửa `tools/monitor.py`:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm CameraAgentMonitor.spec
```
