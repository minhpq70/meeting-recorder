// ───────────── Tiện ích DOM ─────────────
const $ = (id) => document.getElementById(id);

const apiBaseInput = $("apiBase");
const recordBtn = $("recordBtn");
const fileInput = $("fileInput");
const timerEl = $("timer");
const progressEl = $("progress");
const progressText = $("progressText");
const errorEl = $("error");
const resultEl = $("result");

// Ghi nhớ địa chỉ backend.
// Nếu chưa lưu: đoán theo same-origin (hợp khi chạy sau reverse proxy,
// vd https://domain/meeting/  →  https://domain/meeting/api).
function guessApiBase() {
  if (location.protocol === "file:") return "";
  const dir = location.pathname.replace(/\/[^/]*$/, ""); // bỏ index.html
  return location.origin + dir + "/api";
}
apiBaseInput.value = localStorage.getItem("apiBase") || guessApiBase();
apiBaseInput.addEventListener("change", () =>
  localStorage.setItem("apiBase", apiBaseInput.value.trim())
);

let currentMinutes = null;
let currentTranscript = "";

// ───────────── Ghi âm bằng MediaRecorder ─────────────
let mediaRecorder = null;
let chunks = [];
let timerInt = null;
let seconds = 0;

recordBtn.addEventListener("click", async () => {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    chunks = [];
    const mime = MediaRecorder.isTypeSupported("audio/webm")
      ? "audio/webm"
      : "audio/mp4";
    mediaRecorder = new MediaRecorder(stream, { mimeType: mime });

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.push(e.data);
    };
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      stopTimer();
      recordBtn.classList.remove("recording");
      recordBtn.querySelector("span:last-child")?.remove();
      recordBtn.innerHTML = '<span class="dot"></span> Bắt đầu ghi âm';
      const ext = mime.includes("webm") ? "webm" : "mp4";
      const blob = new Blob(chunks, { type: mime });
      uploadFile(new File([blob], `ghi-am.${ext}`, { type: mime }));
    };

    mediaRecorder.start();
    startTimer();
    recordBtn.classList.add("recording");
    recordBtn.innerHTML = '<span class="dot"></span> Dừng & xử lý';
  } catch (err) {
    showError("Không truy cập được micro: " + err.message);
  }
});

function startTimer() {
  seconds = 0;
  timerEl.hidden = false;
  timerEl.textContent = "00:00";
  timerInt = setInterval(() => {
    seconds++;
    const m = String(Math.floor(seconds / 60)).padStart(2, "0");
    const s = String(seconds % 60).padStart(2, "0");
    timerEl.textContent = `${m}:${s}`;
  }, 1000);
}
function stopTimer() {
  clearInterval(timerInt);
  timerEl.hidden = true;
}

// ───────────── Tải lên file có sẵn ─────────────
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) uploadFile(fileInput.files[0]);
  fileInput.value = "";
});

// ───────────── Gửi tới backend ─────────────
async function uploadFile(file) {
  const base = apiBaseInput.value.trim().replace(/\/$/, "");
  if (!base) {
    showError("Vui lòng nhập địa chỉ backend ở trên.");
    return;
  }

  hide(errorEl);
  hide(resultEl);
  show(progressEl);
  progressText.textContent = "Đang tải lên & xử lý (chuẩn hóa → chép lời → lập biên bản)…";

  const form = new FormData();
  form.append("file", file);

  try {
    const res = await fetch(`${base}/upload`, { method: "POST", body: form });
    if (!res.ok) {
      let msg = `Lỗi ${res.status}`;
      try {
        const j = await res.json();
        if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch (_) {}
      throw new Error(msg);
    }
    const data = await res.json();
    renderResult(data);
  } catch (err) {
    showError("Xử lý thất bại: " + err.message);
  } finally {
    hide(progressEl);
  }
}

// ───────────── Hiển thị kết quả ─────────────
function renderResult(data) {
  currentMinutes = data.minutes;
  currentTranscript = data.transcript || "";
  const m = data.minutes || {};

  $("player").src = data.mp3_url;
  $("dlMp3").href = data.mp3_url;
  $("dlMp3").setAttribute("download", "cuoc-hop.mp3");

  $("tomTat").textContent = m.tom_tat || "(không có)";
  fillList("diemChinh", m.diem_chinh);
  fillList("quyetDinh", m.quyet_dinh);

  const tasksEl = $("viecCanLam");
  tasksEl.innerHTML = "";
  if (m.viec_can_lam && m.viec_can_lam.length) {
    m.viec_can_lam.forEach((t) => {
      const div = document.createElement("div");
      div.className = "task";
      div.innerHTML = `
        <div class="noi-dung">${escapeHtml(t.noi_dung || "")}</div>
        <div class="who">Phụ trách: <b>${escapeHtml(t.nguoi_phu_trach || "—")}</b>
          &nbsp;·&nbsp; Hạn: <b>${escapeHtml(t.han || "—")}</b></div>`;
      tasksEl.appendChild(div);
    });
  } else {
    tasksEl.innerHTML = "<p>(không có)</p>";
  }

  $("transcript").textContent = currentTranscript;
  show(resultEl);
  resultEl.scrollIntoView({ behavior: "smooth" });
}

function fillList(id, arr) {
  const el = $(id);
  el.innerHTML = "";
  if (arr && arr.length) {
    arr.forEach((x) => {
      const li = document.createElement("li");
      li.textContent = x;
      el.appendChild(li);
    });
  } else {
    el.innerHTML = "<li>(không có)</li>";
  }
}

// ───────────── Tải về transcript & biên bản ─────────────
$("dlTxt").addEventListener("click", () => {
  downloadBlob(currentTranscript, "transcript.txt", "text/plain");
});
$("dlJson").addEventListener("click", () => {
  downloadBlob(
    JSON.stringify(currentMinutes, null, 2),
    "bien-ban.json",
    "application/json"
  );
});

function downloadBlob(content, filename, type) {
  const blob = new Blob([content], { type: `${type};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ───────────── Helpers ─────────────
function show(el) { el.hidden = false; }
function hide(el) { el.hidden = true; }
function showError(msg) {
  errorEl.textContent = msg;
  show(errorEl);
}
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ───────────── Service worker (PWA) ─────────────
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}
