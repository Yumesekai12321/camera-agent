# Windows Facebook enforcement (tùy chọn)

Thành phần này sửa file `hosts` và không phải phần bắt buộc của Camera Agent. Chỉ dùng sau khi
quy trình giám sát/block đã được phê duyệt. `controller.py` phải chạy bằng PowerShell
Administrator và mặc định chỉ nghe trên `127.0.0.1:8765`.

Thiết lập token dài, ngẫu nhiên trước khi chạy:

```powershell
$env:CONTROLLER_TOKEN = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }))
.\.venv\Scripts\python.exe extras\windows_enforcement\controller.py
```

Webhook `POST /mainflux` phải có header `X-Controller-Token` bằng đúng token trên. Không bind
`CONTROLLER_HOST=0.0.0.0` nếu chưa có firewall/reverse proxy TLS và cơ chế chuyển secret header.
Body tối đa 64 KiB và nội dung không được log.

Kiểm tra/thao tác hosts thủ công:

```powershell
powershell -NoProfile -File extras\windows_enforcement\facebook_enforcer.ps1 -Action Status
powershell -NoProfile -File extras\windows_enforcement\facebook_enforcer.ps1 -Action Block
powershell -NoProfile -File extras\windows_enforcement\facebook_enforcer.ps1 -Action Unblock
```

Script tạo backup ban đầu ở
`C:\Windows\System32\drivers\etc\hosts.camera-agent.backup`. State 3 `CAMERA_OFFLINE` bị ignore
để lỗi camera không tự thay đổi chính sách block hiện tại.

