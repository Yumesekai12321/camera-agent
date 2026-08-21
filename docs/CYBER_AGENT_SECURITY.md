# Cyber Agent — chính sách an toàn

## Phạm vi bắt buộc

Chỉ đánh giá thiết bị do operator sở hữu hoặc có ủy quyền rõ ràng. Scope là allowlist cấu hình; subnet không được cấu hình không được suy ra hay tự thêm. Public IP bị từ chối mặc định. Mọi target sai/malformed/denied được log với `DENIED_OUTSIDE_SCOPE` và không phát packet.

Mỗi thao tác mạng phải có:

- timeout hữu hạn;
- concurrency và rate limit nhỏ;
- log structured chỉ chứa event/status/count/thời lượng/mã finding an toàn;
- scope check ngay trước connect/request.

## Safe mode và dry-run

`mode: safe` cho phép các probe read-only bounded trong allowlist. `mode: dry-run` không gửi network packet, dùng mock asset/fingerprint và vẫn chạy state machine. CLI `run --dry-run` ép dry-run cho rehearsal.

`CYBER_AGENT_ENABLED` không bật mặc định. Khi false, runtime camera hiện tại giữ nguyên hành vi. Khi true, camera event chỉ kích hoạt pipeline theo cooldown; không lưu frame và không đưa pixel lên Mainflux.

## Các hành động bị cấm vĩnh viễn

- exploit thật, Metasploit exploit module, RCE, SQL injection, command injection;
- reverse shell, persistence, lateral movement hoặc tải payload;
- brute-force, password spraying, credential guessing hoặc bypass authentication;
- fuzzing, directory brute-force, UDP flood, SYN stealth scan;
- request làm thay đổi trạng thái, upload file, `PLAY/SETUP` RTSP, hoặc destructive action.

RTSP chỉ dùng TCP connect và `OPTIONS` không kèm credential. HTTP baseline dùng `HEAD`/`OPTIONS` và content probe giới hạn để phân biệt directory listing; response body/header value không được lưu. TLS chỉ lưu version, verified/expired và ngày còn lại. Basic auth/RTSP auth chỉ được quan sát qua status/challenge.

## Vulnerability matching

NVD/CISA KEV là nguồn dữ liệu đối chiếu, không phải giấy phép khai thác. Không hard-code/fabricate CVE. Cache local có TTL; remote source mặc định tắt. Finding version match phải có confidence và luôn là assessment cần human review, không phải bằng chứng exploitability.

## Correlation và risk

CV nhìn thấy một vật thể không chứng minh IP nào tương ứng. Correlator chỉ tăng confidence khi có vendor/hostname/MAC/service/timing evidence; dưới ngưỡng là `UNCONFIRMED`. Risk score 0–10 là ưu tiên review, không phải kết luận camera hoặc hệ thống “đã bị xâm nhập”.

## Secrets, storage và telemetry

Token/Thing key/API key chỉ nằm trong environment. SQLite cyber tables không lưu secret, cookie, certificate, body hoặc frame. Mainflux payload chỉ là SenML metadata, counts, service/CVE/risk và event flag; outbox bind device + destination để key/Thing đổi không phát event cũ sang đích mới.

Log an toàn gồm `discovery_started`, `host_discovered`, `DENIED_OUTSIDE_SCOPE`, `service_found`, `cve_matched`, `validation_passed`, `risk_calculated`/state transition, `mainflux_publish_success` và `mainflux_publish_failed`. Không log request body, response body, header value, credential hay token.

## Vận hành có trách nhiệm

Trước active validation cần owner, target list, cửa sổ vận hành, giới hạn rate và phương án dừng. Ưu tiên staging/lab. Tắt `CYBER_AGENT_ENABLED` hoặc dùng `mode: dry-run` khi rehearsal. Không coi output là chứng minh tuyệt đối; mọi finding cần human review và xử lý theo quy trình change management.
