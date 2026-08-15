# AGENTS.md

## Đọc nhanh cho session mới

Đọc [docs/SESSION_HANDOFF.md](docs/SESSION_HANDOFF.md) trước để biết trạng thái vận hành,
lệnh khởi động và các lỗi đã xác minh. File này vẫn là quy tắc canonical; đọc toàn bộ
`README.md` trước khi sửa runtime, manifest, provisioning hoặc hướng dẫn triển khai.

## Mục tiêu và phạm vi

Repository này là Camera Agent multi-device chạy trên Windows. Runtime nhận nguồn `rtsp`,
`adb`, `window` hoặc `webcam`, phát hiện màn hình, phân loại hình ảnh Facebook cho một hoặc
nhiều thiết bị và chỉ gửi telemetry lên Mainflux. Tapo RTSP và Android/ONE Home qua ADB hoặc
window/scrcpy là adapter ví dụ, không phải giả định của product hay nhánh riêng theo
`device_id`.

Đọc toàn bộ `README.md` trước khi sửa runtime, manifest, provisioning hoặc hướng dẫn triển
khai. Không đọc/in `.env` thật khi không cần thiết.

## Bất biến phải giữ

- Không ghi cứng hoặc log Camera Account, RTSP URL có credentials, Mainflux Thing key hay mật
  khẩu đăng nhập. Secret chỉ nằm trong environment/`.env`; YAML chỉ chứa tên biến `*_env`.
- Không bật `SAVE_EVIDENCE` mặc định và không upload frame/snapshot lên Mainflux.
- Giữ mã state: 0 `NO_COMPUTER`, 1 `COMPUTER_NO_FACEBOOK`, 2 `FACEBOOK_DETECTED`,
  3 `CAMERA_OFFLINE`.
- Camera offline không được coi là no-computer và không được tự động mở block trong controller.
- Mọi reader RTSP/ADB/webcam/window phải chỉ giữ frame mới nhất và tự reconnect; không thêm
  queue frame không giới hạn. Không reverse-engineer hay hard-code giao thức/cổng P2P không
  được hãng công bố.
- `agent_type: pentest` chỉ được đánh giá host, port và HTTP path đã khai báo trong manifest,
  thuộc phạm vi được chủ hệ thống ủy quyền. Chỉ thực hiện probe read-only: TCP connect, HTTP
  `HEAD`/`OPTIONS`, TLS/certificate và RTSP `OPTIONS` không kèm credential. Không brute-force,
  fuzz, scan CIDR, bypass authentication, exploit, duy trì truy cập hoặc gửi request thay đổi trạng thái.
- Log pentest chỉ chứa status, count, thời lượng và mã finding an toàn; Mainflux chỉ nhận state,
  count và event flag. Không lưu hoặc upload response body, header/cookie value, certificate,
  credential hay frame.
- Full preview phải fit toàn bộ frame đúng tỉ lệ, không crop/stretch. Compact preview và fleet
  dashboard không được chứa pixel camera vì đây là hàng rào chống mirror reflection.
- Mặc định phải phù hợp i5-8250U/8 GB: inference khoảng 1 Hz và 2 torch threads. YOLO và
  classifier nặng dùng chung trong fleet và tất cả inference phải qua cùng một lock tuần tự.
- Classifier production phải có đủ `facebook_active`, `facebook_mention`, `other`. Nếu thiếu
  `other`, chỉ cảnh báo degraded; không được tuyên bố accuracy production.
- Dataset raw/model là tài sản. Không xóa hoặc ghi đè khi chưa có output candidate và validation
  độc lập đã xác minh; cache/review/training plot có thể tái tạo.
- Chạy bình thường bắt buộc Mainflux; chỉ `--dry-run` được bỏ publishing. HTTP payload phải là
  SenML `n/v/u` với `Content-Type: application/senml+json`; giữ measurement hiện hành.
- Temporal confirmation/cooldown nằm trong local rule engine. Mainflux server rule chỉ nhận
  event pulse để tạo Alarm, tránh alarm lặp ở mỗi heartbeat.
- Mỗi thiết bị phải có reader/reconnect, decision history, local rule/cooldown, publisher,
  Thing key, destination và outbox partition riêng theo `device_id`. Không chia sẻ state giữa
  thiết bị và không dùng chung Thing key/Thing ID.
- Edge event phải ghi thành công vào SQLite outbox trước khi HTTP publish. Outbox không chứa
  secret hoặc frame và phải bind cả device lẫn destination để key/Thing đổi không phát event cũ
  sang destination mới.
- Manifest canonical là schema v2 trong `config/devices*.yaml`. Validate cả entry disabled,
  reject unknown field/duplicate ID/Thing/key reference, và cho phép zero enabled device để
  quản lý; runtime thật vẫn cần ít nhất một device enabled.
- Mainflux có một SenML profile dùng chung nhưng hai rule riêng cho từng device. Provisioning
  phải idempotent, xác minh assignment chính xác và fail closed khi Thing còn gắn legacy/foreign
  rule; không tự unassign hoặc xóa rule/alarm/Thing.
- Onboarding local phải transactional, chống trùng và hỗ trợ dry-run. Disable là mặc định khi
  gỡ; không tự xóa `.env`, outbox, lịch sử, model/dataset hay tài nguyên Mainflux.
- `extras/windows_enforcement` là optional, cần Administrator và không được tự bật/trộn vào core.

## Bản đồ code

- `agent.py`: CLI runtime; mặc định dùng `config/devices.yaml` nếu không override.
- `camera_agent/config.py`: dựng/redact URL, Settings compatibility và destination fingerprint.
- `camera_agent/fleet_config.py`: parser/validator manifest v2 và ánh xạ environment references.
- `camera_agent/device_registry.py`: transaction local cho add/disable/remove manifest + `.env`.
- `camera_agent/camera.py`: RTSP/ADB/webcam/window latest-frame readers và source factory.
- `camera_agent/pentest.py`: bounded vulnerability assessment cho TCP, HTTP(S), TLS và RTSP.
- `camera_agent/pentest_agent.py`: worker pentest theo device, source health và telemetry Mainflux.
- `camera_agent/vision.py`: YOLO detector/ROI và MobileNet classifier.
- `camera_agent/decision.py`: temporal voting/hysteresis và state codes.
- `camera_agent/rules.py` + `config/rules.toml`: pending/violation/cooldown và overrides theo
  device.
- `camera_agent/mainflux.py` + `camera_agent/outbox.py`: SenML publisher, retry và durable event
  outbox.
- `camera_agent/physical_alarm.py`: actuator tùy chọn để phát custom audio trên Tapo C200, không
  nằm trong luồng RTSP/Mainflux telemetry; không giả định `setSirenStatus` có trên mọi firmware.
- `camera_agent/provisioning.py`: reconcile Thing/profile/per-device rule qua Mainflux API.
- `camera_agent/application.py`: orchestration một device, preview và evidence opt-in.
- `camera_agent/fleet.py`: per-device agents, shared models/lock và pixel-free fleet dashboard.
- `camera_agent/ptz.py`: ONVIF Profile S PTZ, bounded worker move/stop; không dùng P2P.
- `camera_agent/auto_patrol.py`: state machine quan sát 5 giây, cooldown và giới hạn 3 event.
- `camera_agent/control.py`: typed SQLite command queue; command motion cũ hết hạn sau 120 giây.
- `camera_agent/local_preview.py`: latest JPEG opt-in cho loopback UI; retry Windows file lock.
- `camera_agent/presence.py`: heartbeat ngắn hạn để control UI nhận biết agent offline.
- `tools/add_device.py`: onboarding interactive/PowerShell, source preflight và optional
  provisioning.
- `tools/remove_device.py`: disable mặc định hoặc xóa riêng entry local khi xác nhận.
- `tools/setup_mainflux_rules.py`: provisioning/reconciliation Mainflux độc lập cho Thing đã có.
- `tools/check_mainflux.py`: health/probe theo `--device-id`, không dùng key của device khác.
- `tools/check_tapo.py` và `tools/check_hvip01.py`: preflight riêng cho hai adapter ví dụ.
- `tools/check_tapo_alarm.py`: validate/kích thử còi vật lý Tapo; cần `--yes` để phát âm thật.
- `tools/check_window.py`, `tools/check_webcam.py`, `tools/calibrate_roi.py`: tiện ích generic.
- `tools/control_server.py`: loopback UI/API, token `CONTROL_SERVER_TOKEN`, không tự chạy agent.
- `tools/check_onvif_ptz.py`: ONVIF probe read-only; movement chỉ khi truyền `--yes`.
- `mainflux/profile.template.json`, `mainflux/rules.template.json`: SenML profile và rule Alarm
  parameterized theo `device_id`.
- `extras/windows_enforcement/`: optional enforcement tách biệt.

Mọi module trong `tools/` phải chạy bằng `python -m tools.<module>`.

## Quy trình thay đổi

1. Với logic không cần camera/Mainflux thật, viết/chỉnh unit test để thấy fail trước rồi mới sửa.
2. Chạy đầy đủ:

   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -v
   .\.venv\Scripts\python.exe -m compileall -q camera_agent tools tests agent.py
   .\.venv\Scripts\python.exe agent.py --check-config
   ```

3. Nếu sửa RTSP/source factory, yêu cầu người vận hành chạy source check tương ứng trên cùng
   LAN/host; với camera generic ưu tiên `tools.add_device` preflight, Tapo dùng thêm
   `python -m tools.check_tapo`.
4. Nếu sửa pentest, viết test cho giới hạn target/port, không log secret/response data, idempotent
   event pulse và probe protocol mới. Chỉ đánh giá target private/LAN trừ khi manifest có
   `allow_public_target: true` cùng phê duyệt phạm vi rõ ràng.
5. Nếu sửa model/dataset, ghi số ảnh từng lớp, split theo device + capture session, confusion
   matrix và giới hạn domain. Không random-split burst frame gần giống nhau.
6. Nếu thay payload Mainflux, giữ tên measurement/SenML, đồng bộ README/tests và chỉ test publish
   theo đúng device: `python -m tools.check_mainflux --device-id <id> --publish-test`.
7. Nếu sửa rule, test edge/cooldown local, template parameterization, exact assignment,
   idempotency, overlap legacy/foreign và late-race verification.
8. Nếu sửa fleet, test duplicate device/key/Thing, isolation state/publisher/outbox, một model
   shared và một inference lock cho cả detector lẫn classifier.
9. Nếu sửa onboarding, test dry-run byte-for-byte, hidden secret, rerun/no-op, rollback khi commit
   file thứ hai thất bại, preflight failure, duplicate resolved Thing key và disable isolation.

## Bàn giao hiện tại

- `config/devices.yaml` đã migrate sang schema v2. Trạng thái enable là dữ liệu vận hành do người
  dùng quản lý; không tự ngắt, xóa hoặc re-provision Thing hiện tại. `config/devices.example.yaml`
  là catalog nguồn generic.
- Onboard thiết bị mới bằng `tools.add_device`; reset hai adapter cũ chỉ được làm theo reset plan
  trong README sau khi người dùng xác nhận và thao tác phần cứng/Mainflux cần thiết.
- Mainflux cũ có thể còn hai shared legacy rules. Provisioner mới phải dừng khi phát hiện overlap;
  người vận hành review rồi unassign Thing khỏi legacy rule thủ công trước khi reconcile rule
  riêng. Không tự xóa rule/alarm lịch sử.
- Chất lượng detection Facebook chưa được xem là hoàn thành. Bắt đầu read-only bằng full frame,
  ROI/YOLO crop, raw probability ba lớp và classifier margin riêng từng device. Không hạ threshold
  để che model thiếu signal.
- Model hiện hành có ba lớp nhưng domain validation cũ không đại diện mọi camera/source mới.
  Giữ production model và xuất candidate riêng; chỉ thay khi các tập độc lập theo device/session
  vượt gate đã ghi trong README.
- Nếu cần sửa collector/trainer, phải có phạm vi triển khai rõ từ người dùng. Nếu người dùng chỉ
  yêu cầu tài liệu/prompt bàn giao, không sửa Python, YAML runtime, model, dataset, `.env`,
  Mainflux hoặc tests.

## Cập nhật runtime hiện tại

- Central control dùng desired-state SQLite trên hub và MQTT Mainflux qua TLS/VPN. Topic phải theo
  `channels/<control_channel_id>/messages/camera-agent/...`; MQTT username là Thing ID, password là
  Thing key. Mỗi control channel chỉ có controller Thing và agent Thing. Không dùng MQTT để tải code,
  model, frame hay token browser; agent chỉ nhận schema desired-state/typed PTZ allow-list.
- `features` là catalog versioned trong manifest: tối đa một feature `facebook_monitor` hoặc
  `person_guard`; `none` chỉ deactivate. `runtime_enabled: false` là standby, phải dừng reader,
  inference, preview và PTZ mà không tạo state/event `CAMERA_OFFLINE`, nhưng vẫn nhận lệnh MQTT.
- Person Guard dùng YOLO COCO `yolo11n.pt` local đã verify SHA-256 trước startup feature. Không train,
  auto-download runtime, identity/face recognition, lưu frame hay gửi pixel. Confirm 2 frame,
  re-arm sau 3 giây absent; event phải SQLite outbox-first. PTZ tracking chỉ largest box, segment
  <=0.25s, interval >=0.5s và qua `PTZArbiter`; switch/standby/Stop hủy motion cũ.
- `mainflux.channel_id` cho HTTP telemetry route `/http/channels/<id>/messages`; `control_channel_id`
  là private control route. Route cũ `/http/messages` đã xác minh mismatch ở deployment cũ, không
  đưa production lên nếu chưa điền UUID và pass `tools.check_mainflux` + `tools.check_mainflux_control`.

- `yume-1` đang dùng RTSP `172.16.7.62:554/stream1` và ONVIF `2020`; probe ONVIF read-only đã PASS.
- PTZ manual/auto chạy worker riêng; auto quét `RIGHT ×3 -> DOWN -> LEFT ×3 -> DOWN`, manifest hiện
  dùng `velocity: 0.55`, `move_duration_seconds: 2.0`, interval `2.2`.
- Control server không phải agent. Chạy một `agent.py` và một `python -m tools.control_server`; agent
  heartbeat giúp UI trả `503 agent offline` thay vì queue vô hạn. Preview web phải opt-in bằng
  `agent.py --web-preview --no-preview` và chỉ giữ một JPEG local.
- Windows có thể khóa JPEG khi browser đọc; preview writer phải retry/bỏ frame, không được làm chết
  `CameraAgent` hoặc Fleet.
- Mainflux hiện vẫn nhận `404` tại `http://localhost/http/messages`; đây là route/deployment mismatch,
  không tự đổi secret. Cần xác nhận base URL, adapter path và channel ID trước khi sửa publisher.

- Tapo C200 của `yume-1` từ chối các method `setSirenStatus` và `play_alarm`, nhưng hỗ trợ
  `testUsrDefAudio`; physical alarm dùng custom audio ID trong `physical_alarm.audio_id` (hiện là
  `8196`). Không reverse-engineer thêm giao thức hoặc tự downgrade firmware.
- `physical_alarm` phải có cooldown không nhỏ hơn duration. Khi cần lặp âm thanh theo event, đồng bộ
  cooldown actuator với cooldown local rule; monitor chỉ hiển thị cooldown của rule.
- `tools/monitor.py` phải tìm được project root khi chạy từ source hoặc PyInstaller executable,
  truyền manifest tuyệt đối và dùng `.venv\Scripts\python.exe` nếu tồn tại. Monitor không được hiển
  thị pixel camera.
- Regression gate trước rollout Person Guard (2026-08-14): `175/175` unittest PASS và `compileall`
  PASS. `agent.py --check-config` phải chạy lại sau khi operator điền UUID/channel Mainflux thật;
  không coi path `/http/messages` legacy là production-ready.

## Tài liệu, bảo mật và vận hành

- Hướng dẫn bằng tiếng Việt; lệnh PowerShell phải copy/paste được từng bước.
- `.env` không commit; `.env.example` chỉ có placeholder. Không in secret trong dry-run, error,
  repr hoặc test fixture.
- Không mở RTSP 554 ra Internet; dùng LAN tin cậy hoặc VPN.
- Vulnerability assessment không phải quyền tự động “thâm nhập”. Mọi test active phải có owner,
  scope, cửa sổ vận hành và phương án rollback được phê duyệt; ưu tiên staging trước production.
- Giám sát nhân viên cần thông báo, mục đích rõ ràng, retention tối thiểu và human review.
- Không tuyên bố hệ thống “xác định truy cập Facebook” tuyệt đối; đây là phân loại hình ảnh có
  false positive/false negative.
