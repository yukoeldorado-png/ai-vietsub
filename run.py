import os, re, time, json, asyncio, subprocess, requests, shutil, glob
from google import genai
from google.genai import types

RAW_KEYS = os.environ.get("GEMINI_API_KEY", "").strip()
API_KEYS = [k.strip() for k in re.split(r'[,;\n\s]+', RAW_KEYS) if k.strip()]

if not API_KEYS:
    raise Exception("Chưa cài GEMINI_API_KEY trong GitHub Secrets!")

print(f"[✓] Đã nạp thành công {len(API_KEYS)} Gemini API Key dự phòng!", flush=True)

CHANNEL_SUB_NAME = "IYaN AI"

CONFIG = {
    "models": ["gemini-3.6-flash"],
    "font_family": "Roboto",
    "font_size": 17,
    "outline_size": 2.6,
    "sub_gap": 18,
    "default_y1": 0.88,
    "use_blur": False
}

# TỰ ĐỘNG NHẬN DỮ LIỆU TỪ BẢNG TÙY CHỌN GITHUB ACTIONS (NẾU CÓ)
if os.environ.get("INPUT_USE_BLUR"):
    CONFIG["use_blur"] = os.environ.get("INPUT_USE_BLUR").lower() == "true"

if os.environ.get("INPUT_SUB_GAP"):
    gap_match = re.search(r'\d+', os.environ.get("INPUT_SUB_GAP"))
    if gap_match:
        CONFIG["sub_gap"] = int(gap_match.group(0))

print(f"[⚙️] CẤU HÌNH ĐÃ CHỌN: Vệt mờ = {CONFIG['use_blur']} | Khoảng cách nâng sub = +{CONFIG['sub_gap']}", flush=True)

YAN_BASE_INSTRUCTIONS = """
Persona: YaN - Chuyên gia Biên dịch ngôn ngữ & Bản địa hóa phim hoạt hình Trung Quốc.
NHIỆM VỤ CỐT LÕI: THUẦN QUÉT CHỮ TRÊN HÌNH ẢNH (VIDEO OCR) & DỊCH PHỤ ĐỀ SANG TIẾNG VIỆT.
TỐI HẬU THƯ BẮT BUỘC:
1. 100% THUẦN OCR HÌNH ẢNH: Video này không có âm thanh. Hãy dùng thị giác đọc chính xác 100% các dòng chữ phụ đề tiếng Trung xuất hiện trên khung hình video.
2. TUYỆT ĐỐI KHÔNG BỎ SÓT: Bất kỳ dòng chữ phụ đề tiếng Trung nào hiện lên trên màn hình (dù chỉ là 1-2 từ cực ngắn) đều PHẢI bóc tách và dịch đầy đủ sang tiếng Việt.
3. KHẮC PHỤC LỖI LRC - ĐỒNG BỘ THỜI GIAN THEO HÌNH ẢNH:
   - Thời gian bắt đầu: Thời điểm dòng chữ tiếng Trung BẮT ĐẦU HIỆN TRÊN MÀN HÌNH.
   - Thời gian kết thúc: Thời điểm dòng chữ tiếng Trung BIẾN MẤT KHỎI MÀN HÌNH. Khi chữ tiếng Trung biến mất, phụ đề PHẢI KẾT THÚC NGAY LẬP TỨC. Tuyệt đối không để phụ đề kéo dài lơ lửng sang cảnh sau như file LRC.
4. ĐỊNH DẠNG BẮT BUỘC (SRT CHUẨN ĐƠN DÒNG):
   Mỗi entry phụ đề chỉ nằm trên DUY NHẤT 1 DÒNG ĐƠN (tối đa 10-12 từ).
   00:mm:ss,mmm --> 00:mm:ss,mmm
5. CẤM dấu ngoặc kép ("). Tuyệt đối cách nhau 1 dòng trống. Không xuất lời dẫn.
"""

CJK_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
if not os.path.exists(CJK_FONT):
    CJK_FONT = "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"

capcut_client = None
try:
    from capcut_tts_api import CapCutClient
    capcut_client = CapCutClient(timeout=45)
    print("[✓] Đã kết nối CapCut Voice API thành công!", flush=True)
except Exception as e:
    print(f"[❌] Lỗi kết nối CapCut API: {e}", flush=True)

def get_video_dimensions(video_path: str):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", video_path]
    try:
        data = json.loads(subprocess.run(cmd, stdout=subprocess.PIPE, text=True).stdout)["streams"][0]
        return int(data["width"]), int(data["height"])
    except: return 1920, 1080

def get_audio_duration(file_path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path]
    try: return float(subprocess.run(cmd, stdout=subprocess.PIPE, text=True).stdout.strip())
    except: return 2.0

def clean_timestamp_str(t: str) -> str:
    t = t.strip().replace('.', ',')
    parts = t.split(':')
    if len(parts) == 2: return f"00:{int(parts[0]):02d}:{parts[1]}"
    elif len(parts) == 3: return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{parts[2]}"
    return t

def srt_time_to_seconds(t_str: str) -> float:
    t_str = t_str.strip().replace('.', ',')
    parts = t_str.split(':')
    if len(parts) == 2:
        m, s = parts; sec, ms = s.split(',')
        return int(m) * 60 + int(sec) + int(ms) / 1000.0
    elif len(parts) == 3:
        h, m, s = parts; sec, ms = s.split(',')
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0
    return 0.0

def seconds_to_srt_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        s += 1; ms -= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def rebuild_pure_srt(raw_text: str) -> str:
    pattern = r'(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}|\d{1,2}:\d{2}[,\.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}|\d{1,2}:\d{2}[,\.]\d{1,3})'
    matches = list(re.finditer(pattern, raw_text))
    if not matches: return raw_text

    raw_entries = []
    for i in range(len(matches)):
        st = clean_timestamp_str(matches[i].group(1))
        et = clean_timestamp_str(matches[i].group(2))
        p1 = matches[i].end()
        p2 = matches[i+1].start() if i + 1 < len(matches) else len(raw_text)

        lines = [l.strip() for l in raw_text[p1:p2].split('\n') if l.strip()]
        valid = [l for l in lines if not re.match(r'^\d+$', l) and '-->' not in l]
        txt = " ".join(valid).replace('"', '').strip()
        if txt:
            raw_entries.append((srt_time_to_seconds(st), srt_time_to_seconds(et), txt))

    cleaned = []
    for i in range(len(raw_entries)):
        s, e, txt = raw_entries[i]
        if i + 1 < len(raw_entries):
            next_s = raw_entries[i+1][0]
            if e > next_s:
                e = max(s + 0.5, next_s - 0.05)
        if e <= s:
            e = s + 1.0
        cleaned.append((seconds_to_srt_time(s), seconds_to_srt_time(e), txt))

    return "\n\n".join([f"{idx}\n{s} --> {e}\n{t}" for idx, (s, e, t) in enumerate(cleaned, 1)])

def parse_srt(srt_file: str):
    items = []
    if not os.path.exists(srt_file): return items
    with open(srt_file, "r", encoding="utf-8") as f: content = f.read()
    for m in re.finditer(r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\Z)', content, re.DOTALL):
        h1, mn1, s1 = m.group(2).split(':'); s1, ms1 = s1.split(',')
        h2, mn2, s2 = m.group(3).split(':'); s2, ms2 = s2.split(',')
        start = int(h1)*3600 + int(mn1)*60 + int(s1) + int(ms1)/1000.0
        end   = int(h2)*3600 + int(mn2)*60 + int(s2) + int(ms2)/1000.0
        items.append((start, end, m.group(4).strip().replace('\n', ' ')))
    return items

def get_srt_from_gemini_auto(video_path: str, model_list=CONFIG["models"], output_srt: str = "temp_sub.srt"):
    safe_video_path = "upload_for_gemini_muted.mp4"
    print("[*] Đang tách sạch âm thanh để ép Gemini 100% thuần OCR chữ trên hình...", flush=True)
    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-an", "-c:v", "copy", safe_video_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(safe_video_path) or os.path.getsize(safe_video_path) == 0:
        shutil.copyfile(video_path, safe_video_path)

    resp = None
    first_sec = 2.0

    config = types.GenerateContentConfig(
        system_instruction=YAN_BASE_INSTRUCTIONS,
        temperature=0.1,
        max_output_tokens=65536
    )

    for key_idx, current_key in enumerate(API_KEYS, 1):
        print(f"\n🔑 =========================================", flush=True)
        print(f"🔑 ĐANG SỬ DỤNG API KEY #{key_idx}...", flush=True)
        print(f"🔑 =========================================", flush=True)
        current_client = genai.Client(api_key=current_key)
        v_file = None

        try:
            print("[*] Đang tải video câm lên Google Gemini để chạy Thuần OCR...", flush=True)
            v_file = current_client.files.upload(file=safe_video_path)
            while v_file.state.name == "PROCESSING":
                time.sleep(1)
                v_file = current_client.files.get(name=v_file.name)
            if v_file.state.name != "ACTIVE":
                raise Exception("Lỗi xử lý file video trên Gemini.")

            video_part = types.Part(
                file_data=types.FileData(file_uri=v_file.uri),
                video_metadata=types.VideoMetadata(fps=24)
            )

            for model_name in model_list:
                print(f"[*] Thuần OCR với model: {model_name} (Key #{key_idx}, FPS: 24)...", flush=True)
                attempt = 1
                while True:
                    try:
                        r = current_client.models.generate_content(
                            model=model_name,
                            contents=[video_part, "Quét chữ phụ đề tiếng Trung trên màn hình (OCR) và xuất SRT chuẩn 1 dòng."],
                            config=config
                        )
                        if r and r.text:
                            resp = r
                            print(f"[✓✓✓] Thuần OCR 24 FPS thành công rực rỡ với {model_name}!", flush=True)
                            break
                    except Exception as e:
                        err_str = str(e)
                        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                            print(f"[⚠️] {model_name} hết lượt sử dụng. Chuyển sang Key tiếp theo sau 0.2s...", flush=True)
                            time.sleep(0.2)
                            break

                        print(f"[!] {model_name} bận (lần {attempt}): {e}. Gửi lại ngay sau 0.2s...", flush=True)
                        time.sleep(0.2)
                        attempt += 1

                if resp: break

        except Exception as e:
            print(f"[!] Key #{key_idx} gặp lỗi: {e}", flush=True)
        finally:
            if v_file:
                try: current_client.files.delete(name=v_file.name)
                except: pass

        if resp and resp.text:
            break
        else:
            if key_idx < len(API_KEYS):
                print(f"[⏩] Chuyển sang API Key #{key_idx + 1} sau 0.2s...\n", flush=True)
                time.sleep(0.2)

    if not resp or not resp.text:
        raise Exception("Không thể OCR từ các API Key dự phòng!")

    pure_srt = rebuild_pure_srt(resp.text)
    with open(output_srt, "w", encoding="utf-8") as f:
        f.write(pure_srt)

    first_time_match = re.search(r'00:(\d{2}):(\d{2}),(\d{3})', pure_srt)
    if first_time_match:
        first_sec = int(first_time_match.group(1)) * 60 + int(first_time_match.group(2)) + 0.5
    return output_srt, first_sec

def detect_sub_top_from_frame(video_path: str, timestamp_sec: float = 2.0):
    img_path = "sub_sample.jpg"
    subprocess.run(["ffmpeg", "-y", "-ss", str(timestamp_sec), "-i", video_path, "-vframes", "1", img_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(img_path): return CONFIG["default_y1"]
    try:
        with open(img_path, "rb") as f: img_bytes = f.read()
        for key in API_KEYS:
            try:
                c = genai.Client(api_key=key)
                res = c.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=[
                        types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                        "Hãy nhìn vào dòng chữ phụ đề tiếng Trung dưới đáy ảnh. Báo duy nhất 1 con số phần trăm: mép trên cùng của dòng chữ phụ đề tiếng Trung (y_top tính từ đỉnh ảnh xuống)."
                    ]
                ).text
                nums = re.findall(r'\d+', res)
                if nums:
                    y1 = int(nums[0]) / 100.0
                    if 0.70 <= y1 <= 0.96:
                        print(f"[✓] AI tự động phát hiện mép trên sub gốc của bộ phim này: {y1*100:.1f}%", flush=True)
                        return y1
                break
            except:
                time.sleep(0.2)
                continue
    except: pass
    return CONFIG["default_y1"]

def generate_voice_file(text: str, out_p: str) -> bool:
    if not capcut_client:
        return False
    
    v_id = "BV421_vivn_streaming"
    attempt = 1
    while True:
        try:
            res = capcut_client.generate_speech(text, voice=v_id, timeout=15)
            capcut_client.download_tts_audio(res, out_p)
            if os.path.exists(out_p) and os.path.getsize(out_p) > 0:
                return True
        except Exception as e:
            if attempt % 5 == 0:
                print(f"[!] CapCut thử lại lần {attempt} cho câu: '{text[:20]}...' ({e})", flush=True)
            time.sleep(0.3)
        attempt += 1

def build_synced_tts_track(srt_file: str, output_audio: str = "full_tts.wav"):
    items = parse_srt(srt_file)
    inputs, parts = [], []
    os.makedirs("temp_tts", exist_ok=True)
    idx = 0
    print(f"[*] Đang tạo giọng đọc Nguồn Nhỏ Ngọt Ngào cho {len(items)} câu thoại...", flush=True)
    for start_sec, end_sec, text in items:
        if not text or text.startswith("["): continue
        raw_p = f"temp_tts/raw_{idx}.mp3"
        fit_p = f"temp_tts/fit_{idx}.mp3"
        try:
            if generate_voice_file(text, raw_p):
                audio_dur = get_audio_duration(raw_p)
                max_allowed_dur = max(0.5, end_sec - start_sec)
                if audio_dur > max_allowed_dur:
                    speed_factor = min(1.85, audio_dur / max_allowed_dur)
                    subprocess.run(["ffmpeg", "-y", "-i", raw_p, "-filter:a", f"atempo={speed_factor:.2f}", "-vn", fit_p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    chosen_p = fit_p
                else: chosen_p = raw_p

                inputs.extend(["-i", chosen_p])
                d_ms = int(start_sec * 1000)
                parts.append(f"[{idx}]adelay={d_ms}|{d_ms}[a{idx}];")
                idx += 1
        except: pass

    if idx > 0:
        chain = "".join([f"[a{i}]" for i in range(idx)])
        fc = "".join(parts) + f"{chain}amix=inputs={idx}:dropout_transition=0:normalize=0[out]"
        subprocess.run(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[out]", output_audio], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_audio
    return None

def render_final_video_auto(input_video, srt_file, tts_audio, output_final, sub_name, y_top_detected):
    print("[*] Đang render video theo cấu hình đã chọn...", flush=True)
    w, h = get_video_dimensions(input_video)

    t_sub = sub_name.replace("'", "").replace(":", " -")
    srt_p = os.path.abspath(srt_file).replace("\\", "/")

    font_name = CONFIG["font_family"]
    fonts_dir_param = ""
    for custom_font in ["font.ttf", "font.otf", "Font.ttf"]:
        if os.path.exists(custom_font):
            font_name = os.path.splitext(custom_font)[0]
            fonts_dir_param = f":fontsdir='.'"
            print(f"[✓] Sử dụng file phông chữ riêng: {custom_font}", flush=True)
            break

    font_size = CONFIG["