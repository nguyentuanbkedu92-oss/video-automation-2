import os
import re
import json
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
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ==== CẤU HÌNH ====
SHEET_ID = "1x5onnS9fZWTm18lDGOLFDDSGQCjC09uCSVBZYTtxmIU"
DRIVE_OUTPUT_FOLDER_ID = "1V27dj-ws6K3xQEtim-P_PhEfhsXgkJ3I"
NGUON_VIDEO_NEN_ROOT_ID = "1q8dWz0BvylzeN8hD5AyeX0_2Rs-Zmfrm"

VOICE = "vi-VN-NamMinhNeural"
LOGO_PATH = "logo.png"

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


# ================== TTS + PHỤ ĐỀ ĐỒNG BỘ ==================

def giay_sang_ass_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int((sec - int(sec)) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


async def tao_audio_va_phu_de(text, audio_path, ass_path, width=1280, height=720):
    communicate = edge_tts.Communicate(text, VOICE, boundary="WordBoundary")
    submaker_words = []

    with open(audio_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start_sec = chunk["offset"] / 10_000_000
                dur_sec = chunk["duration"] / 10_000_000
                submaker_words.append((start_sec, start_sec + dur_sec, chunk["text"]))

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

    concat_path = f"bgconcat_{i}.mp4"
    filter_parts = []
    for j in range(len(playlist_paths)):
        # scale phủ kín khung (không méo hình) rồi crop phần thừa ở giữa,
        # phù hợp cả khi chuyển video ngang gốc sang khung dọc (Shorts) và ngược lại.
        filter_parts.append(
            f"[{j}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1[v{j}]"
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


# ================== TẠO ẢNH CHỮ LIÊN HỆ: GRADIENT CAM + GLOW + VIỀN ĐEN ==================

def tao_anh_chu_lien_he(width, out_path):
    """
    Tao PNG (nen trong suot) chua dong chu lien he voi hieu ung chuyen nghiep:
    - Gradient mau vang cam -> cam dam
    - Glow mem (mo) mau cam quanh chu
    - Vien den mong quanh chu de de doc tren moi nen video
    - Hop nen den mo, bo goc, phia sau chu
    Dung Pillow vi ffmpeg drawtext KHONG the ve gradient/glow that su
    (drawtext chi to duoc 1 mau phang).
    """
    font_size = max(18, int(width * 32 / 1280))
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font = ImageFont.truetype(font_path, font_size)

    tmp = Image.new("RGBA", (10, 10))
    tmp_draw = ImageDraw.Draw(tmp)
    bbox = tmp_draw.textbbox((0, 0), TEXT_LIEN_HE, font=font, stroke_width=2)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    padding_x, padding_y = int(font_size * 0.7), int(font_size * 0.4)
    box_w, box_h = text_w + padding_x * 2, text_h + padding_y * 2
    margin = max(20, font_size // 2)  # chua glow/blur khong bi cat vien

    canvas_w, canvas_h = box_w + margin * 2, box_h + margin * 2
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # 1) Hop nen den mo, bo goc
    box_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(box_layer).rounded_rectangle(
        [margin, margin, margin + box_w, margin + box_h],
        radius=box_h // 3, fill=(0, 0, 0, 110)
    )
    canvas = Image.alpha_composite(canvas, box_layer)

    text_x = margin + padding_x - bbox[0]
    text_y = margin + padding_y - bbox[1]

    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).text((text_x, text_y), TEXT_LIEN_HE, font=font, fill=255)

    # 2) Glow mem mau cam quanh chu
    glow_mask = mask.filter(ImageFilter.GaussianBlur(radius=max(3, font_size // 7)))
    glow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow_layer.paste((255, 150, 40, 200), (0, 0), glow_mask)
    canvas = Image.alpha_composite(canvas, glow_layer)

    # 3) Vien den mong (mask gian ra lam vien, chu goc de gradient de len tren)
    outline_mask = mask.filter(ImageFilter.MaxFilter(5))
    outline_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    outline_layer.paste((0, 0, 0, 255), (0, 0), outline_mask)
    canvas = Image.alpha_composite(canvas, outline_layer)

    # 4) Chu gradient: vang cam nhat (trai) -> cam dam (phai)
    gradient = Image.new("RGB", canvas.size)
    grad_draw = ImageDraw.Draw(gradient)
    left_color, right_color = (255, 205, 70), (255, 90, 0)
    for x in range(canvas.size[0]):
        t = x / max(1, canvas.size[0] - 1)
        r = int(left_color[0] + (right_color[0] - left_color[0]) * t)
        g = int(left_color[1] + (right_color[1] - left_color[1]) * t)
        b = int(left_color[2] + (right_color[2] - left_color[2]) * t)
        grad_draw.line([(x, 0), (x, canvas.size[1])], fill=(r, g, b))

    text_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    text_layer.paste(gradient.convert("RGBA"), (0, 0), mask)
    canvas = Image.alpha_composite(canvas, text_layer)

    canvas.save(out_path, "PNG")


# ================== GHÉP CUỐI: LOGO + CHỮ LIÊN HỆ + PHỤ ĐỀ + AUDIO ==================

def ghep_video(audio_path, background_video, ass_path, out_path, width, height):
    ass_path_abs = os.path.abspath(ass_path)
    ass_path_escaped = ass_path_abs.replace(":", r"\:")

    # Logo co giãn theo khung ngang/dọc
    logo_scale = "140:-1" if height > width else "200:-1"

    # Tao anh chu lien he (gradient + glow + vien) bang Pillow, roi ghep len
    # video bang overlay filter thay vi drawtext (drawtext khong lam duoc gradient).
    contact_img_path = out_path.rsplit(".", 1)[0] + "_contact.png"
    tao_anh_chu_lien_he(width, contact_img_path)

    filter_complex = (
        f"[0:v]scale={width}:{height}[bg];"
        f"[1:v]scale={logo_scale}[logo];"
        f"[bg][logo]overlay=W-w-20:20[bg2];"
        f"[bg2][3:v]overlay=(W-w)/2:50[bg3];"
        f"[bg3]subtitles=filename='{ass_path_escaped}':"
        f"fontsdir=/usr/share/fonts/truetype/dejavu[outv]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", background_video,
        "-i", LOGO_PATH,
        "-i", audio_path,
        "-i", contact_img_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "2:a",
        "-c:v", "libx264", "-c:a", "aac",
        "-shortest",
        out_path,
    ]
    subprocess.run(cmd, check=True)

    if os.path.exists(contact_img_path):
        os.remove(contact_img_path)


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
        asyncio.run(tao_audio_va_phu_de(text, audio_path, ass_path, width, height))
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
