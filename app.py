import os
import re
import tempfile
import requests
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024

# Change this if you run your own LibreTranslate server.
TRANSLATE_URL = os.environ.get("TRANSLATE_URL", "https://libretranslate.com/translate")
TRANSLATE_API_KEY = os.environ.get("TRANSLATE_API_KEY", "")

def extract_video_id(url):
    patterns = [
        r"(?:youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def parse_vtt_or_srt(content):
    content = content.replace("\ufeff", "").replace("\r\n", "\n")
    blocks = re.split(r"\n\s*\n", content.strip())
    cues = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if lines[0].startswith("WEBVTT") or lines[0].startswith("NOTE"):
            continue
        time_line_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_line_index is None:
            continue
        time_line = lines[time_line_index]
        times = time_line.split("-->")
        if len(times) != 2:
            continue
        start = times[0].strip().split(" ")[0].replace(",", ".")
        end = times[1].strip().split(" ")[0].replace(",", ".")
        text_lines = lines[time_line_index + 1:]
        text = re.sub(r"<[^>]+>", "", " ".join(text_lines)).strip()
        if text:
            cues.append({"start": start, "end": end, "text": text})
    return cues

def translate_text(text, source):
    payload = {"q": text, "source": source, "target": "ko", "format": "text"}
    if TRANSLATE_API_KEY:
        payload["api_key"] = TRANSLATE_API_KEY
    response = requests.post(TRANSLATE_URL, json=payload, timeout=45)
    response.raise_for_status()
    data = response.json()
    translated = data.get("translatedText")
    if not translated:
        raise RuntimeError(data.get("error", "번역 결과가 비어 있습니다."))
    return translated

@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/subtitles")
def subtitles():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify(error="유효한 유튜브 영상 링크를 입력해 주세요."), 400

    try:
        import yt_dlp
    except ImportError:
        return jsonify(error="yt-dlp가 설치되지 않았습니다. requirements.txt의 설치 명령을 실행해 주세요."), 500

    with tempfile.TemporaryDirectory() as tmp:
        options = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "en-US", "en-GB", "ja", "ko", "ko-KR"],
            "subtitlesformat": "vtt/best",
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
            files = [os.path.join(tmp, f) for f in os.listdir(tmp)
                     if f.lower().endswith((".vtt", ".srt"))]
            if not files:
                return jsonify(error="가져올 수 있는 자막을 찾지 못했습니다. 이 영상은 자막이 없거나 유튜브에서 자막 접근을 제한할 수 있어요. 아래에서 SRT/VTT 파일을 직접 올려 보세요."), 404
            # Prefer English, then Japanese, then any available subtitle.
            def rank(path):
                name = os.path.basename(path).lower()
                if ".en." in name or ".en-" in name: return 0
                if ".ja." in name: return 1
                if ".ko." in name: return 3
                return 2
            chosen = sorted(files, key=rank)[0]
            with open(chosen, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            cues = parse_vtt_or_srt(raw)
            if not cues:
                return jsonify(error="자막 파일은 찾았지만 읽을 수 있는 자막 구간이 없습니다."), 422
            title = (info or {}).get("title", "유튜브 영상")
            return jsonify(videoId=video_id, title=title, cues=cues[:4000])
        except Exception as exc:
            return jsonify(error="자막을 가져오지 못했습니다. 링크를 확인하거나 SRT/VTT 파일 업로드를 이용해 주세요. (" + str(exc)[:180] + ")"), 502

@app.post("/api/translate")
def translate():
    body = request.get_json(silent=True) or {}
    cues = body.get("cues") or []
    source = body.get("source", "auto")
    if not isinstance(cues, list) or not cues or len(cues) > 4000:
        return jsonify(error="번역할 자막이 없거나 너무 많습니다."), 400
    allowed = {"auto", "en", "ja", "zh", "fr", "de", "es", "it", "pt", "ru"}
    if source not in allowed:
        source = "auto"
    try:
        # Translate in small batches to reduce requests and avoid oversized payloads.
        translated = []
        for cue in cues:
            translated.append({
                "start": cue["start"],
                "end": cue["end"],
                "text": translate_text(cue["text"], source)
            })
        return jsonify(cues=translated)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 502
        if code == 429:
            msg = "무료 번역 서비스 요청 한도에 도달했습니다. 잠시 후 다시 시도하거나 자체 호스팅 번역 서버를 설정해 주세요."
        elif code in (401, 403):
            msg = "번역 서비스에서 API 키가 필요하거나 요청을 거부했습니다. README의 번역 설정을 확인해 주세요."
        else:
            msg = f"번역 서비스 오류(HTTP {code}). 잠시 후 다시 시도해 주세요."
        return jsonify(error=msg), 502
    except requests.RequestException as exc:
        return jsonify(error="번역 서비스에 연결하지 못했습니다. 무료 번역 서버가 일시 중단되었거나 네트워크에서 차단되었을 수 있습니다. 잠시 후 다시 시도해 주세요. 관리자라면 README의 TRANSLATE_URL 설정을 확인하세요. (" + str(exc)[:120] + ")"), 502
    except Exception as exc:
        return jsonify(error="번역 처리 중 오류가 발생했습니다. (" + str(exc)[:120] + ")"), 502

@app.post("/api/upload")
def upload_subtitle():
    file = request.files.get("subtitle")
    if not file or not file.filename:
        return jsonify(error="SRT 또는 VTT 파일을 선택해 주세요."), 400
    filename = secure_filename(file.filename).lower()
    if not filename.endswith((".srt", ".vtt")):
        return jsonify(error="SRT 또는 VTT 파일만 지원합니다."), 400
    raw = file.read().decode("utf-8-sig", errors="replace")
    cues = parse_vtt_or_srt(raw)
    if not cues:
        return jsonify(error="파일에서 자막 구간을 읽지 못했습니다. SRT/VTT 형식을 확인해 주세요."), 422
    return jsonify(cues=cues[:4000], filename=filename)

@app.get("/health")
def health():
    return jsonify(ok=True)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
