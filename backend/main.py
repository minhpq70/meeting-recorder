"""
Backend xử lý ghi âm cuộc họp.

Luồng /upload:
  1. Nhận file âm thanh (mp3/m4a/webm/wav...).
  2. Dùng ffmpeg chuẩn hóa âm lượng (loudnorm) và xuất ra .mp3.
  3. Transcribe — chọn provider qua STT_PROVIDER:
       - "local"  : faster-whisper tự host (model medium, tiếng Việt).
       - "openai" : gọi API tương thích OpenAI (Whisper host sẵn / Groq...).
  4. Gửi transcript tới Claude API để: (1) sửa chính tả/ngắt câu/gắn nhãn người nói,
     (2) sinh biên bản dạng JSON.
  5. Trả về JSON: link mp3, transcript đã sửa, biên bản.
"""

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

# ─────────────────────────── Cấu hình ───────────────────────────
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# STT: "local" (faster-whisper) hoặc "openai" (API tương thích OpenAI)
STT_PROVIDER = os.getenv("STT_PROVIDER", "local").lower()
STT_LANGUAGE = os.getenv("STT_LANGUAGE", os.getenv("WHISPER_LANGUAGE", "vi"))

# Cấu hình faster-whisper (chỉ dùng khi STT_PROVIDER=local)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

# Cấu hình STT đám mây (chỉ dùng khi STT_PROVIDER=openai)
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
STT_MODEL = os.getenv("STT_MODEL", "whisper-1")

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

# URL công khai của backend (khi chạy sau reverse proxy có prefix path).
# Vd qua nginx: https://your-domain.example.com/meeting/api
# Để trống = tự suy ra từ request (dùng khi chạy trực tiếp localhost/LAN).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {".mp3", ".m4a", ".webm", ".wav", ".ogg", ".mp4", ".aac", ".flac"}

# ─────────────────── Khởi tạo model & client (lazy) ───────────────────
_whisper_model = None


def get_whisper():
    """Nạp model faster-whisper một lần, dùng lại cho các request sau."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        _whisper_model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
    return _whisper_model


def get_claude() -> anthropic.Anthropic:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(
            status_code=500,
            detail="Chưa cấu hình ANTHROPIC_API_KEY trong file .env",
        )
    return anthropic.Anthropic()


app = FastAPI(title="Meeting Recorder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Phục vụ file mp3 đã xuất tại /files/<tên file>
app.mount("/files", StaticFiles(directory=str(OUTPUT_DIR)), name="files")


# ─────────────────────────── Bước ffmpeg ───────────────────────────
def normalize_to_mp3(src: Path, dst: Path) -> None:
    """Chuẩn hóa âm lượng (loudnorm) và xuất .mp3."""
    if not shutil.which("ffmpeg"):
        raise HTTPException(
            status_code=500,
            detail="Không tìm thấy ffmpeg. Hãy cài ffmpeg trước khi chạy backend.",
        )
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(src),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "44100",
        "-ac", "1",
        "-b:a", "192k",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        raise HTTPException(
            status_code=500,
            detail=f"ffmpeg lỗi khi xử lý âm thanh: {proc.stderr[-800:]}",
        )


# ──────────────────── Phát hiện có giọng nói (chống ảo giác) ────────────────────
def has_speech(audio: Path, noise_db: int = -40, min_speech_sec: float = 0.8) -> bool:
    """Ước lượng bản ghi có giọng nói không (dùng ffmpeg silencedetect).

    Trả False nếu gần như toàn bộ là im lặng — để chặn việc Whisper "bịa" nội
    dung trên audio trống. Chạy trên FILE GỐC (trước loudnorm) để ngưỡng dB đúng.
    Nếu không xác định được thời lượng thì cho qua (trả True) để khỏi chặn nhầm.
    """
    cmd = ["ffmpeg", "-i", str(audio), "-af",
           f"silencedetect=noise={noise_db}dB:d=0.5", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stderr

    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", out)
    if not m:
        return True
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    silence = sum(float(x) for x in re.findall(r"silence_duration: (\d+\.?\d*)", out))
    if duration <= 0:
        return True
    return (duration - silence) >= min_speech_sec


# ─────────────────────────── Bước Whisper ───────────────────────────
def transcribe_local(audio: Path) -> str:
    """Chép lời bằng faster-whisper tự host."""
    model = get_whisper()
    segments, _info = model.transcribe(
        str(audio),
        language=STT_LANGUAGE,
        vad_filter=True,
        beam_size=5,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def transcribe_openai(audio: Path) -> str:
    """Chép lời qua API tương thích OpenAI (Whisper host sẵn, Groq, ...)."""
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="STT_PROVIDER=openai nhưng chưa cấu hình OPENAI_API_KEY trong .env",
        )
    url = f"{OPENAI_BASE_URL}/audio/transcriptions"
    with audio.open("rb") as f:
        files = {"file": (audio.name, f, "audio/mpeg")}
        data = {
            "model": STT_MODEL,
            "language": STT_LANGUAGE,
            "response_format": "json",
            "temperature": "0",  # giảm ảo giác (hallucination) của Whisper
        }
        try:
            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files=files,
                data=data,
                timeout=300.0,
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Lỗi gọi STT đám mây: {e}")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"STT đám mây trả lỗi {resp.status_code}: {resp.text[:500]}",
        )
    return (resp.json().get("text") or "").strip()


def transcribe(audio: Path) -> str:
    """Chép lời theo provider cấu hình trong STT_PROVIDER."""
    if STT_PROVIDER == "openai":
        return transcribe_openai(audio)
    if STT_PROVIDER == "local":
        return transcribe_local(audio)
    raise HTTPException(
        status_code=500,
        detail=f"STT_PROVIDER='{STT_PROVIDER}' không hợp lệ (dùng 'local' hoặc 'openai').",
    )


# ─────────────────────────── Bước Claude ───────────────────────────
CORRECT_SYSTEM = (
    "Bạn là trợ lý biên tập biên bản họp tiếng Việt. Nhiệm vụ: nhận transcript thô "
    "(do nhận dạng giọng nói tự động sinh ra, có thể sai chính tả, thiếu dấu câu) và "
    "trả về bản đã được biên tập sạch sẽ. Yêu cầu: sửa lỗi chính tả, thêm dấu câu, "
    "ngắt câu và xuống dòng hợp lý theo từng ý/từng lượt nói. Nếu nhận ra có nhiều "
    "người nói khác nhau, gắn nhãn người nói ở đầu dòng (ví dụ 'Người nói 1:', "
    "'Chủ trì:'). KHÔNG tóm tắt, KHÔNG bịa thêm nội dung, giữ nguyên ý nghĩa gốc. "
    "Chỉ trả về văn bản transcript đã biên tập, không thêm lời dẫn."
)

MINUTES_SYSTEM = (
    "Bạn là thư ký cuộc họp. Dựa trên transcript được cung cấp, hãy lập biên bản họp "
    "súc tích, chính xác bằng tiếng Việt. Chỉ dùng thông tin có trong transcript, "
    "không bịa. Nếu một mục không có thông tin, để mảng rỗng."
)

MINUTES_SCHEMA = {
    "type": "object",
    "properties": {
        "tom_tat": {
            "type": "string",
            "description": "Tóm tắt ngắn gọn nội dung và mục đích cuộc họp.",
        },
        "diem_chinh": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Các điểm chính được thảo luận.",
        },
        "quyet_dinh": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Các quyết định đã được thống nhất.",
        },
        "viec_can_lam": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "noi_dung": {"type": "string"},
                    "nguoi_phu_trach": {"type": "string"},
                    "han": {"type": "string"},
                },
                "required": ["noi_dung", "nguoi_phu_trach", "han"],
                "additionalProperties": False,
            },
            "description": "Danh sách việc cần làm kèm người phụ trách và hạn hoàn thành.",
        },
    },
    "required": ["tom_tat", "diem_chinh", "quyet_dinh", "viec_can_lam"],
    "additionalProperties": False,
}


def correct_transcript(client: anthropic.Anthropic, raw: str) -> str:
    """Gửi transcript thô tới Claude để biên tập (sửa lỗi, ngắt câu, gắn nhãn người nói)."""
    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=CORRECT_SYSTEM,
        messages=[{"role": "user", "content": f"Transcript thô:\n\n{raw}"}],
    ) as stream:
        message = stream.get_final_message()
    return "".join(b.text for b in message.content if b.type == "text").strip()


def generate_minutes(client: anthropic.Anthropic, transcript: str) -> dict:
    """Sinh biên bản dạng JSON theo schema cố định."""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8000,
        system=MINUTES_SYSTEM,
        messages=[{"role": "user", "content": f"Transcript cuộc họp:\n\n{transcript}"}],
        output_config={"format": {"type": "json_schema", "schema": MINUTES_SCHEMA}},
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# ─────────────────────────── Endpoints ───────────────────────────
@app.get("/")
def health():
    stt = STT_MODEL if STT_PROVIDER == "openai" else WHISPER_MODEL
    return {"status": "ok", "model": CLAUDE_MODEL, "stt_provider": STT_PROVIDER, "stt_model": stt}


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=422,
            detail=f"Định dạng '{ext}' không hỗ trợ. Cho phép: {sorted(ALLOWED_EXT)}",
        )

    uid = uuid.uuid4().hex
    src_path = UPLOAD_DIR / f"{uid}{ext}"
    mp3_path = OUTPUT_DIR / f"{uid}.mp3"

    # 1) Lưu file upload
    with src_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        # 1b) Chặn bản ghi không có giọng nói (tránh Whisper bịa nội dung)
        if not has_speech(src_path):
            raise HTTPException(
                status_code=422,
                detail="Không phát hiện giọng nói rõ trong bản ghi (âm thanh quá nhỏ "
                       "hoặc im lặng). Hãy ghi lại gần micro và nói to, rõ hơn.",
            )

        # 2) Chuẩn hóa + xuất mp3
        normalize_to_mp3(src_path, mp3_path)

        # 3) Transcribe
        raw_transcript = transcribe(mp3_path)
        if not raw_transcript:
            raise HTTPException(
                status_code=422,
                detail="Không nhận dạng được nội dung nào từ file âm thanh.",
            )

        # 4) Claude: sửa transcript + sinh biên bản
        client = get_claude()
        transcript = correct_transcript(client, raw_transcript)
        minutes = generate_minutes(client, transcript)

        # Lưu transcript ra file (để tải .txt nếu cần)
        (OUTPUT_DIR / f"{uid}.txt").write_text(transcript, encoding="utf-8")
    finally:
        # Xóa file gốc upload, chỉ giữ mp3 đã chuẩn hóa
        src_path.unlink(missing_ok=True)

    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return JSONResponse(
        {
            "id": uid,
            "mp3_url": f"{base}/files/{uid}.mp3",
            "transcript": transcript,
            "minutes": minutes,
        }
    )
