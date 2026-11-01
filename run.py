import os, re, time, json, asyncio, subprocess, requests, shutil, glob
from PIL import Image, ImageDraw, ImageFont
from google import genai
from google.genai import types

# 1. CẤU HÌNH
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
if not GEMINI_KEY:
    raise Exception("Chưa cài GEMINI_API_KEY trong GitHub Secrets!")

client = genai.Client(api_key=GEMINI_KEY)

CHANNEL_SUB_NAME = "BY vietsub"

CONFIG = {
    "models": ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.8-flash"],
    "voice": "CapCut - Nguồn Nhỏ Ngọt Ngào (BV421)",
    "font_family": "Comfortaa",
    "font_size": 16
}

CJK_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
if not os.path.exists(CJK_FONT):
    CJK_FONT = "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"

capcut_client = None
try:
    from capcut_tts_api import CapCutClient
    capcut_client = CapCutClient(timeout=45)
    print("[✓] Đã kết nối CapCut Voice API!")
except Exception as e:
    print(f"[!] CapCut lỗi ({e}), chuyển sang EdgeTTS")

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

def sec_to_time_str(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    ms = int(round((s - int(s)) * 1000))
    if ms >= 1000: ms = 999
    return f"{h:02d}:{m:02d}:{int(s):02d},{ms:03d}"

# ==================== BƯỚC 1: DÒ TOẠ ĐỘ VÙNG PHỤ ĐỀ ĐỘNG ====================
def detect_sub_zone_dynamic(video_path: str, model_choice: str = "gemini-3.7-flash"):
    print("[*] Đang tự động dò tìm vị trí thanh phụ đề của bộ phim này...")
    total_dur = get_audio_duration(video_path)
    sample_times = [total_dur * 0.15, total_dur * 0.35, total_dur * 0.65]
    img_path = "sub_detect_sample.jpg"
    target_m = model_choice.split(" ")[0].strip()

    for test_time in sample_times:
        subprocess.run(["ffmpeg", "-y", "-ss", f"{test_time:.2f}", "-i", video_path, "-vframes", "1", img_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.exists(img_path): continue

        try:
            with open(img_path, "rb") as f: img_bytes = f.read()

            prompt = (
                "Hãy tìm dòng chữ phụ đề lời thoại tiếng Trung của nhân vật chạy ở nửa dưới ảnh. "
                "Báo toạ độ phần trăm từ đỉnh ảnh xuống (0 đến 100): y_start (mép trên dòng chữ) và y_end (mép dưới dòng chữ)."
            )

            config = types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema={
                    "type": "OBJECT",
                    "properties": {
                        "found_subtitle": {"type": "BOOLEAN"},
                        "y_start": {"type": "INTEGER"},
                        "y_end": {"type": "INTEGER"}
                    },
                    "required": ["found_subtitle", "y_start", "y_end"]
                }
            )

            res = client.models.generate_content(
                model=target_m,
                contents=[types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"), prompt],
                config=config
            )

            data = json.loads(res.text)
            if data.get("found_subtitle"):
                y1 = int(data.get("y_start", 0))
                y2 = int(data.get("y_end", 0))
                if y1 > y2: y1, y2 = y2, y1
                diff = y2 - y1
                if 4 <= diff <= 18 and 50 <= y1 <= 96:
                    y1_safe = max(0.50, (y1 - 2.0) / 100.0)
                    y2_safe = min(1.0, (y2 + 2.0) / 100.0)
                    print(f"[✓] Đã bắt trúng toạ độ phụ đề: {y1_safe*100:.1f}% -> {y2_safe*100:.1f}%")
                    return y1_safe, y2_safe
        except: pass

    print("[!] Dùng toạ độ đáy mặc định: 86% -> 98%")
    return 0.86, 0.98

# ==================== BƯỚC 2: NGHE AUDIO 16kHz ĐỂ BẮT TIMESTAMP ====================
def get_audio_speech_intervals(video_path: str, model_choice: str = "gemini-3.7-flash"):
    print("[*] Đang nghe dải âm thanh 16.000 Hz để trích xuất từng mốc câu nói (Không bao giờ bị 1 FPS)...")
    audio_wav = "audio_16k.wav"
    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1", audio_wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    a_file = client.files.upload(file=audio_wav)
    while a_file.state.name == "PROCESSING":
        time.sleep(1.5)
        a_file = client.files.get(name=a_file.name)

    prompt = """
    Hãy nghe kỹ toàn bộ dải âm thanh này.
    Nhiệm vụ: Trích xuất chính xác thời điểm bắt đầu (start) và kết thúc (end) tính bằng giây của từng câu nói của nhân vật.
    QUY TẮC BẮT BUỘC:
    1. Tách nhỏ từng câu: Khi nhân vật ngừng nói hoặc chuyển câu ngắn, BẮT BUỘC ngắt thành mốc riêng.
    2. Tuyệt đối không gộp các câu thoại độc lập vào nhau.
    3. Định dạng JSON: Trả về danh sách [{"start": float, "end": float}].
    """

    config = types.GenerateContentConfig(
        temperature=0.0,
        response_mime_type="application/json",
        response_schema={
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start": {"type": "NUMBER"},
                    "end": {"type": "NUMBER"}
                },
                "required": ["start", "end"]
            }
        }
    )

    target_m = model_choice.split(" ")[0].strip()
    res = client.models.generate_content(model=target_m, contents=[a_file, prompt], config=config)

    try: client.files.delete(name=a_file.name)
    except: pass
    if os.path.exists(audio_wav): os.remove(audio_wav)

    intervals = json.loads(res.text)
    # Lọc các mốc hợp lệ
    valid_intervals = []
    for item in intervals:
        s = float(item.get("start", 0))
        e = float(item.get("end", 0))
        if e > s and (e - s) >= 0.3:
            valid_intervals.append({"start": s, "end": e})

    print(f"[✓] Đã bắt được {len(valid_intervals)} mốc câu nói độc lập từ âm thanh!")
    return valid_intervals

# ==================== BƯỚC 3: CHỤP TỪNG THANH CHỮ & GHÉP THÀNH FILMSTRIP ====================
def build_composite_filmstrip(video_path: str, intervals, y1_pct: float, y2_pct: float):
    w, h = get_video_dimensions(video_path)
    crop_y = int(h * y1_pct)
    crop_h = int(h * (y2_pct - y1_pct))

    os.makedirs("sub_crops", exist_ok=True)
    crop_images = []

    print(f"[*] Đang chụp ảnh thanh phụ đề của từng mốc thoại ({len(intervals)} ảnh)...")
    for idx, item in enumerate(intervals):
        mid_time = (item["start"] + item["end"]) / 2.0
        crop_file = f"sub_crops/crop_{idx:03d}.jpg"
        
        # Chụp đúng thanh chữ tại thời điểm câu đang nói
        cmd = [
            "ffmpeg", "-y", "-ss", f"{mid_time:.3f}", "-i", video_path,
            "-vframes", "1",
            "-vf", f"crop=iw:{crop_h}:0:{crop_y},scale=960:-1",
            crop_file
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if os.path.exists(crop_file):
            try:
                img = Image.open(crop_file).convert("RGB")
                crop_images.append(img)
            except:
                # Ảnh trống dự phòng nếu lỗi
                crop_images.append(Image.new("RGB", (960, 60), color=(0, 0, 0)))
        else:
            crop_images.append(Image.new("RGB", (960, 60), color=(0, 0, 0)))

    # Ghép toàn bộ ảnh thành một dải cuộn phim dọc (Filmstrip)
    total_w = 960
    each_h = crop_images[0].height if crop_images else 60
    total_h = each_h * len(crop_images)

    filmstrip = Image.new("RGB", (total_w, total_h), color=(0, 0, 0))
    draw = ImageDraw.Draw(filmstrip)

    for idx, img in enumerate(crop_images):
        y_pos = idx * each_h
        filmstrip.paste(img, (0, y_pos))
        # Đánh số thứ tự [0], [1], [2]... rõ ràng ở góc trái
        draw.text((10, y_pos + 5), f"[{idx}]", fill=(255, 255, 0))

    filmstrip_path = "all_subtitles_filmstrip.jpg"
    filmstrip.save(filmstrip_path, quality=85)
    shutil.rmtree("sub_crops", ignore_errors=True)
    return filmstrip_path

# ==================== BƯỚC 4: OCR & DỊCH TỪNG ẢNH ====================
def ocr_and_translate_filmstrip(filmstrip_path: str, num_items: int, model_choice: str = "gemini-3.7-flash"):
    print(f"[*] Đang gửi cuộn ảnh chứa {num_items} dòng phụ đề cho Gemini OCR & dịch (Chỉ 1 lần gọi)...")
    with open(filmstrip_path, "rb") as f:
        img_bytes = f.read()

    prompt = f"""
    Trong ảnh này có đúng {num_items} thanh chữ phụ đề tiếng Trung được đánh số thứ tự từ [0] đến [{num_items-1}].
    Nhiệm vụ:
    1. Nhìn vào từng thanh chữ, nhận diện chữ tiếng Trung (OCR) và dịch nghĩa sang tiếng Việt tự nhiên theo phong cách hoạt hình.
    2. Nếu một thanh nào đó không có chữ hoặc chỉ có hình nền mờ, hãy để chuỗi rỗng "".
    3. Trả về đúng một mảng JSON gồm {num_items} phần tử chuỗi tiếng Việt theo đúng thứ tự từ [0] đến [{num_items-1}].
    """

    config = types.GenerateContentConfig(
        temperature=0.0,
        response_mime_type="application/json",
        response_schema={
            "type": "ARRAY",
            "items": {"type": "STRING"}
        }
    )

    target_m = model_choice.split(" ")[0].strip()
    res = client.models.generate_content(
        model=target_m,
        contents=[types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"), prompt],
        config=config
    )

    if os.path.exists(filmstrip_path): os.remove(filmstrip_path)
    translations = json.loads(res.text)
    print(f"[✓] Đã dịch xong {len(translations)} dòng phụ đề độc lập!")
    return translations

# ==================== BƯỚC 5: TẠO FILE SRT CHUẨN 1:1 ====================
def build_strict_1to1_srt(intervals, translations, output_srt: str = "final_perfect.srt"):
    count = min(len(intervals), len(translations))
    srt_lines = []

    valid_idx = 1
    for i in range(count):
        txt = translations[i].strip().replace('"', '').replace('\n', ' ')
        if not txt: continue

        s_str = sec_to_time_str(intervals[i]["start"])
        e_str = sec_to_time_str(intervals[i]["end"])

        srt_lines.append(f"{valid_idx}\n{s_str} --> {e_str}\n{txt}")
        valid_idx += 1

    final_text = "\n\n".join(srt_lines)
    with open(output_srt, "w", encoding="utf-8") as f:
        f.write(final_text)

    print(f"[✓] Đã tạo file SRT hoàn hảo với {valid_idx - 1} entry (Tuyệt đối không gộp)!")
    return output_srt

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

def generate_voice_file(text: str, voice_type: str, out_p: str) -> bool:
    import edge_tts
    if "CapCut" in voice_type and capcut_client:
        v_id = "BV421_vivn_streaming"
        if "BV422" in voice_type: v_id = "BV422_vivn_streaming"
        elif "BV701" in voice_type: v_id = "BV701_streaming"
        for _ in range(3):
            try:
                res = capcut_client.generate_speech(text, voice=v_id, timeout=30)
                capcut_client.download_tts_audio(res, out_p)
                if os.path.exists(out_p) and os.path.getsize(out_p) > 0: return True
            except: time.sleep(1)

    v_code, pitch, rate = "vi-VN-HoaiMyNeural", "+16Hz", "+8%"
    if "Nam Minh" in voice_type: v_code, pitch, rate = "vi-VN-NamMinhNeural", "-2Hz", "+6%"
    try:
        asyncio.run(edge_tts.Communicate(text, v_code, pitch=pitch, rate=rate).save(out_p))
        return os.path.exists(out_p) and os.path.getsize(out_p) > 0
    except: return False

def build_synced_tts_track(srt_file: str, voice_type: str, output_audio: str = "full_tts.wav"):
    items = parse_srt(srt_file)
    inputs, parts = [], []
    os.makedirs("temp_tts", exist_ok=True)
    idx = 0
    for start_sec, end_sec, text in items:
        if not text or text.startswith("["): continue
        raw_p = f"temp_tts/raw_{idx}.mp3"
        fit_p = f"temp_tts/fit_{idx}.mp3"
        try:
            if generate_voice_file(text, voice_type, raw_p):
                audio_dur = get_audio_duration(raw_p)
                max_allowed_dur = max(0.4, end_sec - start_sec - 0.05)
                if audio_dur > max_allowed_dur:
                    speed_factor = min(1.90, audio_dur / max_allowed_dur)
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

def render_final_video_auto(input_video, srt_file, tts_audio, output_final, sub_name, y1_pct, y2_pct, sub_fontsize=16, font_family="Comfortaa"):
    w, h = get_video_dimensions(input_video)

    y_start = (int(h * y1_pct) // 2) * 2
    crop_h = (int(h * (y2_pct - y1_pct)) // 2) * 2
    crop_h = max(32, min(crop_h, h - y_start))

    # Căn chữ phụ đề Việt nằm chuẩn tâm vào chính giữa toạ độ vệt mờ vừa dò
    y_center_pct = (y1_pct + y2_pct) / 2.0
    center_margin_v = max(4, int((1.0 - y_center_pct) * 288 - (int(sub_fontsize) / 4)))

    t_sub = sub_name.replace("'", "").replace(":", " -")
    srt_p = os.path.abspath(srt_file).replace("\\", "/")

    items = parse_srt(srt_file)
    en_list = [f"between(t,{max(0, s-0.08):.2f},{e+0.18:.2f})" for s, e, t in items if t]
    en_str = "+".join(en_list) if en_list else "1"

    font_name = "Comfortaa"
    if "Roboto" in font_family: font_name = "Roboto"
    font_org_cmd = f":fontfile='{CJK_FONT}'" if os.path.exists(CJK_FONT) else ""

    vf = (
        f"[0:v]split[m][c];"
        f"[c]crop=iw:{crop_h}:0:{y_start},gblur=sigma=26[b];"
        f"[m][b]overlay=0:{y_start}:enable='{en_str}'[v1];"
        f"[v1]drawtext=text='sub - {t_sub}':x=25:y=25{font_org_cmd}:fontcolor=white:shadowcolor=black:shadowx=1:shadowy=1,"
        f"drawtext=text='{t_sub}':x='mod(t*65,w+tw)-tw':y='mod(t*32,h+th)-th':fontsize=20:fontcolor=white@0.10,"
        f"subtitles=filename='{srt_p}':force_style='FontName={font_name},FontSize={int(sub_fontsize)},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.0,Alignment=2,MarginV={center_margin_v}'[vout]"
    )

    if tts_audio and os.path.exists(tts_audio):
        af = "[0:a]volume=0.15[bg];[1:a]volume=1.0[voice];[bg][voice]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        cmd = ["ffmpeg", "-y", "-i", input_video, "-i", tts_audio, "-filter_complex", f"{vf};{af}", "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", output_final]
    else:
        cmd = ["ffmpeg", "-y", "-i", input_video, "-filter_complex", vf, "-map", "[vout]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-c:a", "copy", output_final]

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_final

def clean_trash():
    shutil.rmtree("temp_tts", ignore_errors=True)
    shutil.rmtree("sub_crops", ignore_errors=True)
    for f in ["temp_sub.srt", "full_tts.wav", "sub_detect_sample.jpg", "audio_16k.wav", "all_subtitles_filmstrip.jpg"]:
        if os.path.exists(f): 
            try: os.remove(f)
            except: pass

def main():
    input_dir = "input_videos"
    output_dir = "output_videos"
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    raw_files = []
    for ext in ("*.mp4", "*.mov", "*.mkv", "*.webm", "*.MP4", "*.MOV", "*.MKV"):
        raw_files.extend(glob.glob(os.path.join(input_dir, ext)))

    video_files = [f for f in raw_files if "_Vietsub" not in os.path.basename(f)]

    if not video_files:
        print("[!] Không tìm thấy video gốc nào cần xử lý!")
        return

    print(f"🚀 Bắt đầu quy trình tự động hoàn hảo: Audio Timestamps + Filmstrip OCR cho {len(video_files)} video...")

    for idx, video_path in enumerate(video_files, 1):
        clean_trash()
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        out_name = os.path.join(output_dir, f"{base_name}_Vietsub.mp4")
        print(f"\n=======================================================")
        print(f"▶️ [{idx}/{len(video_files)}] Đang làm video: {base_name}")
        print(f"=======================================================")

        try:
            # 1. Tự động dò toạ độ thanh chữ của bộ phim này
            y1, y2 = detect_sub_zone_dynamic(video_path, CONFIG["models"][0])

            # 2. Nghe âm thanh 16kHz để lấy các mốc câu nói (Không bị 1 FPS)
            intervals = get_audio_speech_intervals(video_path, CONFIG["models"][0])

            # 3. Chụp từng thanh phụ đề tại các mốc đó & ghép thành Filmstrip
            filmstrip_path = build_composite_filmstrip(video_path, intervals, y1, y2)

            # 4. OCR & Dịch từng ảnh trong Filmstrip (Chính xác 100% không gộp)
            translations = ocr_and_translate_filmstrip(filmstrip_path, len(intervals), CONFIG["models"][0])

            # 5. Xuất file SRT chuẩn 1:1
            srt_p = build_strict_1to1_srt(intervals, translations, "final_sub.srt")

            # 6. Tạo giọng đọc CapCut
            tts_p = build_synced_tts_track(srt_p, CONFIG["voice"])

            # 7. Render hoàn tất
            render_final_video_auto(video_path, srt_p, tts_p, out_name, CHANNEL_SUB_NAME, y1, y2)
            print(f"[✓] HOÀN TẤT XUẤT SẮC: {out_name}")
        except Exception as e:
            print(f"[❌] Lỗi khi xử lý {base_name}: {e}")
            print("⏩ Nhảy sang video tiếp theo...")
        clean_trash()

    print("\n🎉 ĐÃ XỬ LÝ XONG TOÀN BỘ VIDEO!")

if __name__ == "__main__":
    main()* 2
    crop_h = max(32, min(crop_h, h - y_start))

    y_center_pct = (y1_safe + y2_safe) / 2.0
    center_margin_v = max(4, int((1.0 - y_center_pct) * 288 - (int(sub_fontsize) / 4)))

    t_sub = sub_name.replace("'", "").replace(":", " -")
    srt_p = os.path.abspath(srt_file).replace("\\", "/")

    items = parse_srt(srt_file)
    en_list = [f"between(t,{max(0, s-0.08):.2f},{e+0.18:.2f})" for s, e, t in items if t]
    en_str = "+".join(en_list) if en_list else "1"

    font_name = "Comfortaa"
    if "Roboto" in font_family: font_name = "Roboto"
    font_org_cmd = f":fontfile='{CJK_FONT}'" if os.path.exists(CJK_FONT) else ""

    vf = (
        f"[0:v]split[m][c];"
        f"[c]crop=iw:{crop_h}:0:{y_start},gblur=sigma=22[b];"
        f"[m][b]overlay=0:{y_start}:enable='{en_str}'[v1];"
        f"[v1]drawtext=text='sub - {t_sub}':x=25:y=25{font_org_cmd}:fontcolor=white:shadowcolor=black:shadowx=1:shadowy=1,"
        f"drawtext=text='{t_sub}':x='mod(t*65,w+tw)-tw':y='mod(t*32,h+th)-th':fontsize=20:fontcolor=white@0.10,"
        f"subtitles=filename='{srt_p}':force_style='FontName={font_name},FontSize={int(sub_fontsize)},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.0,Alignment=2,MarginV={center_margin_v}'[vout]"
    )

    if tts_audio and os.path.exists(tts_audio):
        af = "[0:a]volume=0.15[bg];[1:a]volume=1.0[voice];[bg][voice]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        cmd = ["ffmpeg", "-y", "-i", input_video, "-i", tts_audio, "-filter_complex", f"{vf};{af}", "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", output_final]
    else:
        cmd = ["ffmpeg", "-y", "-i", input_video, "-filter_complex", vf, "-map", "[vout]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-c:a", "copy", output_final]

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_final

def clean_trash():
    shutil.rmtree("temp_tts", ignore_errors=True)
    for f in ["upload_for_gemini.mp4", "temp_sub.srt", "full_tts.wav", "sub_sample.jpg"]:
        if os.path.exists(f): 
            try: os.remove(f)
            except: pass

def main():
    input_dir = "input_videos"
    output_dir = "output_videos"
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    raw_files = []
    for ext in ("*.mp4", "*.mov", "*.mkv", "*.webm", "*.MP4", "*.MOV", "*.MKV"):
        raw_files.extend(glob.glob(os.path.join(input_dir, ext)))

    video_files = [f for f in raw_files if "_Vietsub" not in os.path.basename(f)]

    if not video_files:
        print("[!] Không tìm thấy video gốc nào cần xử lý!")
        return

    print(f"🚀 Tìm thấy tổng cộng {len(video_files)} video. Bắt đầu xử lý...")

    for idx, video_path in enumerate(video_files, 1):
        clean_trash()
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        out_name = os.path.join(output_dir, f"{base_name}_Vietsub.mp4")
        print(f"\n=======================================================")
        print(f"▶️ [{idx}/{len(video_files)}] Đang làm video: {base_name}")
        print(f"=======================================================")

        try:
            # 1. Bóc sub trực tiếp từ video gốc chuẩn xác
            srt_p, first_sec = get_srt_from_gemini_auto(video_path)

            # 2. Dò toạ độ khung mờ
            y1, y2 = detect_sub_zone_from_frame(video_path, first_sec, CONFIG["models"][0])

            # 3. Tạo giọng đọc CapCut
            tts_p = build_synced_tts_track(srt_p, CONFIG["voice"])

            # 4. Render video hoàn chỉnh
            render_final_video_auto(video_path, srt_p, tts_p, out_name, CHANNEL_SUB_NAME, y1, y2)
            print(f"[✓] HOÀN TẤT: {out_name}")
        except Exception as e:
            print(f"[❌] Lỗi khi xử lý {base_name}: {e}")
            print("⏩ Nhảy sang video tiếp theo...")
        clean_trash()

    print("\n🎉 ĐÃ XỬ LÝ XONG TẤT CẢ VIDEO!")

if __name__ == "__main__":
    main()
        except: pass

    if idx > 0:
        chain = "".join([f"[a{i}]" for i in range(idx)])
        fc = "".join(parts) + f"{chain}amix=inputs={idx}:dropout_transition=0:normalize=0[out]"
        subprocess.run(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[out]", output_audio], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_audio
    return None

def render_final_video_auto(input_video, srt_file, tts_audio, output_final, sub_name, y1_pct, y2_pct, sub_fontsize=16, font_family="Comfortaa"):
    w, h = get_video_dimensions(input_video)
    y_start = (int(h * y1_pct) // 2) * 2
    crop_h = (int(h * (y2_pct - y1_pct)) // 2) * 2
    crop_h = max(36, min(crop_h, h - y_start))

    y_center_pct = (y1_pct + y2_pct) / 2.0
    center_margin_v = max(4, int((1.0 - y_center_pct) * 288 - (int(sub_fontsize) / 4)))

    t_sub = sub_name.replace("'", "").replace(":", " -")
    srt_p = os.path.abspath(srt_file).replace("\\", "/")

    items = parse_srt(srt_file)
    en_list = [f"between(t,{max(0, s-0.08):.2f},{e+0.18:.2f})" for s, e, t in items if t]
    en_str = "+".join(en_list) if en_list else "1"

    font_name = "Comfortaa"
    if "Roboto" in font_family: font_name = "Roboto"
    font_org_cmd = f":fontfile='{CJK_FONT}'" if os.path.exists(CJK_FONT) else ""

    vf = (
        f"[0:v]split[m][c];"
        f"[c]crop=iw:{crop_h}:0:{y_start},gblur=sigma=26[b];"
        f"[m][b]overlay=0:{y_start}:enable='{en_str}'[v1];"
        f"[v1]drawtext=text='sub - {t_sub}':x=25:y=25{font_org_cmd}:fontcolor=white:shadowcolor=black:shadowx=1:shadowy=1,"
        f"drawtext=text='{t_sub}':x='mod(t*65,w+tw)-tw':y='mod(t*32,h+th)-th':fontsize=20:fontcolor=white@0.10,"
        f"subtitles=filename='{srt_p}':force_style='FontName={font_name},FontSize={int(sub_fontsize)},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.0,Alignment=2,MarginV={center_margin_v}'[vout]"
    )

    if tts_audio and os.path.exists(tts_audio):
        af = "[0:a]volume=0.15[bg];[1:a]volume=1.0[voice];[bg][voice]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        cmd = ["ffmpeg", "-y", "-i", input_video, "-i", tts_audio, "-filter_complex", f"{vf};{af}", "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", output_final]
    else:
        cmd = ["ffmpeg", "-y", "-i", input_video, "-filter_complex", vf, "-map", "[vout]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-c:a", "copy", output_final]

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_final

def clean_trash():
    shutil.rmtree("temp_tts", ignore_errors=True)
    for f in ["upload_for_gemini.mp4", "temp_sub.srt", "full_tts.wav", "sub_sample.jpg", "cfr_standard.mp4"]:
        if os.path.exists(f): 
            try: os.remove(f)
            except: pass

def main():
    input_dir = "input_videos"
    output_dir = "output_videos"
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    raw_files = []
    for ext in ("*.mp4", "*.mov", "*.mkv", "*.webm", "*.MP4", "*.MOV", "*.MKV"):
        raw_files.extend(glob.glob(os.path.join(input_dir, ext)))

    video_files = [f for f in raw_files if "_Vietsub" not in os.path.basename(f)]

    if not video_files:
        print("[!] Không tìm thấy video gốc nào cần xử lý!")
        return

    print(f"🚀 Tìm thấy tổng cộng {len(video_files)} video. Bắt đầu xử lý...")

    for idx, video_path in enumerate(video_files, 1):
        clean_trash()
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        out_name = os.path.join(output_dir, f"{base_name}_Vietsub.mp4")
        print(f"\n=======================================================")
        print(f"▶️ [{idx}/{len(video_files)}] Đang làm video: {base_name}")
        print(f"=======================================================")

        try:
            # 1. Chuẩn hoá CFR 30fps
            cfr_video = convert_to_cfr_30fps(video_path)

            # 2. Bóc sub 1 lần duy nhất toàn bộ video chuẩn tự nhiên AI Studio
            srt_p, first_sec = get_srt_from_gemini_natural(cfr_video)

            # 3. Dò toạ độ khung mờ
            y1, y2 = detect_sub_zone_from_frame(cfr_video, first_sec, CONFIG["models"][0])

            # 4. Tạo giọng đọc CapCut
            tts_p = build_synced_tts_track(srt_p, CONFIG["voice"])

            # 5. Render video hoàn chỉnh
            render_final_video_auto(cfr_video, srt_p, tts_p, out_name, CHANNEL_SUB_NAME, y1, y2)
            print(f"[✓] HOÀN TẤT: {out_name}")
        except Exception as e:
            print(f"[❌] Lỗi khi xử lý {base_name}: {e}")
            print("⏩ Nhảy sang video tiếp theo...")
        clean_trash()

    print("\n🎉 ĐÃ XỬ LÝ XONG TẤT CẢ VIDEO!")

if __name__ == "__main__":
    main()
