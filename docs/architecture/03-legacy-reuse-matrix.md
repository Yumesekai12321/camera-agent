# Ma trận tái sử dụng adapter legacy

Tài liệu này trả lời nhanh câu hỏi “được dùng lại phần nào” khi thêm device mới. Product không
được rẽ nhánh theo `device_id`; chọn adapter bằng `source_type` và manifest schema v2.

| Nhu cầu | Adapter/module dùng lại | Ghi chú |
|---|---|---|
| Camera IP | `source_type: rtsp`, `camera_agent/camera.py` | Chỉ giữ latest frame, tự reconnect |
| Tapo RTSP | RTSP structured source + `adapter: tapo` | Camera Account ở `.env`, không ghi URL credential vào YAML |
| ONVIF PTZ | `camera_agent/ptz.py`, port thường `2020` | Profile S; chỉ probe/move trong LAN được ủy quyền |
| Android display | `source_type: adb` | Serial và timeout qua environment/manifest reference |
| Windows display | `source_type: window` | scrcpy/window là source adapter, không phải product branch |
| Webcam | `source_type: webcam` | Backend/capture config nằm trong manifest |
| Alarm vật lý Tapo | `camera_agent/physical_alarm.py` | Optional actuator, tách khỏi RTSP/Mainflux |
| Mainflux telemetry | `mainflux.py` + `outbox.py` | SenML; outbox phân vùng theo device + destination |
| Control/PTZ UI | `tools/control_server.py` + `control.py` | Loopback-only; server không tự chạy agent |
| Central MQTT desired state | `mainflux_control.py` + `tools/control_server.py` | Per-device Mainflux channel, SQLite generation/ACK; không remote code |
| Person Guard | `vision.py` + `person_guard.py` | YOLO COCO local hash-verified, anonymous largest person box, outbox-first alert |
| PTZ arbitration | `ptz.py` `PTZArbiter` | Serialize manual, Facebook patrol, Person Guard; switch/Stop hủy motion cũ |

## Luồng quyết định

```text
manifest v2 -> source factory -> latest frame -> detector/classifier
             -> decision/rule local -> SenML/outbox -> Mainflux
control UI -> SQLite command queue -> agent worker -> ONVIF PTZ
central UI -> desired-state SQLite -> Mainflux MQTT private channel -> agent ACK
```

Không reverse-engineer P2P, không chia sẻ Thing key/Thing ID/state giữa device. Khi một adapter
legacy cần nâng cấp, viết test bounded trước, giữ `--dry-run`, rồi chạy regression gate trong
[SESSION_HANDOFF.md](../SESSION_HANDOFF.md).
