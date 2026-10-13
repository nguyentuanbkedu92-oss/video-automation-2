import os
import re
import sys
import json
import time
import random
import unicodedata
import asyncio
import subprocess
import gspread
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import edge_tts

# =====================================================================
# CÔNG TẮC BẬT / TẮT GIỌNG ĐỌC  (đổi True <-> False rồi commit là xong)
# Thứ tự ưu tiên: edge-tts -> VieNeu -> Piper (nguồn nào hỏng hoặc tắt thì tự bỏ qua)
# =====================================================================
BAT_EDGE_TTS = True    # giọng Microsoft edge-tts (phụ đề khớp từng từ). Hiện bị lỗi 403 trên GitHub, khi hết chặn sẽ tự dùng lại
BAT_VIENEU = True      # VieNeu-TTS (miễn phí, chạy CPU)
BAT_PIPER = True       # Piper (miễn phí, offline, dự phòng cuối)

# ==== CẤU HÌNH ====
SHEET_ID = "1x5onnS9fZWTm18lDGOLFDDSGQCjC09uCSVBZYTtxmIU"
DRIVE_OUTPUT_FOLDER_ID = "1V27dj-ws6K3xQEtim-P_PhEfhsXgkJ3I"
NGUON_VIDEO_NEN_ROOT_ID = "1q8dWz0BvylzeN8hD5AyeX0_2Rs-Zmfrm"

VOICE = "vi-VN-NamMinhNeural"
GIONG_DU_PHONG_EDGE = "vi-VN-HoaiMyNeural"
VIENEU_VOICE = "Hải Đăng"   # nam, Bắc (muốn giọng nữ đổi thành "Trúc Ly")
VIENEU_PRECISION = os.environ.get("VIENEU_PRECISION", "fp32")
PIPER_MODEL = "piper_models/vi.onnx"
SO_LAN_THU_TTS = 3
GIAN_CACH_TTS_GIAY = 2   # nghỉ ngắn trước mỗi lần gọi edge-tts cho đỡ bị giới hạn tần suất
LOGO_PATH = "logo.png"

_vieneu = None
VIENEU_HONG = False
EDGE_HONG = False   # sau khi edge-tts hỏng 1 video, bỏ qua edge-tts cho các video còn lại

# Dùng cho khung DỌC (Shorts) - 2 dòng tách riêng, giống file Shorts gốc
TEXT_TEN_SHOP = "Thành Đạt Led"
TEXT_SDT = "0986474671 -  0867933396"

# Dùng cho khung NGANG - 1 dòng gộp chung, giống file ngang gốc
TEXT_LIEN_HE = "Thành Đạt Led - 0986474671 -  0867933396"

SO_VIDEO_NEN_MOI_LAN = (2, 3)
SO_TU_MOI_CUM_PHU_DE = 11  # số từ mỗi cụm phụ đề hiện ra 1 lần
THOI_GIAN_HIEN_TOI_THIEU_GIAY = 1.8  # mỗi cụm hiện tối thiểu bao lâu, dù đọc nhanh

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

sa_creds = SACredentials.from_service_account_file("service_account.json", scopes=SCOPES)
gc = gspread.authorize(sa_creds)
drive_read = build("drive", "v3", credentials=sa_creds)

oauth_creds = OAuthCredentials(
    token=None,
    refresh_token=os.environ["OAUTH_REFRESH_TOKEN"],
    client_id=os.environ["OAUTH_CLIENT_ID"],
    client_secret=os.environ["OAUTH_CLIENT_SECRET"],
    token_uri="https://oauth2.googleapis.com/token",
    scopes=["https://www.googleapis.com/auth/drive"],
)
drive_upload = build("drive", "v3", credentials=oauth_creds)

# Trỏ chính xác vào tab 06_VIDEO_QUEUE của Google Sheet
sheet = gc.open_by_key(SHEET_ID).worksheet("06_VIDEO_QUEUE")


# ================== TIỆN ÍCH ==================

class TTSBlocked(Exception):
    """Mọi nguồn giọng đọc đều không tạo được âm thanh."""


def slugify_vi(text):
    text = text.lower().replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "video"


def get_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True
    )
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def tim_folder_theo_ten_chinh_xac(ten, parent_id, service):
    ten_escaped = ten.replace("'", "\\'")
    query = (
        f"name = '{ten_escaped}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def tim_hoac_tao_thu_muc(ten, parent_id, service):
    existing = tim_folder_theo_ten_chinh_xac(ten, parent_id, service)
    if existing:
        return existing
    metadata = {
        "name": ten,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder.get("id")


def lay_danh_sach_video(folder_id):
    results = drive_read.files().list(
        q=f"'{folder_id}' in parents and mimeType contains 'video/' and trashed = false",
        fields="files(id, name)"
    ).execute()
    return results.get("files", [])


def tai_video_ve(file_id, out_path):
    request = drive_read.files().get_media(fileId=file_id)
    with open(out_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()


# ================== KHUNG HÌNH: NGANG / DỌC THEO Video_Type ==================

def lay_kich_thuoc_theo_loai(video_type):
    """
    'Dài' (hoặc giá trị khác/trống) -> ngang 1280x720 (16:9)
    'Ngắn' -> dọc 720x1280 (9:16), hợp Shorts/TikTok/Reels
    So sánh không phân biệt hoa/thường, chấp nhận cả bản không dấu.
    """
    vt = (video_type or "").strip().lower()
    if vt in ("ngắn", "ngan"):
        return 720, 1280   # dọc
    return 1280, 720       # ngang (mặc định)


# ================== GIỌNG ĐỌC (edge-tts -> VieNeu -> Piper) ==================

def giay_sang_ass_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int((sec - int(sec)) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def uoc_luong_tu(text, audio_path):
    """Không có mốc thời gian từng từ -> chia đều theo độ dài chữ trên tổng thời lượng audio."""
    dur = get_duration(audio_path)
    tu = text.split()
    if not tu:
        return []
    trong = [len(t) + 1 for t in tu]
    tong, t0, words = sum(trong), 0.0, []
    for t, w in zip(tu, trong):
        d = dur * w / tong
        words.append((t0, t0 + d, t))
        t0 += d
    return words


async def _edge_tts(text, audio_path, voice):
    communicate = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    words = []
    with open(audio_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start_sec = chunk["offset"] / 10_000_000
                dur_sec = chunk["duration"] / 10_000_000
                words.append((start_sec, start_sec + dur_sec, chunk["text"]))
    return words


def tao_giong_vieneu(text, audio_path, voice_name):
    """VieNeu-TTS (CPU/ONNX). Không có mốc từng từ -> phụ đề ước lượng theo độ dài chữ."""
    global _vieneu
    if _vieneu is None:
        from vieneu import Vieneu
        t0 = time.time()
        _vieneu = Vieneu(precision=VIENEU_PRECISION)
        print(f"[VieNeu] đã nạp model sau {time.time() - t0:.0f}s (precision={VIENEU_PRECISION})", flush=True)
    base = audio_path.rsplit(".", 1)[0]
    wav_path = base + "_vieneu.wav"
    try:
        t0 = time.time()
        audio = _vieneu.infer(text, voice=voice_name)
        _vieneu.save(audio, wav_path)
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", wav_path,
             "-ar", "44100", "-ac", "1", audio_path],
            check=True,
        )
        if os.path.getsize(audio_path) < 1000:
            raise RuntimeError("VieNeu trả audio rỗng")
        dur = get_duration(audio_path)
        print(f"[VieNeu] tạo {dur:.0f}s âm thanh mất {time.time() - t0:.0f}s (giọng {voice_name})", flush=True)
        return uoc_luong_tu(text, audio_path)
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


def tao_giong_piper(text, audio_path):
    """Piper (offline, miễn phí). Không có mốc từng từ -> phụ đề ước lượng theo độ dài chữ."""
    if not os.path.exists(PIPER_MODEL):
        raise RuntimeError("chưa có model Piper")
    base = audio_path.rsplit(".", 1)[0]
    txt_path, wav_path = base + "_piper.txt", base + "_piper.wav"
    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        r = subprocess.run(
            [sys.executable, "-m", "piper", "-m", PIPER_MODEL, "-c", PIPER_MODEL + ".json",
             "-i", txt_path, "-f", wav_path],
            capture_output=True, text=True, timeout=900,
        )
        if r.returncode != 0 or not os.path.exists(wav_path):
            raise RuntimeError(f"Piper lỗi: {(r.stderr or r.stdout)[-200:]}")
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", wav_path,
             "-ar", "44100", "-ac", "1", audio_path],
            check=True,
        )
        if os.path.getsize(audio_path) < 1000:
            raise RuntimeError("Piper trả audio rỗng")
        return uoc_luong_tu(text, audio_path)
    finally:
        for p in (txt_path, wav_path):
            if os.path.exists(p):
                os.remove(p)


def tao_giong_doc(text, audio_path):
    """Trả về danh sách (bắt đầu, kết thúc, từ). Thứ tự ưu tiên:
    1) edge-tts (phụ đề khớp từng từ). Hỏng thì đánh dấu EDGE_HONG, các video sau khỏi thử lại.
    2) VieNeu-TTS (miễn phí, CPU).  3) Piper (miễn phí, offline).  4) Không được thì TTSBlocked."""
    global EDGE_HONG, VIENEU_HONG
    co_piper = BAT_PIPER and os.path.exists(PIPER_MODEL)
    co_vieneu = BAT_VIENEU and not VIENEU_HONG
    voices = [VOICE, GIONG_DU_PHONG_EDGE]
    loi = None

    if BAT_EDGE_TTS and not EDGE_HONG:
        so_lan = 2 if (co_piper or co_vieneu) else SO_LAN_THU_TTS
        for lan in range(so_lan):
            voice = voices[lan % len(voices)]
            try:
                time.sleep(GIAN_CACH_TTS_GIAY)
                words = asyncio.run(_edge_tts(text, audio_path, voice))
                if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
                    raise RuntimeError("audio rỗng")
                if not words:
                    print("[TTS] không có mốc từ, ước lượng thời gian phụ đề.", flush=True)
                    words = uoc_luong_tu(text, audio_path)
                return words
            except Exception as e:
                loi = e
                print(f"[TTS {lan + 1}/{so_lan}] edge {voice}: {type(e).__name__}: {str(e)[:120]}", flush=True)
                if lan < so_lan - 1:
                    time.sleep(10 * (lan + 1))
        EDGE_HONG = True
        print("[TTS] edge-tts đang hỏng, các video còn lại trong lần chạy này sẽ bỏ qua edge-tts.", flush=True)

    if co_vieneu:
        try:
            return tao_giong_vieneu(text, audio_path, VIENEU_VOICE)
        except Exception as e:
            loi = e
            VIENEU_HONG = True   # lỗi 1 lần thì các video sau chuyển thẳng sang Piper
            print(f"[TTS VieNeu] {type(e).__name__}: {str(e)[:500]} -> chuyển sang Piper", flush=True)

    if co_piper:
        try:
            words = tao_giong_piper(text, audio_path)
            print("[TTS] dùng Piper (giọng offline miễn phí)", flush=True)
            return words
        except Exception as e:
            loi = e
            print(f"[TTS Piper] {type(e).__name__}: {str(e)[:200]}", flush=True)

    raise TTSBlocked(
        f"mọi nguồn giọng đọc đều lỗi (edge={BAT_EDGE_TTS}, vieneu={co_vieneu}, piper={co_piper}): {str(loi)[:120]}"
    )


# ================== PHỤ ĐỀ ĐỒNG BỘ ==================

def viet_phu_de(submaker_words, ass_path, width=1280, height=720):
    cum_list = []
    for idx in range(0, len(submaker_words), SO_TU_MOI_CUM_PHU_DE):
        nhom = submaker_words[idx: idx + SO_TU_MOI_CUM_PHU_DE]
        if not nhom:
            continue
        start = nhom[0][0]
        end = nhom[-1][1]
        noi_dung = " ".join(w[2] for w in nhom)
        cum_list.append((start, end, noi_dung))

    for idx in range(len(cum_list)):
        start, end, noi_dung = cum_list[idx]
        gioi_han = (
            cum_list[idx + 1][0] if idx + 1 < len(cum_list) else end + THOI_GIAN_HIEN_TOI_THIEU_GIAY
        )
        end_moi = min(start + THOI_GIAN_HIEN_TOI_THIEU_GIAY, gioi_han)
        end_moi = max(end_moi, end)
        cum_list[idx] = (start, end_moi, noi_dung)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,1,2,80,80,55,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dong_su_kien = []
    for start, end, noi_dung in cum_list:
        noi_dung_esc = noi_dung.replace("\n", "\\N")
        dong_su_kien.append(
            f"Dialogue: 0,{giay_sang_ass_time(start)},{giay_sang_ass_time(end)},Default,,0,0,0,,{noi_dung_esc}"
        )

    print(f"[DEBUG] {len(submaker_words)} từ, {len(cum_list)} cụm phụ đề", flush=True)

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(dong_su_kien))


# ================== GHÉP NHIỀU VIDEO NỀN, XÁO TRỘN ==================

def chuan_bi_video_nen(folder_id, audio_duration, i, width, height):
    danh_sach_goc = lay_danh_sach_video(folder_id)
    if not danh_sach_goc:
        return None

    so_luong_muon_lay = random.randint(*SO_VIDEO_NEN_MOI_LAN)
    so_luong_muon_lay = min(so_luong_muon_lay, len(danh_sach_goc))

    playlist_video_info = random.sample(danh_sach_goc, so_luong_muon_lay)
    random.shuffle(playlist_video_info)

    playlist_paths = []
    total = 0.0
    count = 0

    for video in playlist_video_info:
        local_path = f"bgsrc_{i}_{count}.mp4"
        tai_video_ve(video["id"], local_path)
        dur = get_duration(local_path)
        playlist_paths.append(local_path)
        total += dur
        count += 1

    while total < audio_duration + 2:
        video = random.choice(danh_sach_goc)
        local_path = f"bgsrc_{i}_{count}.mp4"
        tai_video_ve(video["id"], local_path)
        dur = get_duration(local_path)
        playlist_paths.append(local_path)
        total += dur
        count += 1
        if count > 40:
            break

    # QUAN TRỌNG: luôn ghép nối các clip nền ở khung NGANG GỐC (1280x720),
    # KHÔNG crop theo width/height của video cuối cùng ở bước này.
    # Lý do: nếu crop cứng về khung dọc (720x1280) ngay đây, video nền sẽ mất
    # nội dung 2 bên, và bước ghep_video() phía sau (tạo nền mờ phóng to + đặt
    # video gốc giữa khung dọc) sẽ mất tác dụng vì không còn phần "thừa" để mờ.
    # Giữ nguyên khung ngang gốc ở đây, việc chuyển sang dọc (mờ lấp, không cắt)
    # hay giữ ngang được xử lý riêng ở ghep_video() theo đúng thiết kế ban đầu.
    BASE_W, BASE_H = 1280, 720
    concat_path = f"bgconcat_{i}.mp4"
    filter_parts = []
    for j in range(len(playlist_paths)):
        filter_parts.append(
            f"[{j}:v]scale={BASE_W}:{BASE_H}:force_original_aspect_ratio=increase,"
            f"crop={BASE_W}:{BASE_H},setsar=1[v{j}]"
        )
    concat_inputs = "".join(f"[v{j}]" for j in range(len(playlist_paths)))
    filter_parts.append(f"{concat_inputs}concat=n={len(playlist_paths)}:v=1:a=0[bgv]")
    filter_complex = ";".join(filter_parts)

    cmd = ["ffmpeg", "-y"]
    for p in playlist_paths:
        cmd += ["-i", p]
    cmd += ["-filter_complex", filter_complex, "-map", "[bgv]", "-an", concat_path]
    subprocess.run(cmd, check=True)

    for p in playlist_paths:
        os.remove(p)

    return concat_path


# ================== GHÉP CUỐI: LOGO + CHỮ LIÊN HỆ + PHỤ ĐỀ + AUDIO ==================
# Kiểu logo/chữ liên hệ được chọn theo khung hình, giống hệt 2 file gốc:
#   - Khung DỌC (height > width, Shorts): nền mờ phóng to lấp khung, video gốc
#     thu nhỏ đặt giữa, logo to phía trên, 2 dòng chữ (tên shop + SĐT) tách riêng.
#   - Khung NGANG: giữ nguyên khung ngang, logo nhỏ góc phải trên, 1 dòng chữ
#     liên hệ gộp chung phía trên giữa khung.

def ghep_video(audio_path, background_video, ass_path, out_path, width, height):
    ass_path_abs = os.path.abspath(ass_path)

    if not os.path.exists(ass_path_abs):
        raise FileNotFoundError(f"Khong tim thay file phu de: {ass_path_abs}")
    with open(ass_path_abs, encoding="utf-8") as f:
        noi_dung_ass = f.read()
    if "Dialogue:" not in noi_dung_ass:
        print(f"[CANH BAO] File .ass khong co dong Dialogue nao (khong co phu de duoc tao): {ass_path_abs}")

    ass_path_escaped = ass_path_abs.replace(":", r"\:")

    if height > width:
        # ---- Khung DỌC (Shorts), giống file Shorts gốc ----
        # Kích thước logo/chữ/vị trí gốc được tính cho khung tham chiếu rộng 1080px.
        # Co giãn theo tỉ lệ width/1080 để không bị tràn khi width thực tế (720) nhỏ hơn.
        ty_le = width / 1080.0
        logo_w = max(1, round(340 * ty_le))
        logo_h = max(1, round(140 * ty_le))
        y_logo = round(55 * ty_le)
        fontsize_shop = max(10, round(40 * ty_le))
        y_shop = round(205 * ty_le)
        borderw_shop = max(1, round(3 * ty_le))
        boxborderw_shop = max(1, round(18 * ty_le))
        fontsize_sdt = max(10, round(48 * ty_le))
        y_sdt = round(300 * ty_le)
        borderw_sdt = max(1, round(3 * ty_le))
        boxborderw_sdt = max(1, round(20 * ty_le))

        filter_complex = (
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=25,eq=brightness=-0.05[bgblur];"
            f"[fgsrc]scale={width}:-2[fgvid];"
            f"[bgblur][fgvid]overlay=(W-w)/2:(H-h)/2[bg];"
            f"[1:v]scale={logo_w}:{logo_h}[logo];"
            f"[bg][logo]overlay=(W-w)/2:{y_logo}[bg2];"
            f"[bg2]drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"text='{TEXT_TEN_SHOP}':fontsize={fontsize_shop}:fontcolor=white:"
            f"borderw={borderw_shop}:bordercolor=black@0.9:"
            f"box=1:boxcolor=black@0.45:boxborderw={boxborderw_shop}:"
            f"x=(w-text_w)/2:y={y_shop}[bg2b];"
            f"[bg2b]drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"text='{TEXT_SDT}':fontsize={fontsize_sdt}:fontcolor=#FFD500:"
            f"borderw={borderw_sdt}:bordercolor=black@0.9:"
            f"box=1:boxcolor=black@0.5:boxborderw={boxborderw_sdt}:"
            f"x=(w-text_w)/2:y={y_sdt}[bg3];"
            f"[bg3]subtitles=filename='{ass_path_escaped}':"
            f"fontsdir=/usr/share/fonts/truetype/dejavu[outv]"
        )
    else:
        # ---- Khung NGANG, giống file ngang gốc ----
        filter_complex = (
            f"[0:v]scale={width}:{height}[bg];"
            f"[1:v]scale=200:-1[logo];"
            f"[bg][logo]overlay=W-w-20:20[bg2];"
            f"[bg2]drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"text='{TEXT_LIEN_HE}':fontsize=32:fontcolor=#FF8A00:"
            f"borderw=2:bordercolor=black@0.8:"
            f"box=1:boxcolor=black@0.4:boxborderw=14:"
            f"x=(w-text_w)/2:y=50[bg3];"
            f"[bg3]subtitles=filename='{ass_path_escaped}':"
            f"fontsdir=/usr/share/fonts/truetype/dejavu[outv]"
        )

    cmd = [
        "ffmpeg", "-y",
        "-i", background_video,
        "-i", LOGO_PATH,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "2:a",
        "-c:v", "libx264", "-c:a", "aac",
        "-shortest",
        out_path,
    ]
    subprocess.run(cmd, check=True)


def upload_to_drive(file_path, file_name, parent_folder_id):
    file_metadata = {"name": file_name, "parents": [parent_folder_id]}
    media = MediaFileUpload(file_path, resumable=True)
    file = drive_upload.files().create(
        body=file_metadata, media_body=media, fields="id"
    ).execute()
    file_id = file.get("id")

    drive_upload.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return f"https://drive.google.com/file/d/{file_id}/view"


# ================== MAIN ==================

def main():
    rows = sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        # Đọc đúng tên cột trạng thái thực tế trong sheet
        status = row.get("Trạng_thái_video", "").strip()

        # Chấp nhận CA "Chờ dựng" (chạy tay/lịch trực tiếp trên GitHub) LAN
        # "Đang dựng" (khi Apps Script đã tự khóa dòng trước khi gửi dispatch,
        # xem 03_VideoBuilder.gs). Neu khong, cac dong do Sheet kich hoat se
        # khong bao gio duoc main.py nhan ra vi status da bi GAS doi truoc do.
        if status not in ("Chờ dựng", "Đang dựng"):
            continue

        tieu_de = row.get("TieuDe", "").strip()
        text = row.get("NoiDung", "").strip()
        loai_den = row.get("LoaiDen", "").strip()
        video_type = row.get("Video_Type", "").strip()

        if not text or not loai_den or not tieu_de:
            continue

        width, height = lay_kich_thuoc_theo_loai(video_type)

        # Cập nhật trạng thái sang "Đang dựng" để đánh dấu đang xử lý
        sheet.update_cell(i, sheet.find("Trạng_thái_video").col, "Đang dựng")

        folder_id = tim_folder_theo_ten_chinh_xac(loai_den, NGUON_VIDEO_NEN_ROOT_ID, drive_read)
        if not folder_id:
            print(f"Dòng {i}: không tìm thấy thư mục '{loai_den}' trong Nguon Video Nen, bỏ qua.")
            sheet.update_cell(i, sheet.find("Trạng_thái_video").col, "Lỗi")
            continue

        audio_path = f"audio_{i}.mp3"
        ass_path = f"caption_{i}.ass"
        try:
            words = tao_giong_doc(text, audio_path)
        except TTSBlocked as e:
            print(f"Không có giọng đọc nào dùng được, dừng sớm: {e}", flush=True)
            if os.path.exists(audio_path):
                os.remove(audio_path)
            sys.exit(1)   # để GitHub báo ĐỎ thay vì báo xanh khi chưa dựng được video
        viet_phu_de(words, ass_path, width, height)
        audio_duration = get_duration(audio_path)

        bg_concat_path = chuan_bi_video_nen(folder_id, audio_duration, i, width, height)
        if not bg_concat_path:
            print(f"Dòng {i}: thư mục '{loai_den}' không có video, bỏ qua.")
            sheet.update_cell(i, sheet.find("Trạng_thái_video").col, "Lỗi")
            os.remove(audio_path)
            os.remove(ass_path)
            continue

        out_path = f"output_{i}.mp4"
        ghep_video(audio_path, bg_concat_path, ass_path, out_path, width, height)

        sub_folder_id = tim_hoac_tao_thu_muc(loai_den, DRIVE_OUTPUT_FOLDER_ID, drive_upload)
        ten_file = slugify_vi(tieu_de) + ".mp4"
        link = upload_to_drive(out_path, ten_file, sub_folder_id)

        # Ghi Link Drive và đổi trạng thái thành "Đã đăng"
        sheet.update_cell(i, sheet.find("Link_Drive").col, link)
        sheet.update_cell(i, sheet.find("Trạng_thái_video").col, "Đã đăng")

        for p in [audio_path, ass_path, bg_concat_path, out_path]:
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()
