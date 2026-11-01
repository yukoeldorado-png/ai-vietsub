import os, re, time, json, asyncio, subprocess, requests, shutil, glob
from google import genai
from google.genai import types

# 1. CẤU HÌNH
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
if not GEMINI_KEY:
    raise Exception("Chưa cài GEMINI_API_KEY trong GitHub Secrets!")

client = genai.Client(api_key=GEMINI_KEY)

CHANNEL_SUB_NAME = "IYaN AI"

CONFIG = {
    "models": ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.8-flash"],
    "voice": "CapCut - Nguồn Nhỏ Ngọt Ngào (BV421)",
    "font_family": "Comfortaa",
    "font_size": 16,
    "default_y1": 0.86,
    "default_y2": 0.98
}

# GIỮ NGUYÊN PROMPT GỐC CỰC CHUẨN CỦA BẠN + THÊM QUY TẮC CẤM GỘP KHI CHỮ NHẢY
YAN_BASE_INSTRUCTIONS = """
Persona: YaN - Chuyên gia Biên dịch ngôn ngữ & Bản địa hóa phim hoạt hình Trung Quốc.
TỐI HẬU THƯ:
1. Mỗi entry phụ đề BẮT BUỘC chỉ được nằm trên DUY NHẤT 1 DÒNG ĐƠN (tối đa 10-12 từ).
2. TUYỆT ĐỐI KHÔNG GỘP CÂU: Mỗi khi dòng phụ đề tiếng Trung ở đáy màn hình nhảy chữ mới (dù là câu ngắn 1-2 từ hay nhảy liên tiếp dồn dập), BẮT BUỘC tạo 1 entry riêng biệt khớp theo thời điểm chữ xuất hiện.
3. Định dạng bắt buộc: 00:mm:ss,mmm --> 00:mm:ss,mmm (LUÔN CÓ ĐỦ 00: Ở CẢ 2 ĐẦU).
4. Tuyệt đối cách nhau bởi 1 dòng trống. CẤM DẤU NGOẶC KÉP ("). Không xuất lời dẫn.
"""

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

def clean_timestamp_str(t: str) -> str:
    t = t.strip().replace('.', ',')
    parts = t.split(':')
    if len(parts) == 2: return f"00:{int(parts[0]):02d}:{parts[1]}"
    elif len(parts) == 3: return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{parts[2]}"
    return t

def rebuild_pure_srt(raw_text: str) -> str:
    pattern = r'(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}|\d{1,2}:\d{2}[,\.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}|\d{1,2}:\d{2}[,\.]\d{1,3})'
    matches = list(re.finditer(pattern, raw_text))
    if not matches: return raw_text
    entries = []
    for i in range(len(matches)):
        st = clean_timestamp_str(matches[i].group(1))
        et = clean_timestamp_str(matches[i].group(2))
        p1 = matches[i].end()
        p2 = matches[i+1].start() if i + 1 < len(matches) else len(raw_text)
        lines = [l.strip() for l in raw_text[p1:p2].split('\n') if l.strip()]
        valid = [l for l in lines if not re.match(r'^\d+$', l) and '-->' not in l]
        txt = " ".join(valid).replace('"', '').strip()
        if txt: entries.append((st, et, txt))
    return "\n\n".join([f"{idx}\n{s} --> {e}\n{t}" for idx, (s, e, t) in enumerate(entries, 1)])

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
    safe_video_path = "upload_for_gemini.mp4"
    shutil.copyfile(video_path, safe_video_path)
    v_file = client.files.upload(file=safe_video_path)
    while v_file.state.name == "PROCESSING":
        time.sleep(2)
        v_file = client.files.get(name=v_file.name)
    if v_file.state.name != "ACTIVE": raise Exception("Gemini không đọc được video.")

    config = types.GenerateContentConfig(system_instruction=YAN_BASE_INSTRUCTIONS, temperature=0.1)
    resp = None
    prompt_req = "Xuất SRT chuẩn theo quy tắc, bám sát mốc thời gian từng lần chữ Trung nhảy lên màn hình."

    for model_name in model_list:
        print(f"[*] Bóc sub với model: {model_name}...")
        for attempt in range(1, 4):
            try:
                r = client.models.generate_content(model=model_name, contents=[v_file, prompt_req], config=config)
                if r and r.text:
                    resp = r; print(f"[✓] Bóc sub thành công với {model_name}!"); break
            except Exception as e:
                print(f"[!] Thử lại model {model_name} ({e})")
                time.sleep(3)
        if resp: break

    try: client.files.delete(name=v_file.name)
    except: pass

    if not resp or not resp.text: raise Exception("Model Gemini quá tải!")
    pure_srt = rebuild_pure_srt(resp.text)
    with open(output_srt, "w", encoding="utf-8") as f: f.write(pure_srt)

    first_time_match = re.search(r'00:(\d{2}):(\d{2}),(\d{3})', pure_srt)
    first_sec = 2.0
    if first_time_match:
        first_sec = int(first_time_match.group(1)) * 60 + int(first_time_match.group(2)) + 0.5
    return output_srt, first_sec

def detect_sub_zone_from_frame(video_path: str, timestamp_sec: float = 2.0, model_choice: str = "gemini-3.7-flash"):
    img_path = "sub_sample.jpg"
    subprocess.run(["ffmpeg", "-y", "-ss", str(timestamp_sec), "-i", video_path, "-vframes", "1", img_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(img_path): return CONFIG["default_y1"], CONFIG["default_y2"]
    try:
        with open(img_path, "rb") as f: img_bytes = f.read()
        res = client.models.generate_content(
            model=model_choice,
            contents=[types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"), "Hãy nhìn vào dòng chữ phụ đề tiếng Trung chạy dưới đáy ảnh. Báo chính xác 2 số phần trăm: y_start, y_end tính từ đỉnh ảnh xuống."]
        ).text
        nums = re.findall(r'\d+', res)
        if len(nums) >= 2:
            y1, y2 = int(nums[0]) / 100.0, int(nums[1]) / 100.0
            y1, y2 = min(y1, y2), max(y1, y2)
            if 0.70 <= y1 < y2 <= 1.0: return y1, y2
    except: pass
    return CONFIG["default_y1"], CONFIG["default_y2"]

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

# TỰ ĐỘNG MỞ RỘNG VỆT MỜ THÊM ĐỂ CHE KÍN 100% VIỀN CHỮ GỐC
def render_final_video_auto(input_video, srt_file, tts_audio, output_final, sub_name, y1_pct, y2_pct, sub_fontsize=16, font_family="Comfortaa"):
    w, h = get_video_dimensions(input_video)

    # Nới rộng an toàn: lấn lên trên 2.5% và xuống dưới 1.5%
    y1_safe = max(0.68, y1_pct - 0.025)
    y2_safe = min(1.0, y2_pct + 0.015)

    y_start = (int(h * y1_safe) // 2) * 2
    crop_h = (int(h * (y2_safe - y1_safe)) // 2) * 2
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
