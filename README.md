# 📝 Meeting Recorder — Ghi âm & sinh biên bản họp bằng AI

Ứng dụng 2 phần dùng được trên **điện thoại**:

- **Backend (FastAPI):** nhận file âm thanh → chuẩn hóa bằng `ffmpeg` → chép lời
  (chọn **tự host Whisper** hoặc **API STT có sẵn**) → gửi Claude API để sửa transcript +
  sinh biên bản dạng JSON.
- **Frontend (PWA):** ghi âm trực tiếp bằng `MediaRecorder` hoặc tải file lên, hiển thị
  biên bản đẹp, cho tải về MP3 / transcript / biên bản.

> ⚠️ **Lưu ý pháp lý:** Luôn đảm bảo **mọi bên tham gia đã đồng ý** trước khi ghi âm.
> Giao diện cũng có dòng nhắc này.

---

## 0. Tóm tắt triển khai

- **Cổng mặc định:** backend `127.0.0.1:8010`, frontend `127.0.0.1:5180` (đổi tùy môi trường).
- **STT:** `STT_PROVIDER=openai` (`STT_MODEL=whisper-1`, tiếng Việt) hoặc `local` (faster-whisper).
  Biên bản: Claude `claude-sonnet-4-6`.
- **Sau reverse proxy có prefix path:** đặt `PUBLIC_BASE_URL=https://<domain>/<prefix>/api`
  trong `backend/.env` để link MP3 trả về đúng đường dẫn.
- **Chạy nền bằng pm2** (chạy backend qua python venv với `-m uvicorn`, vì pm2 hiểu nhầm
  binary `uvicorn` là script Node):

  ```bash
  # Backend
  pm2 start ./backend/.venv/bin/python --name meeting-api --cwd ./backend \
    -- -m uvicorn main:app --host 127.0.0.1 --port 8010
  # Frontend
  pm2 start python3 --name meeting-web --cwd ./frontend \
    -- -m http.server 5180 --bind 127.0.0.1
  pm2 save
  ```

  Quản lý: `pm2 restart meeting-api`, `pm2 logs meeting-api`, `pm2 status`.

- **Chống Whisper "bịa" trên audio im lặng:** backend đặt STT `temperature=0` và có hàm
  `has_speech()` (ffmpeg `silencedetect`) — bản ghi gần như im lặng sẽ bị trả lỗi
  *"Không phát hiện giọng nói rõ…"* thay vì sinh biên bản bịa. → Phải **ghi có giọng nói rõ**.

Chi tiết cài đặt và các cách triển khai (LAN, ngrok, reverse proxy) ở các mục dưới.

---

## 1. Yêu cầu hệ thống

- **Python 3.10+**
- **ffmpeg** (bắt buộc — backend gọi qua dòng lệnh)
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - macOS: `brew install ffmpeg`
  - Windows: tải tại https://ffmpeg.org rồi thêm vào PATH
- **Khóa Anthropic API** (`ANTHROPIC_API_KEY`)
- Chép lời (chọn 1 trong 2, xem mục **2b**):
  - `STT_PROVIDER=local`: lần đầu `faster-whisper` tự tải model `medium` (~1.5 GB), chạy được offline, không tốn phí gọi STT.
  - `STT_PROVIDER=openai`: không cần GPU/không tải model, gọi API STT có sẵn (cần `OPENAI_API_KEY`).

---

## 2. Chạy Backend

```bash
cd backend

# (khuyến nghị) tạo virtualenv
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# cài thư viện
pip install -r requirements.txt

# cấu hình môi trường
cp .env.example .env
# Mở .env và điền ANTHROPIC_API_KEY=sk-ant-...

# chạy server
uvicorn main:app --host 0.0.0.0 --port 8010
```

Kiểm tra: mở `http://localhost:8010/` → thấy `{"status":"ok",...}`.

> 💡 Máy có **GPU NVIDIA**? Sửa trong `.env`: `WHISPER_DEVICE=cuda` và
> `WHISPER_COMPUTE_TYPE=float16` để chép lời nhanh hơn nhiều.

---

## 2b. Chọn cách chép lời (STT)

Đặt `STT_PROVIDER` trong `backend/.env`. **Frontend không đổi gì** — vẫn chỉ gọi `/upload`.

### Tự host Whisper (mặc định)

```env
STT_PROVIDER=local
WHISPER_MODEL=medium
```

Chạy được offline, không tốn phí gọi STT, nhưng cần tải model và chạy chậm trên CPU.

### Dùng API STT có sẵn (ngại tự dựng Whisper)

Backend gọi một endpoint **tương thích OpenAI** (`/audio/transcriptions`) nên dùng được
nhiều nhà cung cấp chỉ bằng cách đổi `OPENAI_BASE_URL` + `STT_MODEL`:

```env
# OpenAI (Whisper host sẵn)
STT_PROVIDER=openai
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
STT_MODEL=whisper-1           # hoặc gpt-4o-transcribe
```

```env
# Groq (nhanh, dùng whisper-large-v3)
STT_PROVIDER=openai
OPENAI_BASE_URL=https://api.groq.com/openai/v1
OPENAI_API_KEY=gsk_...
STT_MODEL=whisper-large-v3
```

Ở chế độ này **không cần cài faster-whisper** cũng không cần GPU. Kiểm tra cấu hình
hiện tại bằng cách mở `http://localhost:8010/` → xem `stt_provider` / `stt_model`.

---

## 3. Chạy Frontend

Frontend là HTML/JS tĩnh, chỉ cần một web server đơn giản:

```bash
cd frontend
python3 -m http.server 5180
```

Mở `http://localhost:5180` trên máy tính để thử trước.

---

## 4. Truy cập từ điện thoại qua HTTPS

`MediaRecorder` (ghi âm trong trình duyệt) **bắt buộc HTTPS** (trừ `localhost`).
Vì vậy để dùng trên điện thoại, hãy expose cả frontend và backend qua HTTPS.
Hai cách phổ biến:

### Cách A — ngrok (nhanh nhất, HTTPS hợp lệ riêng — không phụ thuộc cert domain)

ngrok đã được tải sẵn ở `~/.local/bin/ngrok`. Cấu hình 2 tunnel ở `deploy/ngrok.yml`.

**1) Thêm authtoken (1 lần):** lấy token tại
https://dashboard.ngrok.com/get-started/your-authtoken rồi:
```bash
~/.local/bin/ngrok config add-authtoken <TOKEN_CUA_BAN>
```

**2) Đảm bảo `PUBLIC_BASE_URL=` để trống trong `backend/.env`** (để link MP3 tự khớp
URL ngrok). Chạy backend với cờ proxy headers:
```bash
cd backend && . .venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8010 --proxy-headers --forwarded-allow-ips="*"
# Frontend (terminal khác)
cd frontend && python3 -m http.server 5180 --bind 127.0.0.1
```

**3) Mở 2 tunnel (1 lệnh):**
```bash
~/.local/bin/ngrok start --all \
  --config ~/.config/ngrok/ngrok.yml \
  --config /path/to/meeting-recorder/deploy/ngrok.yml
```
ngrok in ra 2 URL HTTPS: một cho **web** (5180), một cho **api** (8010).

**4) Trên điện thoại:**
- Mở URL HTTPS của **web** (vd `https://abc123.ngrok-free.app`).
- Trong ô **"Địa chỉ backend"** dán URL HTTPS của **api** (vd `https://def456.ngrok-free.app`).
  URL được lưu lại cho lần sau.

> CORS đã mở mặc định (`CORS_ORIGINS=*`) nên 2 domain ngrok khác nhau vẫn gọi được.
> URL ngrok free đổi mỗi lần chạy — chạy lại thì dán lại URL api mới.

### Cách B — Tailscale (ổn định, mạng riêng)

1. Cài Tailscale trên máy chủ và điện thoại, đăng nhập cùng tài khoản.
2. Bật HTTPS:
   ```bash
   tailscale serve --bg --https=8443 http://localhost:8010   # backend
   tailscale serve --bg --https=443  http://localhost:5180   # frontend
   ```
   (hoặc dùng `tailscale funnel` nếu cần truy cập ngoài mạng nội bộ)
3. Trên điện thoại mở URL `https://<tên-máy>.<tailnet>.ts.net` của frontend,
   và điền URL backend tương ứng vào ô cấu hình.

### Cách C — Qua domain nginx sẵn có (khuyến nghị: `your-domain.example.com`)

Reverse proxy dưới domain đang chạy → dùng luôn TLS + đường internet sẵn có,
1 origin nên không vướng CORS, ghi âm chạy được (HTTPS). Khi đó:

- Frontend: `https://your-domain.example.com/meeting/`
- Backend:  `https://your-domain.example.com/meeting/api` (ô "Địa chỉ backend" tự điền)

**1) Chạy 2 server (bind localhost, để nginx proxy):**
```bash
# Backend
cd backend && . .venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8010
# Frontend (terminal khác)
cd frontend && python3 -m http.server 5180 --bind 127.0.0.1
```

**2) Đặt `PUBLIC_BASE_URL` trong `backend/.env`** (để link MP3 trả về đúng prefix):
```env
PUBLIC_BASE_URL=https://your-domain.example.com/meeting/api
```

**3) Gắn block nginx** (file `deploy/nginx-meeting.locations`) vào server 443 của
`your-domain.example.com`. Thêm **1 dòng include** ngay trước `location / {` trong
`/etc/nginx/conf.d/your-site.conf`:
```nginx
    include /path/to/meeting-recorder/deploy/nginx-meeting.locations;
```

**4) Kiểm tra & reload nginx (cần sudo):**
```bash
sudo nginx -t && sudo nginx -s reload
```

Sau đó mở `https://your-domain.example.com/meeting/` trên điện thoại (4G/mạng bất kỳ).

> 💡 Hai server nên chạy nền bền (vd `pm2 start` hoặc `nohup ... &`/systemd) để
> không tắt khi đóng terminal.

### Cài như app (PWA)

Trên điện thoại, mở frontend bằng Safari (iOS) / Chrome (Android) →
menu → **"Thêm vào màn hình chính"**. Ứng dụng sẽ chạy toàn màn hình như app gốc.

---

## 5. Cách dùng

1. Mở app trên điện thoại, điền **địa chỉ backend** (chỉ cần 1 lần).
2. Bấm **Bắt đầu ghi âm** (cho họp trực tiếp/loa ngoài) hoặc **Tải lên file**.
3. Chờ xử lý (chuẩn hóa → chép lời → lập biên bản).
4. Xem biên bản: **Tóm tắt / Điểm chính / Quyết định / Việc cần làm**
   (kèm người phụ trách & hạn).
5. Tải về **MP3**, **transcript (.txt)**, **biên bản (.json)**.

---

## 6. API tham khảo

`POST /upload` — `multipart/form-data`, field `file` (mp3/m4a/webm/wav/ogg/mp4/aac/flac).

Trả về:

```json
{
  "id": "…",
  "mp3_url": "https://.../files/<id>.mp3",
  "transcript": "Người nói 1: …",
  "minutes": {
    "tom_tat": "…",
    "diem_chinh": ["…"],
    "quyet_dinh": ["…"],
    "viec_can_lam": [
      { "noi_dung": "…", "nguoi_phu_trach": "…", "han": "…" }
    ]
  }
}
```

---

## 7. Cấu trúc thư mục

```
meeting-recorder/
├── backend/
│   ├── main.py            # FastAPI: /upload, ffmpeg, whisper, Claude
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── index.html
    ├── styles.css
    ├── app.js             # MediaRecorder + upload + render
    ├── manifest.webmanifest
    ├── sw.js              # service worker (PWA)
    └── icon.svg
```

---

## 8. Khắc phục sự cố

| Triệu chứng | Nguyên nhân / cách xử lý |
|---|---|
| `Không tìm thấy ffmpeg` | Chưa cài ffmpeg hoặc chưa có trong PATH. |
| `Chưa cấu hình ANTHROPIC_API_KEY` | Điền khóa vào `backend/.env`. |
| Nút ghi âm không hoạt động trên điện thoại | Phải truy cập qua **HTTPS** (ngrok/tailscale), không phải `http://`. |
| Lần đầu chạy rất lâu | Đang tải model Whisper `medium` (~1.5 GB). |
| Chép lời chậm trên CPU | Dùng GPU (`WHISPER_DEVICE=cuda`), đổi `WHISPER_MODEL=small`, hoặc chuyển `STT_PROVIDER=openai`. |
| `chưa cấu hình OPENAI_API_KEY` | Đang ở `STT_PROVIDER=openai` mà thiếu key — điền `OPENAI_API_KEY` trong `.env`. |
| Gọi backend bị chặn CORS | Đặt `CORS_ORIGINS=*` (mặc định) trong `.env`. |
