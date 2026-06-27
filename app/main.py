import os
import re
import json
import sqlite3
import smtplib
import hashlib
import requests
import shutil
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parseaddr
from urllib.parse import urlencode

from dotenv import load_dotenv
from authlib.integrations.starlette_client import OAuth
from openai import OpenAI

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")

# Vercel เป็น Serverless: เขียนไฟล์ได้เฉพาะ /tmp
# ใช้ /tmp/clinic.db เพื่อกัน Serverless Function crash ตอนสร้าง/แก้ไข SQLite
IS_VERCEL = bool(os.getenv("VERCEL"))
if IS_VERCEL:
    DB_PATH = "/tmp/clinic.db"
else:
    DB_PATH = os.path.join(BASE_DIR, "clinic.db")

STATIC_IMAGE_DIR = os.path.join(BASE_DIR, "app", "static", "images")
PACKAGE_IMAGE_DIR = os.path.join(STATIC_IMAGE_DIR, "packages")
PROMOTION_IMAGE_DIR = os.path.join(STATIC_IMAGE_DIR, "promotions")
REVIEW_IMAGE_DIR = os.path.join(STATIC_IMAGE_DIR, "reviews")
DOCTOR_IMAGE_DIR = os.path.join(STATIC_IMAGE_DIR, "doctors")
SERVICE_IMAGE_DIR = os.path.join(STATIC_IMAGE_DIR, "services")
CHANNEL_IMAGE_DIR = os.path.join(STATIC_IMAGE_DIR, "homecare_channel")
STATIC_VIDEO_DIR = os.path.join(BASE_DIR, "app", "static", "videos")
SERVICE_VIDEO_DIR = os.path.join(STATIC_VIDEO_DIR, "services")

# ตอนรันบน Vercel โฟลเดอร์ deployment อาจเป็น read-only
# จึงพยายามสร้างโฟลเดอร์แบบปลอดภัย ไม่ให้เว็บ crash ถ้าสร้างไม่ได้
def safe_makedirs(path: str):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        print("CREATE FOLDER SKIPPED:", path, repr(e))

for folder in [
    PACKAGE_IMAGE_DIR,
    PROMOTION_IMAGE_DIR,
    REVIEW_IMAGE_DIR,
    DOCTOR_IMAGE_DIR,
    SERVICE_IMAGE_DIR,
    CHANNEL_IMAGE_DIR,
    SERVICE_VIDEO_DIR,
]:
    safe_makedirs(folder)

load_dotenv(ENV_PATH, override=True)


def force_load_env():
    if not os.path.exists(ENV_PATH):
        print("========== ENV ERROR ==========")
        print(".env not found:", ENV_PATH)
        print("===============================")
        return

    with open(ENV_PATH, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


force_load_env()

app = FastAPI(title="Home Care Clinic")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "home-care-clinic-secret-key"),
)

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "app", "static")),
    name="static",
)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "app", "templates"))

_original_template_response = templates.TemplateResponse


def fixed_template_response(*args, **kwargs):
    if args and isinstance(args[0], str):
        template_name = args[0]
        context = args[1] if len(args) > 1 else kwargs.pop("context", {})

        if context is None:
            context = {}

        request = context.get("request")

        if request is None:
            raise RuntimeError(
                "TemplateResponse แบบเก่าต้องมี {'request': request} ใน context"
            )

        return _original_template_response(request, template_name, context, **kwargs)

    return _original_template_response(*args, **kwargs)


templates.TemplateResponse = fixed_template_response


oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def clean_email_address(value: str) -> str:
    if not value:
        return ""

    name, email = parseaddr(value)
    return email or value.strip()


def normalize_slug(value: str):
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9\-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value


def safe_filename(value: str) -> str:
    value = (value or "").lower().strip()
    value = re.sub(r"[^a-z0-9\-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "file"


def save_uploaded_image(folder_path: str, prefix: str, image_file: UploadFile | None):
    if not image_file or not image_file.filename:
        return None

    original_name = image_file.filename.lower()
    allowed = (".jpg", ".jpeg", ".png", ".webp")

    if not original_name.endswith(allowed):
        return None

    ext = os.path.splitext(original_name)[1].lower()
    safe_prefix = safe_filename(prefix)

    filename = f"{safe_prefix}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}{ext}"
    file_path = os.path.join(folder_path, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(image_file.file, buffer)

    return filename


def admin_redirect_with_message(path: str, success: str = "", error: str = ""):
    params = {}

    if success:
        params["success"] = success

    if error:
        params["error"] = error

    if params:
        return RedirectResponse(f"{path}?{urlencode(params)}", status_code=303)

    return RedirectResponse(path, status_code=303)


def get_admin_messages(request: Request):
    return {
        "success": request.query_params.get("success", ""),
        "error": request.query_params.get("error", ""),
    }


def ai_translate_5_languages(th_text: str = "", en_text: str = ""):
    th_text = (th_text or "").strip()
    en_text = (en_text or "").strip()

    empty_result = {
        "th": "",
        "en": "",
        "zh": "",
        "ja": "",
        "ko": "",
    }

    if not th_text and not en_text:
        return empty_result

    fallback = {
        "th": th_text or en_text,
        "en": en_text or th_text,
        "zh": th_text or en_text,
        "ja": th_text or en_text,
        "ko": th_text or en_text,
    }

    force_load_env()

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_TRANSLATE_MODEL", "gpt-4o-mini").strip()

    if not api_key:
        print("AI TRANSLATE SKIPPED: Missing OPENAI_API_KEY")
        return fallback

    try:
        client = OpenAI(api_key=api_key)

        prompt = f"""
You are a professional translator for an aesthetic clinic website.

Translate the content naturally and professionally into:
- Thai
- English
- Simplified Chinese
- Japanese
- Korean

Rules:
- Keep meaning suitable for a beauty clinic / aesthetic clinic.
- Keep text concise and marketing-friendly.
- Do not add new medical claims.
- Return ONLY valid JSON.
- JSON keys must be exactly: th, en, zh, ja, ko.

Input Thai:
{th_text}

Input English:
{en_text}

Return format:
{{
  "th": "...",
  "en": "...",
  "zh": "...",
  "ja": "...",
  "ko": "..."
}}
"""

        response = client.responses.create(
            model=model,
            input=prompt,
            text={
                "format": {
                    "type": "json_object"
                }
            },
        )

        raw = response.output_text.strip()
        data = json.loads(raw)

        return {
            "th": data.get("th") or fallback["th"],
            "en": data.get("en") or fallback["en"],
            "zh": data.get("zh") or fallback["zh"],
            "ja": data.get("ja") or fallback["ja"],
            "ko": data.get("ko") or fallback["ko"],
        }

    except Exception as e:
        print("AI TRANSLATE ERROR:", repr(e))
        return fallback


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            phone TEXT,
            password_hash TEXT NOT NULL,
            provider TEXT DEFAULT 'local',
            google_id TEXT,
            role TEXT DEFAULT 'USER',
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            customer_phone TEXT,
            doctor_id INTEGER,
            package_name TEXT NOT NULL,
            appointment_date TEXT NOT NULL,
            appointment_time TEXT NOT NULL,
            note TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            revenue_amount REAL DEFAULT 0,
            email_sent INTEGER DEFAULT 0,
            line_sent INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            description TEXT,
            price TEXT,
            price_amount REAL DEFAULT 0,
            badge TEXT,
            image_file TEXT,
            image_file_1 TEXT,
            image_file_2 TEXT,
            image_file_3 TEXT,
            image_file_4 TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,

            name_th TEXT,
            name_en TEXT,
            name_zh TEXT,
            name_ja TEXT,
            name_ko TEXT,

            description_th TEXT,
            description_en TEXT,
            description_zh TEXT,
            description_ja TEXT,
            description_ko TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS promotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            description TEXT,
            price TEXT,
            badge TEXT,
            image_file_1 TEXT,
            image_file_2 TEXT,
            image_file_3 TEXT,
            image_file_4 TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,

            title_th TEXT,
            title_en TEXT,
            title_zh TEXT,
            title_ja TEXT,
            title_ko TEXT,

            description_th TEXT,
            description_en TEXT,
            description_zh TEXT,
            description_ja TEXT,
            description_ko TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS homecare_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            video_url TEXT NOT NULL,
            platform TEXT DEFAULT 'other',
            cover_image_file TEXT,
            is_active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            review_text TEXT NOT NULL,
            rating INTEGER DEFAULT 5,
            image_file TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS doctors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            specialty TEXT,
            expertise TEXT,
            phone TEXT,
            image_file TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,

            name_th TEXT,
            name_en TEXT,
            name_zh TEXT,
            name_ja TEXT,
            name_ko TEXT,

            specialty_th TEXT,
            specialty_en TEXT,
            specialty_zh TEXT,
            specialty_ja TEXT,
            specialty_ko TEXT,

            expertise_th TEXT,
            expertise_en TEXT,
            expertise_zh TEXT,
            expertise_ja TEXT,
            expertise_ko TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            description TEXT,
            detail TEXT,
            image_file TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,

            title_th TEXT,
            title_en TEXT,
            title_zh TEXT,
            title_ja TEXT,
            title_ko TEXT,

            description_th TEXT,
            description_en TEXT,
            description_zh TEXT,
            description_ja TEXT,
            description_ko TEXT,

            detail_th TEXT,
            detail_en TEXT,
            detail_zh TEXT,
            detail_ja TEXT,
            detail_ko TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS service_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            media_type TEXT DEFAULT 'image',
            image_file TEXT,
            video_file TEXT,
            video_url TEXT,
            caption TEXT,
            sort_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(service_id) REFERENCES services(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS service_programs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            slug TEXT,
            short_description TEXT,
            detail TEXT,
            cover_image_file TEXT,
            sort_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(service_id) REFERENCES services(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS program_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            program_id INTEGER NOT NULL,
            media_type TEXT DEFAULT 'image',
            image_file TEXT,
            video_file TEXT,
            video_url TEXT,
            caption TEXT,
            sort_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(program_id) REFERENCES service_programs(id)
        )
        """
    )


    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS doctor_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            note TEXT,
            is_available INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(doctor_id) REFERENCES doctors(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS line_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_user_id TEXT,
            display_name TEXT,
            message_text TEXT,
            customer_name TEXT,
            phone TEXT,
            package_name TEXT,
            appointment_date TEXT,
            appointment_time TEXT,
            status TEXT DEFAULT 'NEW',
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    def add_column_if_missing(table_name: str, column_name: str, column_sql: str):
        columns = [
            row["name"]
            for row in cur.execute(f"PRAGMA table_info({table_name})").fetchall()
        ]

        if column_name not in columns:
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")

    add_column_if_missing("users", "provider", "provider TEXT DEFAULT 'local'")
    add_column_if_missing("users", "google_id", "google_id TEXT")
    add_column_if_missing("users", "role", "role TEXT DEFAULT 'USER'")

    add_column_if_missing("bookings", "customer_phone", "customer_phone TEXT")
    add_column_if_missing("bookings", "doctor_id", "doctor_id INTEGER")
    add_column_if_missing("bookings", "revenue_amount", "revenue_amount REAL DEFAULT 0")
    add_column_if_missing("bookings", "email_sent", "email_sent INTEGER DEFAULT 0")
    add_column_if_missing("bookings", "line_sent", "line_sent INTEGER DEFAULT 0")

    add_column_if_missing("packages", "price_amount", "price_amount REAL DEFAULT 0")
    add_column_if_missing("packages", "image_file", "image_file TEXT")
    add_column_if_missing("packages", "image_file_1", "image_file_1 TEXT")
    add_column_if_missing("packages", "image_file_2", "image_file_2 TEXT")
    add_column_if_missing("packages", "image_file_3", "image_file_3 TEXT")
    add_column_if_missing("packages", "image_file_4", "image_file_4 TEXT")

    add_column_if_missing("packages", "name_th", "name_th TEXT")
    add_column_if_missing("packages", "name_en", "name_en TEXT")
    add_column_if_missing("packages", "name_zh", "name_zh TEXT")
    add_column_if_missing("packages", "name_ja", "name_ja TEXT")
    add_column_if_missing("packages", "name_ko", "name_ko TEXT")
    add_column_if_missing("packages", "description_th", "description_th TEXT")
    add_column_if_missing("packages", "description_en", "description_en TEXT")
    add_column_if_missing("packages", "description_zh", "description_zh TEXT")
    add_column_if_missing("packages", "description_ja", "description_ja TEXT")
    add_column_if_missing("packages", "description_ko", "description_ko TEXT")

    add_column_if_missing("promotions", "title_th", "title_th TEXT")
    add_column_if_missing("promotions", "title_en", "title_en TEXT")
    add_column_if_missing("promotions", "title_zh", "title_zh TEXT")
    add_column_if_missing("promotions", "title_ja", "title_ja TEXT")
    add_column_if_missing("promotions", "title_ko", "title_ko TEXT")
    add_column_if_missing("promotions", "description_th", "description_th TEXT")
    add_column_if_missing("promotions", "description_en", "description_en TEXT")
    add_column_if_missing("promotions", "description_zh", "description_zh TEXT")
    add_column_if_missing("promotions", "description_ja", "description_ja TEXT")
    add_column_if_missing("promotions", "description_ko", "description_ko TEXT")

    add_column_if_missing("doctors", "specialty", "specialty TEXT")
    add_column_if_missing("doctors", "expertise", "expertise TEXT")
    add_column_if_missing("doctors", "phone", "phone TEXT")
    add_column_if_missing("doctors", "image_file", "image_file TEXT")
    add_column_if_missing("doctors", "is_active", "is_active INTEGER DEFAULT 1")

    add_column_if_missing("doctors", "name_th", "name_th TEXT")
    add_column_if_missing("doctors", "name_en", "name_en TEXT")
    add_column_if_missing("doctors", "name_zh", "name_zh TEXT")
    add_column_if_missing("doctors", "name_ja", "name_ja TEXT")
    add_column_if_missing("doctors", "name_ko", "name_ko TEXT")
    add_column_if_missing("doctors", "specialty_th", "specialty_th TEXT")
    add_column_if_missing("doctors", "specialty_en", "specialty_en TEXT")
    add_column_if_missing("doctors", "specialty_zh", "specialty_zh TEXT")
    add_column_if_missing("doctors", "specialty_ja", "specialty_ja TEXT")
    add_column_if_missing("doctors", "specialty_ko", "specialty_ko TEXT")
    add_column_if_missing("doctors", "expertise_th", "expertise_th TEXT")
    add_column_if_missing("doctors", "expertise_en", "expertise_en TEXT")
    add_column_if_missing("doctors", "expertise_zh", "expertise_zh TEXT")
    add_column_if_missing("doctors", "expertise_ja", "expertise_ja TEXT")
    add_column_if_missing("doctors", "expertise_ko", "expertise_ko TEXT")

    add_column_if_missing("services", "title", "title TEXT")
    add_column_if_missing("services", "slug", "slug TEXT")
    add_column_if_missing("services", "description", "description TEXT")
    add_column_if_missing("services", "detail", "detail TEXT")
    add_column_if_missing("services", "image_file", "image_file TEXT")
    add_column_if_missing("services", "is_active", "is_active INTEGER DEFAULT 1")
    add_column_if_missing("services", "created_at", "created_at TEXT")

    add_column_if_missing("services", "title_th", "title_th TEXT")
    add_column_if_missing("services", "title_en", "title_en TEXT")
    add_column_if_missing("services", "title_zh", "title_zh TEXT")
    add_column_if_missing("services", "title_ja", "title_ja TEXT")
    add_column_if_missing("services", "title_ko", "title_ko TEXT")

    add_column_if_missing("services", "description_th", "description_th TEXT")
    add_column_if_missing("services", "description_en", "description_en TEXT")
    add_column_if_missing("services", "description_zh", "description_zh TEXT")
    add_column_if_missing("services", "description_ja", "description_ja TEXT")
    add_column_if_missing("services", "description_ko", "description_ko TEXT")

    add_column_if_missing("services", "detail_th", "detail_th TEXT")
    add_column_if_missing("services", "detail_en", "detail_en TEXT")
    add_column_if_missing("services", "detail_zh", "detail_zh TEXT")
    add_column_if_missing("services", "detail_ja", "detail_ja TEXT")
    add_column_if_missing("services", "detail_ko", "detail_ko TEXT")

    add_column_if_missing("service_media", "service_id", "service_id INTEGER")
    add_column_if_missing("service_media", "media_type", "media_type TEXT DEFAULT 'image'")
    add_column_if_missing("service_media", "image_file", "image_file TEXT")
    add_column_if_missing("service_media", "video_file", "video_file TEXT")
    add_column_if_missing("service_media", "video_url", "video_url TEXT")
    add_column_if_missing("service_media", "caption", "caption TEXT")
    add_column_if_missing("service_media", "sort_order", "sort_order INTEGER DEFAULT 0")
    add_column_if_missing("service_media", "is_active", "is_active INTEGER DEFAULT 1")
    add_column_if_missing("service_media", "created_at", "created_at TEXT")

    add_column_if_missing("service_programs", "service_id", "service_id INTEGER")
    add_column_if_missing("service_programs", "title", "title TEXT")
    add_column_if_missing("service_programs", "slug", "slug TEXT")
    add_column_if_missing("service_programs", "short_description", "short_description TEXT")
    add_column_if_missing("service_programs", "detail", "detail TEXT")
    add_column_if_missing("service_programs", "cover_image_file", "cover_image_file TEXT")
    add_column_if_missing("service_programs", "sort_order", "sort_order INTEGER DEFAULT 0")
    add_column_if_missing("service_programs", "is_active", "is_active INTEGER DEFAULT 1")
    add_column_if_missing("service_programs", "created_at", "created_at TEXT")

    add_column_if_missing("program_media", "program_id", "program_id INTEGER")
    add_column_if_missing("program_media", "media_type", "media_type TEXT DEFAULT 'image'")
    add_column_if_missing("program_media", "image_file", "image_file TEXT")
    add_column_if_missing("program_media", "video_file", "video_file TEXT")
    add_column_if_missing("program_media", "video_url", "video_url TEXT")
    add_column_if_missing("program_media", "caption", "caption TEXT")
    add_column_if_missing("program_media", "sort_order", "sort_order INTEGER DEFAULT 0")
    add_column_if_missing("program_media", "is_active", "is_active INTEGER DEFAULT 1")
    add_column_if_missing("program_media", "created_at", "created_at TEXT")

    add_column_if_missing("line_leads", "display_name", "display_name TEXT")
    add_column_if_missing("line_leads", "message_text", "message_text TEXT")
    add_column_if_missing("line_leads", "customer_name", "customer_name TEXT")
    add_column_if_missing("line_leads", "phone", "phone TEXT")
    add_column_if_missing("line_leads", "package_name", "package_name TEXT")
    add_column_if_missing("line_leads", "appointment_date", "appointment_date TEXT")
    add_column_if_missing("line_leads", "appointment_time", "appointment_time TEXT")
    add_column_if_missing("line_leads", "status", "status TEXT DEFAULT 'NEW'")
    add_column_if_missing("line_leads", "note", "note TEXT")

    conn.commit()
    conn.close()


def seed_admin_user():
    force_load_env()

    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()

    if not admin_email or not admin_password:
        print("========== ADMIN SEED SKIPPED ==========")
        print("Please set ADMIN_EMAIL and ADMIN_PASSWORD in .env")
        print("========================================")
        return

    conn = get_db()
    cur = conn.cursor()

    existing = cur.execute(
        "SELECT * FROM users WHERE email = ?",
        (admin_email,),
    ).fetchone()

    if existing:
        cur.execute(
            """
            UPDATE users
            SET full_name = ?, password_hash = ?, provider = ?, role = ?
            WHERE email = ?
            """,
            (
                "Home Care Admin",
                hash_password(admin_password),
                "local",
                "ADMIN",
                admin_email,
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO users (
                full_name,
                email,
                phone,
                password_hash,
                provider,
                google_id,
                role,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Home Care Admin",
                admin_email,
                "",
                hash_password(admin_password),
                "local",
                None,
                "ADMIN",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    conn.commit()
    conn.close()

    print("========== ADMIN READY ==========")
    print("ADMIN_EMAIL:", admin_email)
    print("=================================")


def seed_default_clinic_data():
    conn = get_db()
    cur = conn.cursor()

    default_packages = [
        (
            "Facial Radiance Package",
            "facial-radiance",
            "ทำความสะอาดผิว เติมความชุ่มชื้น และเพิ่มความกระจ่างใส",
            "฿2,890",
            2890,
            "POPULAR",
            "facial-radiance.jpg",
        ),
        (
            "Acne Care Package",
            "acne-care",
            "ดูแลปัญหาสิว ลดการอุดตัน และฟื้นฟูผิวให้เรียบเนียน",
            "฿3,690",
            3690,
            "BEST SELLER",
            "acne-care.jpg",
        ),
        (
            "Laser Glow Package",
            "laser-glow",
            "ปรับสีผิว ลดรอย และช่วยให้ผิวดูละเอียดขึ้น",
            "฿4,990",
            4990,
            "LIMITED",
            "laser-glow.jpg",
        ),
    ]

    for package in default_packages:
        exists = cur.execute(
            "SELECT id FROM packages WHERE slug = ?",
            (package[1],),
        ).fetchone()

        if not exists:
            cur.execute(
                """
                INSERT INTO packages (
                    name,
                    slug,
                    description,
                    price,
                    price_amount,
                    badge,
                    image_file,
                    image_file_1,
                    is_active,
                    created_at,
                    name_th,
                    name_en,
                    description_th,
                    description_en
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    package[0],
                    package[1],
                    package[2],
                    package[3],
                    package[4],
                    package[5],
                    package[6],
                    package[6],
                    datetime.now().isoformat(timespec="seconds"),
                    package[0],
                    package[0],
                    package[2],
                    package[2],
                ),
            )

    default_promotions = [
        (
            "Facial Radiance Package",
            "promo-facial-radiance",
            "ทำความสะอาดผิว เติมความชุ่มชื้น และเพิ่มความกระจ่างใส",
            "฿2,890",
            "POPULAR",
            "facial-radiance.jpg",
        ),
        (
            "Acne Care Package",
            "promo-acne-care",
            "ดูแลปัญหาสิว ลดการอุดตัน และฟื้นฟูผิวให้เรียบเนียน",
            "฿3,690",
            "BEST SELLER",
            "acne-care.jpg",
        ),
        (
            "Laser Glow Package",
            "promo-laser-glow",
            "ปรับสีผิว ลดรอย และช่วยให้ผิวดูละเอียดขึ้น",
            "฿4,990",
            "LIMITED",
            "laser-glow.jpg",
        ),
    ]

    for promo in default_promotions:
        exists = cur.execute(
            "SELECT id FROM promotions WHERE slug = ?",
            (promo[1],),
        ).fetchone()

        if not exists:
            cur.execute(
                """
                INSERT INTO promotions (
                    title,
                    slug,
                    description,
                    price,
                    badge,
                    image_file_1,
                    is_active,
                    created_at,
                    title_th,
                    title_en,
                    description_th,
                    description_en
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    promo[0],
                    promo[1],
                    promo[2],
                    promo[3],
                    promo[4],
                    promo[5],
                    datetime.now().isoformat(timespec="seconds"),
                    promo[0],
                    promo[0],
                    promo[2],
                    promo[2],
                ),
            )

    default_reviews = [
        ("Clean & Friendly", "คลินิกสะอาด บริการเป็นกันเอง และมีระบบจองที่ชัดเจน", 5),
        ("Easy Booking", "เลือกแพ็กเกจได้ง่าย มีรายละเอียดครบ และได้รับการดูแลรวดเร็ว", 5),
        ("Personalized Care", "ดูแลแบบเฉพาะบุคคล เหมาะกับลูกค้าที่ต้องการความน่าเชื่อถือ", 5),
    ]

    for review in default_reviews:
        exists = cur.execute(
            "SELECT id FROM reviews WHERE customer_name = ?",
            (review[0],),
        ).fetchone()

        if not exists:
            cur.execute(
                """
                INSERT INTO reviews (
                    customer_name,
                    review_text,
                    rating,
                    image_file,
                    is_active,
                    created_at
                )
                VALUES (?, ?, ?, NULL, 1, ?)
                """,
                (
                    review[0],
                    review[1],
                    review[2],
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    default_doctors = [
        (
            "Dr. Ploy",
            "Aesthetic Doctor",
            "ดูแลสิว ผิวแพ้ง่าย เติมความชุ่มชื้น และวางแผนดูแลผิวเฉพาะบุคคล",
            "",
            1,
        ),
        (
            "Dr. Mint",
            "Skin Specialist",
            "เลเซอร์ผิวหน้า ลดรอยสิว ปรับผิวกระจ่างใส และฟื้นฟูผิวโทรม",
            "",
            1,
        ),
        (
            "Dr. Nicha",
            "Laser Specialist",
            "เลเซอร์ ฝ้า กระ จุดด่างดำ และปรับสภาพผิวให้เรียบเนียน",
            "",
            1,
        ),
    ]

    for doctor in default_doctors:
        exists = cur.execute(
            "SELECT id FROM doctors WHERE name = ?",
            (doctor[0],),
        ).fetchone()

        if not exists:
            cur.execute(
                """
                INSERT INTO doctors (
                    name,
                    specialty,
                    expertise,
                    phone,
                    image_file,
                    is_active,
                    created_at,
                    name_th,
                    name_en,
                    specialty_th,
                    specialty_en,
                    expertise_th,
                    expertise_en
                )
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doctor[0],
                    doctor[1],
                    doctor[2],
                    doctor[3],
                    doctor[4],
                    datetime.now().isoformat(timespec="seconds"),
                    doctor[0],
                    doctor[0],
                    doctor[1],
                    doctor[1],
                    doctor[2],
                    doctor[2],
                ),
            )

    default_services = [
        (
            "ปรึกษาปัญหาผิว",
            "skin-consultation",
            "วิเคราะห์สภาพผิวและแนะนำโปรแกรมที่เหมาะสมกับลูกค้าแต่ละคน",
            "บริการปรึกษาปัญหาผิวโดยทีมคลินิก เหมาะสำหรับลูกค้าที่ต้องการเลือกโปรแกรมดูแลผิวให้ตรงกับปัญหาและงบประมาณ",
            "",
        ),
        (
            "ทรีตเมนต์ผิวหน้า",
            "facial-treatment",
            "ดูแลผิวหน้าให้สะอาด ชุ่มชื้น ลดความหมองคล้ำ และช่วยให้ผิวดูสุขภาพดี",
            "ทรีตเมนต์ผิวหน้าสำหรับฟื้นฟูผิว เติมความชุ่มชื้น และดูแลผิวหมองคล้ำอย่างอ่อนโยน",
            "",
        ),
        (
            "เลเซอร์ผิว",
            "skin-laser",
            "โปรแกรมเลเซอร์เพื่อผิวกระจ่างใส ลดรอย และฟื้นฟูสภาพผิว",
            "บริการเลเซอร์ผิวเพื่อช่วยดูแลรอยสิว จุดด่างดำ และความหมองคล้ำ โดยเลือกโปรแกรมตามสภาพผิว",
            "",
        ),
        (
            "คอร์สดูแลต่อเนื่อง",
            "continuing-care-course",
            "ออกแบบคอร์สการดูแลเป็นรอบ เหมาะสำหรับลูกค้าที่ต้องการผลลัพธ์ต่อเนื่อง",
            "คอร์สดูแลต่อเนื่องสำหรับลูกค้าที่ต้องการวางแผนดูแลผิวระยะยาว พร้อมติดตามผลเป็นรอบ",
            "",
        ),
    ]

    for service in default_services:
        exists = cur.execute(
            "SELECT id FROM services WHERE slug = ?",
            (service[1],),
        ).fetchone()

        if not exists:
            cur.execute(
                """
                INSERT INTO services (
                    title,
                    slug,
                    description,
                    detail,
                    image_file,
                    is_active,
                    created_at,

                    title_th,
                    title_en,
                    description_th,
                    description_en,
                    detail_th,
                    detail_en
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    service[0],
                    service[1],
                    service[2],
                    service[3],
                    service[4],
                    datetime.now().isoformat(timespec="seconds"),

                    service[0],
                    service[0],
                    service[2],
                    service[2],
                    service[3],
                    service[3],
                ),
            )

    conn.commit()
    conn.close()


@app.on_event("startup")
def startup_event():
    init_db()
    seed_admin_user()
    seed_default_clinic_data()


def current_user(request: Request):
    user_id = request.session.get("user_id")

    if not user_id:
        return None

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    conn.close()

    return user


def user_role(user) -> str:
    if not user:
        return "GUEST"

    try:
        return user["role"] or "USER"
    except Exception:
        return "USER"


def require_admin(request: Request):
    user = current_user(request)

    if not user:
        return None

    if user_role(user) != "ADMIN":
        return None

    return user


def get_active_packages():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM packages
        WHERE is_active = 1
        ORDER BY id ASC
        """
    ).fetchall()
    conn.close()
    return rows


def get_active_promotions():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT
            *,
            CASE
                WHEN image_file_1 IS NOT NULL AND image_file_1 != ''
                THEN '/static/images/promotions/' || image_file_1
                ELSE ''
            END AS image_url
        FROM promotions
        WHERE is_active = 1
        ORDER BY id ASC
        """
    ).fetchall()
    conn.close()
    return rows


def detect_video_platform(video_url: str):
    url = (video_url or "").lower()
    if "instagram.com" in url:
        return "instagram"
    if "tiktok.com" in url:
        return "tiktok"
    if "facebook.com" in url or "fb.watch" in url:
        return "facebook"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    return "other"


def get_active_homecare_channels():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT
            *,
            CASE
                WHEN cover_image_file IS NOT NULL AND cover_image_file != ''
                THEN '/static/images/homecare_channel/' || cover_image_file
                ELSE ''
            END AS cover_image_url
        FROM homecare_channels
        WHERE is_active = 1
        ORDER BY sort_order ASC, id DESC
        """
    ).fetchall()
    conn.close()
    return rows


def get_active_reviews():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM reviews
        WHERE is_active = 1
        ORDER BY id DESC
        """
    ).fetchall()
    conn.close()
    return rows


def get_active_doctors():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT
            *,
            CASE
                WHEN image_file IS NOT NULL AND image_file != ''
                THEN '/static/images/doctors/' || image_file
                ELSE ''
            END AS image_url
        FROM doctors
        WHERE is_active = 1
        ORDER BY id ASC
        """
    ).fetchall()
    conn.close()
    return rows




def get_active_services():
    conn = get_db()
    service_rows = conn.execute(
        """
        SELECT
            *,
            CASE
                WHEN image_file IS NOT NULL AND image_file != ''
                THEN '/static/images/services/' || image_file
                ELSE ''
            END AS image_url,
            title AS name,
            detail AS full_detail,
            detail AS subtitle
        FROM services
        WHERE is_active = 1
        ORDER BY id ASC
        """
    ).fetchall()

    program_rows = conn.execute(
        """
        SELECT *
        FROM service_programs
        WHERE is_active = 1
        ORDER BY service_id ASC, sort_order ASC, id ASC
        """
    ).fetchall()

    media_rows = conn.execute(
        """
        SELECT *
        FROM program_media
        WHERE is_active = 1
        ORDER BY program_id ASC, sort_order ASC, id ASC
        """
    ).fetchall()
    conn.close()

    media_by_program = {}
    for row in media_rows:
        item = dict(row)
        if item.get("image_file"):
            item["image_url"] = f"/static/images/services/{item['image_file']}"
        else:
            item["image_url"] = ""

        if item.get("video_file"):
            item["video_src"] = f"/static/videos/services/{item['video_file']}"
        else:
            item["video_src"] = item.get("video_url") or ""

        media_by_program.setdefault(item["program_id"], []).append(item)

    programs_by_service = {}
    for row in program_rows:
        program = dict(row)
        if program.get("cover_image_file"):
            program["cover_image_url"] = f"/static/images/services/{program['cover_image_file']}"
        else:
            program["cover_image_url"] = ""

        media_list = media_by_program.get(program["id"], [])
        if not media_list and program.get("cover_image_file"):
            media_list = [
                {
                    "id": 0,
                    "program_id": program["id"],
                    "media_type": "image",
                    "image_file": program.get("cover_image_file") or "",
                    "video_file": "",
                    "video_url": "",
                    "caption": program.get("short_description") or program.get("detail") or "",
                    "sort_order": 0,
                    "is_active": 1,
                    "image_url": program.get("cover_image_url") or "",
                    "video_src": "",
                }
            ]
        program["media"] = media_list
        program["media_json"] = json.dumps(media_list, ensure_ascii=False)
        programs_by_service.setdefault(program["service_id"], []).append(program)

    services = []
    for row in service_rows:
        item = dict(row)
        item["programs"] = programs_by_service.get(item["id"], [])
        item["programs_json"] = json.dumps(item["programs"], ensure_ascii=False)
        services.append(item)

    return services

def get_package_by_slug(slug: str):
    conn = get_db()
    row = conn.execute(
        """
        SELECT * FROM packages
        WHERE slug = ? AND is_active = 1
        """,
        (slug,),
    ).fetchone()
    conn.close()
    return row


def get_package_price_amount(package_name: str) -> float:
    conn = get_db()
    row = conn.execute(
        "SELECT price_amount FROM packages WHERE name = ? OR name_th = ? OR name_en = ?",
        (package_name, package_name, package_name),
    ).fetchone()
    conn.close()

    if not row:
        return 0

    return float(row["price_amount"] or 0)


def get_doctor_name(doctor_id):
    if not doctor_id:
        return "-"

    conn = get_db()
    row = conn.execute(
        "SELECT name FROM doctors WHERE id = ?",
        (doctor_id,),
    ).fetchone()
    conn.close()

    return row["name"] if row else "-"


def get_revenue_stats():
    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")
    year = datetime.now().strftime("%Y")

    conn = get_db()

    daily = conn.execute(
        """
        SELECT COALESCE(SUM(revenue_amount), 0) AS total
        FROM bookings
        WHERE appointment_date = ?
          AND status != 'CANCELLED'
        """,
        (today,),
    ).fetchone()["total"]

    monthly = conn.execute(
        """
        SELECT COALESCE(SUM(revenue_amount), 0) AS total
        FROM bookings
        WHERE substr(appointment_date, 1, 7) = ?
          AND status != 'CANCELLED'
        """,
        (month,),
    ).fetchone()["total"]

    yearly = conn.execute(
        """
        SELECT COALESCE(SUM(revenue_amount), 0) AS total
        FROM bookings
        WHERE substr(appointment_date, 1, 4) = ?
          AND status != 'CANCELLED'
        """,
        (year,),
    ).fetchone()["total"]

    conn.close()

    return {
        "daily": float(daily or 0),
        "monthly": float(monthly or 0),
        "yearly": float(yearly or 0),
    }


def send_booking_email(
    to_email: str,
    full_name: str,
    package_name: str,
    appointment_date: str,
    appointment_time: str,
    doctor_name: str = "-",
    customer_phone: str = "-",
    subject_prefix: str = "ยืนยันการจองคิว",
):
    force_load_env()

    smtp_host = os.getenv("SMTP_HOST", "").strip() or "smtp.gmail.com"
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    smtp_user = clean_email_address(os.getenv("SMTP_USER", "").strip())
    smtp_pass = os.getenv("SMTP_PASS", "").strip().replace(" ", "")
    smtp_from_raw = os.getenv("SMTP_FROM", "").strip()
    smtp_from = clean_email_address(smtp_from_raw) or smtp_user

    to_email = clean_email_address(to_email)
    subject = f"{subject_prefix} Home Care Clinic"

    text = f"""
Home Care Clinic

เรียนคุณ {full_name}

{subject_prefix}ของคุณเรียบร้อยแล้ว

เบอร์โทร: {customer_phone}
คุณหมอ: {doctor_name}
Package: {package_name}
Appointment Date: {appointment_date}
Appointment Time: {appointment_time}
Status: Pending Confirmation

กรุณามาถึงก่อนเวลานัดประมาณ 10-15 นาที

ขอบคุณที่ไว้วางใจ Home Care Clinic
"""

    html = f"""
    <div style="font-family:Arial,sans-serif;background:#fff3f4;padding:30px;">
      <div style="max-width:620px;margin:auto;background:#fffaf3;border-radius:18px;padding:28px;border:1px solid #f0d8bd;">
        <h2 style="color:#c7a46b;margin-top:0;">Home Care Clinic</h2>
        <p>เรียนคุณ <b>{full_name}</b>,</p>
        <p>{subject_prefix}ของคุณเรียบร้อยแล้ว</p>

        <div style="background:#f8d8dc;padding:18px;border-radius:14px;margin:20px 0;">
          <p><b>Phone:</b> {customer_phone}</p>
          <p><b>Doctor:</b> {doctor_name}</p>
          <p><b>Package:</b> {package_name}</p>
          <p><b>Appointment Date:</b> {appointment_date}</p>
          <p><b>Appointment Time:</b> {appointment_time}</p>
          <p><b>Status:</b> Pending Confirmation</p>
        </div>

        <p>กรุณามาถึงก่อนเวลานัดประมาณ 10-15 นาที</p>
        <p style="color:#66785f;">ขอบคุณที่ไว้วางใจ Home Care Clinic</p>
      </div>
    </div>
    """

    if not smtp_host or not smtp_user or not smtp_pass:
        print("========== EMAIL NOT SENT ==========")
        print("SMTP config missing. Please check .env")
        print("====================================")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = smtp_from
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [to_email], msg.as_string())

        print("========== EMAIL SENT ==========")
        print(f"Booking email sent to: {to_email}")
        print("================================")
        return True

    except Exception as e:
        print("========== EMAIL ERROR ==========")
        print(repr(e))
        print("=================================")
        return False


def send_line_booking_alert(
    full_name: str,
    email: str,
    package_name: str,
    appointment_date: str,
    appointment_time: str,
    note: str = "",
    doctor_name: str = "-",
    customer_phone: str = "-",
):
    force_load_env()

    line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    line_user_id = os.getenv("LINE_ADMIN_USER_ID", "").strip()

    if not line_token or not line_user_id:
        print("========== LINE NOT SENT ==========")
        print("Missing LINE_CHANNEL_ACCESS_TOKEN or LINE_ADMIN_USER_ID in .env")
        print("===================================")
        return False

    message = f"""🔔 มีการจองคิวใหม่ Home Care Clinic

👤 ลูกค้า: {full_name}
📞 เบอร์โทร: {customer_phone}
📧 Email: {email}
👩‍⚕️ คุณหมอ: {doctor_name}
💆 Package: {package_name}
📅 วันที่: {appointment_date}
⏰ เวลา: {appointment_time}
📝 หมายเหตุ: {note or "-"}

สถานะ: PENDING"""

    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {line_token}",
                "Content-Type": "application/json",
            },
            json={
                "to": line_user_id,
                "messages": [{"type": "text", "text": message}],
            },
            timeout=20,
        )

        print("========== LINE RESPONSE ==========")
        print("STATUS:", response.status_code)
        print("BODY:", response.text)
        print("===================================")

        return response.status_code == 200

    except Exception as e:
        print("========== LINE ERROR ==========")
        print(repr(e))
        print("================================")
        return False


def get_line_profile(line_user_id: str):
    force_load_env()

    line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()

    if not line_token or not line_user_id:
        return ""

    try:
        response = requests.get(
            f"https://api.line.me/v2/bot/profile/{line_user_id}",
            headers={"Authorization": f"Bearer {line_token}"},
            timeout=15,
        )

        if response.status_code == 200:
            data = response.json()
            return data.get("displayName", "")

        print("LINE PROFILE ERROR:", response.status_code, response.text)
        return ""

    except Exception as e:
        print("LINE PROFILE EXCEPTION:", repr(e))
        return ""


def parse_line_booking_message(message_text: str):
    data = {
        "customer_name": "",
        "phone": "",
        "package_name": "",
        "appointment_date": "",
        "appointment_time": "",
        "note": "",
        "status": "NEW",
    }

    if not message_text:
        return data

    text = message_text.strip()

    if "จอง" in text:
        data["status"] = "BOOKING_REQUEST"

    lines = text.splitlines()

    for line in lines:
        clean = line.strip()

        if ":" not in clean:
            continue

        key, value = clean.split(":", 1)
        key = key.strip().lower()
        value = value.strip()

        if key in ["ชื่อ", "name", "customer", "ลูกค้า"]:
            data["customer_name"] = value

        elif key in ["เบอร์", "เบอร์โทร", "phone", "tel", "โทร"]:
            data["phone"] = value

        elif key in ["แพ็กเกจ", "แพคเกจ", "package", "บริการ"]:
            data["package_name"] = value

        elif key in ["วันที่", "date", "วัน"]:
            data["appointment_date"] = value

        elif key in ["เวลา", "time"]:
            data["appointment_time"] = value

        elif key in ["หมายเหตุ", "note", "เพิ่มเติม"]:
            data["note"] = value

    return data


def save_line_lead(
    line_user_id: str,
    display_name: str,
    message_text: str,
):
    parsed = parse_line_booking_message(message_text)

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO line_leads (
            line_user_id,
            display_name,
            message_text,
            customer_name,
            phone,
            package_name,
            appointment_date,
            appointment_time,
            status,
            note,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            line_user_id,
            display_name,
            message_text,
            parsed["customer_name"],
            parsed["phone"],
            parsed["package_name"],
            parsed["appointment_date"],
            parsed["appointment_time"],
            parsed["status"],
            parsed["note"],
            datetime.now().isoformat(timespec="seconds"),
        ),
    )

    conn.commit()
    lead_id = cur.lastrowid
    conn.close()

    return lead_id


def reply_line_message(reply_token: str, text: str):
    force_load_env()

    line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()

    if not line_token or not reply_token:
        return False

    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {line_token}",
                "Content-Type": "application/json",
            },
            json={
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": text}],
            },
            timeout=15,
        )

        print("LINE REPLY:", response.status_code, response.text)
        return response.status_code == 200

    except Exception as e:
        print("LINE REPLY ERROR:", repr(e))
        return False


@app.get("/")
def root():
    return RedirectResponse("/home", status_code=303)


@app.get("/home")
def home(request: Request):
    force_load_env()

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "user": current_user(request),
            "packages": get_active_packages(),
            "promotions": get_active_promotions(),
            "reviews": get_active_reviews(),
            "doctors": get_active_doctors(),
            "services": get_active_services(),
            "homecare_channels": get_active_homecare_channels(),
            "clinic_location_name": os.getenv(
                "CLINIC_LOCATION_NAME",
                "Home Care Clinic, Bangkok",
            ),
            "clinic_google_map_url": os.getenv(
                "CLINIC_GOOGLE_MAP_URL",
                "https://maps.google.com/?q=Home+Care+Clinic+Bangkok",
            ),
        },
    )


@app.get("/contact")
def contact_page(request: Request):
    force_load_env()

    return templates.TemplateResponse(
        "contact.html",
        {
            "request": request,
            "user": current_user(request),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
            "clinic_location_name": os.getenv(
                "CLINIC_LOCATION_NAME",
                "Home Care Clinic, Bangkok",
            ),
            "clinic_google_map_url": os.getenv(
                "CLINIC_GOOGLE_MAP_URL",
                "https://maps.google.com/?q=Home+Care+Clinic+Bangkok",
            ),
        },
    )


@app.post("/contact")
def contact_submit(
    request: Request,
    customer_name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    package_name: str = Form("ติดต่อสอบถาม"),
    appointment_date: str = Form(""),
    appointment_time: str = Form(""),
    note: str = Form(""),
):
    force_load_env()

    customer_name = (customer_name or "").strip()
    phone = (phone or "").strip()
    email = (email or "").strip()
    package_name = (package_name or "ติดต่อสอบถาม").strip()
    appointment_date = (appointment_date or "").strip()
    appointment_time = (appointment_time or "").strip()
    note = (note or "").strip()

    if not customer_name:
        return RedirectResponse(
            "/contact?error=กรุณากรอกชื่อ",
            status_code=303,
        )

    message_text = f"""ติดต่อจากหน้า Contact
ชื่อ: {customer_name}
เบอร์โทร: {phone or '-'}
Email: {email or '-'}
บริการที่สนใจ: {package_name or '-'}
วันที่สนใจ: {appointment_date or '-'}
เวลาที่สนใจ: {appointment_time or '-'}
หมายเหตุ: {note or '-'}"""

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO line_leads (
            line_user_id,
            display_name,
            message_text,
            customer_name,
            phone,
            package_name,
            appointment_date,
            appointment_time,
            status,
            note,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "CONTACT_FORM",
            customer_name,
            message_text,
            customer_name,
            phone,
            package_name,
            appointment_date,
            appointment_time,
            "NEW",
            note,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    conn.close()

    try:
        send_line_booking_alert(
            full_name=customer_name,
            email=email or "-",
            package_name=package_name or "ติดต่อสอบถาม",
            appointment_date=appointment_date or "-",
            appointment_time=appointment_time or "-",
            note=f"ติดต่อจากหน้า Contact: {note or '-'}",
            doctor_name="-",
            customer_phone=phone or "-",
        )
    except Exception as e:
        print("CONTACT LINE ALERT ERROR:", repr(e))

    return RedirectResponse(
        "/contact?success=ส่งข้อมูลสำเร็จ เจ้าหน้าที่จะติดต่อกลับโดยเร็ว",
        status_code=303,
    )


@app.get("/register")
def register_page(request: Request):
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "user": current_user(request),
            "error": None,
        },
    )


@app.post("/register")
def register(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    password: str = Form(...),
):
    email = email.strip().lower()

    conn = get_db()
    exists = conn.execute(
        "SELECT id FROM users WHERE email = ?",
        (email,),
    ).fetchone()

    if exists:
        conn.close()
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "user": current_user(request),
                "error": "Email นี้ถูกใช้งานแล้ว กรุณา Login",
            },
        )

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (
            full_name,
            email,
            phone,
            password_hash,
            provider,
            google_id,
            role,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            full_name.strip(),
            email,
            phone.strip(),
            hash_password(password),
            "local",
            None,
            "USER",
            datetime.now().isoformat(timespec="seconds"),
        ),
    )

    conn.commit()
    user_id = cur.lastrowid
    conn.close()

    request.session["user_id"] = user_id
    return RedirectResponse("/booking", status_code=303)


@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "user": current_user(request),
            "error": None,
            "next_url": request.query_params.get("next", "/booking"),
        },
    )


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next_url: str = Form("/booking"),
):
    email = email.strip().lower()

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    conn.close()

    if not user:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "user": None,
                "error": "Email หรือ Password ไม่ถูกต้อง",
                "next_url": next_url,
            },
        )

    if user["provider"] == "google" and not user["password_hash"]:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "user": None,
                "error": "บัญชีนี้สมัครผ่าน Google กรุณากด Login with Google",
                "next_url": next_url,
            },
        )

    if user["password_hash"] != hash_password(password):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "user": None,
                "error": "Email หรือ Password ไม่ถูกต้อง",
                "next_url": next_url,
            },
        )

    request.session["user_id"] = user["id"]

    if user_role(user) == "ADMIN":
        return RedirectResponse("/admin/dashboard", status_code=303)

    return RedirectResponse(next_url or "/booking", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/home", status_code=303)


@app.get("/auth/google")
async def auth_google(request: Request):
    if not os.getenv("GOOGLE_CLIENT_ID") or not os.getenv("GOOGLE_CLIENT_SECRET"):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "user": current_user(request),
                "error": "ยังไม่ได้ตั้งค่า GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET ในไฟล์ .env",
                "next_url": "/booking",
            },
        )

    redirect_uri = request.url_for("auth_google_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get("userinfo")

        if not user_info:
            user_info = await oauth.google.userinfo(token=token)

        google_id = user_info.get("sub")
        email = user_info.get("email")
        full_name = user_info.get("name") or email

        if not email:
            return RedirectResponse("/login", status_code=303)

        email = email.strip().lower()

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,),
        ).fetchone()

        if user:
            conn.execute(
                """
                UPDATE users
                SET provider = ?, google_id = ?
                WHERE id = ?
                """,
                ("google", google_id, user["id"]),
            )
            conn.commit()
            user_id = user["id"]
        else:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO users (
                    full_name,
                    email,
                    phone,
                    password_hash,
                    provider,
                    google_id,
                    role,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    full_name,
                    email,
                    "",
                    "",
                    "google",
                    google_id,
                    "USER",
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            user_id = cur.lastrowid

        conn.close()
        request.session["user_id"] = user_id

        return RedirectResponse("/booking", status_code=303)

    except Exception as e:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "user": current_user(request),
                "error": f"Google Login ไม่สำเร็จ: {str(e)}",
                "next_url": "/booking",
            },
        )


@app.get("/line/webhook")
def line_webhook_check():
    return {"ok": True, "message": "LINE webhook endpoint is ready"}


@app.post("/line/webhook")
async def line_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    print("")
    print("========== LINE WEBHOOK ==========")
    print(body)
    print("==================================")
    print("")

    events = body.get("events", [])

    for event in events:
        event_type = event.get("type")
        source = event.get("source", {})
        message = event.get("message", {})
        reply_token = event.get("replyToken")

        line_user_id = source.get("userId", "")
        message_type = message.get("type", "")
        message_text = message.get("text", "")

        if event_type == "message" and message_type == "text":
            display_name = get_line_profile(line_user_id)

            lead_id = save_line_lead(
                line_user_id=line_user_id,
                display_name=display_name,
                message_text=message_text,
            )

            print("========== LINE LEAD SAVED ==========")
            print("LEAD ID:", lead_id)
            print("LINE USER:", line_user_id)
            print("DISPLAY NAME:", display_name)
            print("MESSAGE:", message_text)
            print("=====================================")

            if "จอง" in message_text:
                reply_line_message(
                    reply_token,
                    "ขอบคุณค่ะ Home Care Clinic ได้รับข้อมูลการจองแล้วค่ะ ทีมงานจะตรวจสอบและติดต่อกลับเพื่อยืนยันวันนัดอีกครั้งนะคะ",
                )
            else:
                reply_line_message(
                    reply_token,
                    "ขอบคุณที่ติดต่อ Home Care Clinic ค่ะ หากต้องการจองคิว กรุณาพิมพ์ตามรูปแบบนี้:\n\nจองคิว\nชื่อ:\nเบอร์โทร:\nแพ็กเกจ:\nวันที่:\nเวลา:\nหมายเหตุ:",
                )

    return {"ok": True}


@app.get("/booking")
def booking_page(request: Request):
    user = current_user(request)

    if not user:
        return RedirectResponse("/login?next=/booking", status_code=303)

    if user_role(user) == "ADMIN":
        return RedirectResponse("/admin/dashboard", status_code=303)

    return templates.TemplateResponse(
        "booking.html",
        {
            "request": request,
            "user": user,
            "packages": get_active_packages(),
        },
    )


@app.get("/api/doctors/available")
def available_doctors(date: str = ""):
    rows = get_active_doctors()

    return {
        "doctors": [
            {
                "doctor_id": row["id"],
                "name": row["name"],
                "specialty": row["specialty"],
                "expertise": row["expertise"],
                "start_time": "",
                "end_time": "",
            }
            for row in rows
        ]
    }


@app.post("/booking")
def create_booking(
    request: Request,
    customer_phone: str = Form(...),
    doctor_id: int = Form(...),
    package_name: str = Form(...),
    appointment_date: str = Form(...),
    appointment_time: str = Form(...),
    note: str = Form(""),
):
    user = current_user(request)

    if not user:
        return RedirectResponse("/login?next=/booking", status_code=303)

    if user_role(user) == "ADMIN":
        return RedirectResponse("/admin/dashboard", status_code=303)

    doctor_name = get_doctor_name(doctor_id)
    revenue_amount = get_package_price_amount(package_name)

    email_sent = send_booking_email(
        to_email=user["email"],
        full_name=user["full_name"],
        package_name=package_name,
        appointment_date=appointment_date,
        appointment_time=appointment_time,
        doctor_name=doctor_name,
        customer_phone=customer_phone,
    )

    line_sent = send_line_booking_alert(
        full_name=user["full_name"],
        email=user["email"],
        package_name=package_name,
        appointment_date=appointment_date,
        appointment_time=appointment_time,
        note=note,
        doctor_name=doctor_name,
        customer_phone=customer_phone,
    )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO bookings (
            user_id,
            customer_phone,
            doctor_id,
            package_name,
            appointment_date,
            appointment_time,
            note,
            status,
            revenue_amount,
            email_sent,
            line_sent,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["id"],
            customer_phone,
            doctor_id,
            package_name,
            appointment_date,
            appointment_time,
            note,
            "PENDING",
            revenue_amount,
            1 if email_sent else 0,
            1 if line_sent else 0,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )

    booking_id = cur.lastrowid
    conn.commit()
    conn.close()

    return RedirectResponse(f"/booking-success/{booking_id}", status_code=303)


@app.get("/booking-success/{booking_id}")
def booking_success(request: Request, booking_id: int):
    user = current_user(request)

    if not user:
        return RedirectResponse("/login?next=/booking", status_code=303)

    conn = get_db()
    booking = conn.execute(
        """
        SELECT
            bookings.*,
            doctors.name AS doctor_name
        FROM bookings
        LEFT JOIN doctors ON doctors.id = bookings.doctor_id
        WHERE bookings.id = ? AND bookings.user_id = ?
        """,
        (booking_id, user["id"]),
    ).fetchone()
    conn.close()

    if not booking:
        return RedirectResponse("/home", status_code=303)

    return templates.TemplateResponse(
        "booking_success.html",
        {
            "request": request,
            "user": user,
            "booking": booking,
        },
    )


@app.get("/admin/login")
def admin_login_redirect():
    return RedirectResponse("/login", status_code=303)


@app.get("/admin")
def admin_root():
    return RedirectResponse("/admin/dashboard", status_code=303)


@app.get("/admin/dashboard")
def admin_dashboard(request: Request):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/dashboard", status_code=303)

    conn = get_db()

    stats = {
        "total_bookings": conn.execute(
            "SELECT COUNT(*) AS count FROM bookings"
        ).fetchone()["count"],
        "pending_bookings": conn.execute(
            "SELECT COUNT(*) AS count FROM bookings WHERE status = 'PENDING'"
        ).fetchone()["count"],
        "email_sent": conn.execute(
            "SELECT COUNT(*) AS count FROM bookings WHERE email_sent = 1"
        ).fetchone()["count"],
        "line_sent": conn.execute(
            "SELECT COUNT(*) AS count FROM bookings WHERE line_sent = 1"
        ).fetchone()["count"],
        "line_leads": conn.execute(
            "SELECT COUNT(*) AS count FROM line_leads"
        ).fetchone()["count"],
        "promotions": conn.execute(
            "SELECT COUNT(*) AS count FROM promotions"
        ).fetchone()["count"],
        "reviews": conn.execute(
            "SELECT COUNT(*) AS count FROM reviews"
        ).fetchone()["count"],
        "doctors": conn.execute(
            "SELECT COUNT(*) AS count FROM doctors"
        ).fetchone()["count"],
        "services": conn.execute(
            "SELECT COUNT(*) AS count FROM services"
        ).fetchone()["count"],
    }

    revenue_stats = get_revenue_stats()

    latest_bookings = conn.execute(
        """
        SELECT
            bookings.*,
            users.full_name,
            users.email,
            users.phone,
            doctors.name AS doctor_name
        FROM bookings
        JOIN users ON users.id = bookings.user_id
        LEFT JOIN doctors ON doctors.id = bookings.doctor_id
        ORDER BY bookings.created_at DESC
        LIMIT 10
        """
    ).fetchall()

    latest_line_leads = conn.execute(
        """
        SELECT *
        FROM line_leads
        ORDER BY created_at DESC
        LIMIT 10
        """
    ).fetchall()

    conn.close()

    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "user": admin,
            "stats": stats,
            "revenue_stats": revenue_stats,
            "bookings": latest_bookings,
            "line_leads": latest_line_leads,
        },
    )


@app.get("/admin/bookings")
def admin_bookings(request: Request):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/bookings", status_code=303)

    conn = get_db()
    bookings = conn.execute(
        """
        SELECT
            bookings.*,
            users.full_name,
            users.email,
            users.phone,
            doctors.name AS doctor_name
        FROM bookings
        JOIN users ON users.id = bookings.user_id
        LEFT JOIN doctors ON doctors.id = bookings.doctor_id
        ORDER BY bookings.appointment_date DESC, bookings.appointment_time DESC
        """
    ).fetchall()
    conn.close()

    return templates.TemplateResponse(
        "admin_bookings.html",
        {
            "request": request,
            "user": admin,
            "bookings": bookings,
        },
    )



@app.get("/admin/bookings/{booking_id}/edit")
def admin_edit_booking_page(request: Request, booking_id: int):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse(f"/login?next=/admin/bookings/{booking_id}/edit", status_code=303)

    conn = get_db()
    booking = conn.execute(
        """
        SELECT
            bookings.*,
            users.full_name AS customer_name,
            users.email AS customer_email,
            users.phone AS user_phone,
            doctors.name AS doctor_name
        FROM bookings
        JOIN users ON users.id = bookings.user_id
        LEFT JOIN doctors ON doctors.id = bookings.doctor_id
        WHERE bookings.id = ?
        """,
        (booking_id,),
    ).fetchone()

    doctors = conn.execute(
        """
        SELECT *
        FROM doctors
        WHERE is_active = 1
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    if not booking:
        return RedirectResponse("/admin/bookings?error=ไม่พบรายการจอง", status_code=303)

    return templates.TemplateResponse(
        "admin_edit_booking.html",
        {
            "request": request,
            "user": admin,
            "booking": booking,
            "doctors": doctors,
        },
    )


@app.post("/admin/bookings/{booking_id}/edit")
def admin_update_booking(
    request: Request,
    booking_id: int,
    customer_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    customer_phone: str = Form(""),
    package_name: str = Form(""),
    doctor_id: str = Form(""),
    appointment_date: str = Form(""),
    appointment_time: str = Form(""),
    note: str = Form(""),
    status: str = Form("PENDING"),
    revenue_amount: str = Form("0"),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse(f"/login?next=/admin/bookings/{booking_id}/edit", status_code=303)

    allowed_status = {"PENDING", "CONFIRMED", "DONE", "CANCELLED"}
    if status not in allowed_status:
        status = "PENDING"

    try:
        revenue_value = float(revenue_amount or 0)
    except ValueError:
        revenue_value = 0

    doctor_value = None
    if str(doctor_id).strip():
        try:
            doctor_value = int(doctor_id)
        except ValueError:
            doctor_value = None

    conn = get_db()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id = ?",
        (booking_id,),
    ).fetchone()

    if not booking:
        conn.close()
        return RedirectResponse("/admin/bookings?error=ไม่พบรายการจอง", status_code=303)

    conn.execute(
        """
        UPDATE users
        SET full_name = ?,
            email = ?,
            phone = ?
        WHERE id = ?
        """,
        (
            customer_name.strip() or "ลูกค้า",
            email.strip(),
            phone.strip(),
            booking["user_id"],
        ),
    )

    conn.execute(
        """
        UPDATE bookings
        SET customer_phone = ?,
            doctor_id = ?,
            package_name = ?,
            appointment_date = ?,
            appointment_time = ?,
            note = ?,
            status = ?,
            revenue_amount = ?
        WHERE id = ?
        """,
        (
            customer_phone.strip() or phone.strip(),
            doctor_value,
            package_name.strip() or "ไม่ระบุบริการ",
            appointment_date.strip(),
            appointment_time.strip(),
            note.strip(),
            status,
            revenue_value,
            booking_id,
        ),
    )

    conn.commit()
    conn.close()

    return RedirectResponse("/admin/bookings?success=แก้ไขรายการจองสำเร็จ", status_code=303)


@app.post("/admin/bookings/{booking_id}/status")
def admin_update_booking_status(
    request: Request,
    booking_id: int,
    status: str = Form("PENDING"),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/bookings", status_code=303)

    allowed_status = {"PENDING", "CONFIRMED", "DONE", "CANCELLED"}
    if status not in allowed_status:
        status = "PENDING"

    conn = get_db()
    conn.execute(
        "UPDATE bookings SET status = ? WHERE id = ?",
        (status, booking_id),
    )
    conn.commit()
    conn.close()

    return RedirectResponse("/admin/bookings?success=อัปเดตสถานะสำเร็จ", status_code=303)


@app.post("/admin/bookings/{booking_id}/delete")
def admin_delete_booking(request: Request, booking_id: int):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/bookings", status_code=303)

    conn = get_db()
    conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    conn.commit()
    conn.close()

    return RedirectResponse("/admin/bookings?success=ลบรายการจองสำเร็จ", status_code=303)


@app.get("/admin/line-leads")
def admin_line_leads(request: Request):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/line-leads", status_code=303)

    conn = get_db()
    leads = conn.execute(
        """
        SELECT *
        FROM line_leads
        ORDER BY created_at DESC
        """
    ).fetchall()
    conn.close()

    return templates.TemplateResponse(
        "admin_line_leads.html",
        {
            "request": request,
            "user": admin,
            "leads": leads,
        },
    )


@app.post("/admin/line-leads/{lead_id}/update")
def admin_update_line_lead(
    request: Request,
    lead_id: int,
    customer_name: str = Form(""),
    phone: str = Form(""),
    package_name: str = Form(""),
    appointment_date: str = Form(""),
    appointment_time: str = Form(""),
    status: str = Form("NEW"),
    note: str = Form(""),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/line-leads", status_code=303)

    conn = get_db()
    conn.execute(
        """
        UPDATE line_leads
        SET customer_name = ?,
            phone = ?,
            package_name = ?,
            appointment_date = ?,
            appointment_time = ?,
            status = ?,
            note = ?
        WHERE id = ?
        """,
        (
            customer_name,
            phone,
            package_name,
            appointment_date,
            appointment_time,
            status,
            note,
            lead_id,
        ),
    )
    conn.commit()
    conn.close()

    return RedirectResponse("/admin/line-leads", status_code=303)


@app.get("/admin/packages")
def admin_packages(request: Request):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/packages", status_code=303)

    conn = get_db()
    packages = conn.execute(
        "SELECT * FROM packages ORDER BY id DESC"
    ).fetchall()
    conn.close()

    messages = get_admin_messages(request)

    return templates.TemplateResponse(
        "admin_packages.html",
        {
            "request": request,
            "user": admin,
            "packages": packages,
            "success": messages["success"],
            "error": messages["error"],
        },
    )


@app.post("/admin/packages")
def admin_create_package(
    request: Request,
    name_th: str = Form(""),
    name_en: str = Form(""),
    description_th: str = Form(""),
    description_en: str = Form(""),
    slug: str = Form(...),
    price: str = Form(""),
    price_amount: float = Form(0),
    badge: str = Form(""),
    image_file_1: UploadFile = File(None),
    image_file_2: UploadFile = File(None),
    image_file_3: UploadFile = File(None),
    image_file_4: UploadFile = File(None),
    is_active: int = Form(1),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/packages", status_code=303)

    slug = normalize_slug(slug)

    if not name_th.strip() and not name_en.strip():
        return admin_redirect_with_message(
            "/admin/packages",
            error="กรุณาใส่ชื่อ Package ภาษาไทยหรืออังกฤษอย่างน้อย 1 ภาษา",
        )

    if not slug:
        return admin_redirect_with_message(
            "/admin/packages",
            error="กรุณาใส่ Slug เป็นภาษาอังกฤษ เช่น skin-booster",
        )

    try:
        name_i18n = ai_translate_5_languages(
            th_text=name_th,
            en_text=name_en,
        )

        desc_i18n = ai_translate_5_languages(
            th_text=description_th,
            en_text=description_en,
        )

        img1 = save_uploaded_image(PACKAGE_IMAGE_DIR, f"{slug}-1", image_file_1)
        img2 = save_uploaded_image(PACKAGE_IMAGE_DIR, f"{slug}-2", image_file_2)
        img3 = save_uploaded_image(PACKAGE_IMAGE_DIR, f"{slug}-3", image_file_3)
        img4 = save_uploaded_image(PACKAGE_IMAGE_DIR, f"{slug}-4", image_file_4)

        conn = get_db()

        exists = conn.execute(
            "SELECT id FROM packages WHERE slug = ?",
            (slug,),
        ).fetchone()

        if exists:
            conn.close()
            return admin_redirect_with_message(
                "/admin/packages",
                error=f"Slug '{slug}' มีอยู่แล้ว กรุณาเปลี่ยนเป็นชื่ออื่น",
            )

        conn.execute(
            """
            INSERT INTO packages (
                name,
                slug,
                description,
                price,
                price_amount,
                badge,
                image_file,
                image_file_1,
                image_file_2,
                image_file_3,
                image_file_4,
                is_active,
                created_at,

                name_th,
                name_en,
                name_zh,
                name_ja,
                name_ko,

                description_th,
                description_en,
                description_zh,
                description_ja,
                description_ko
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name_i18n["th"],
                slug,
                desc_i18n["th"],
                price.strip(),
                price_amount,
                badge.strip(),
                img1,
                img1,
                img2,
                img3,
                img4,
                is_active,
                datetime.now().isoformat(timespec="seconds"),

                name_i18n["th"],
                name_i18n["en"],
                name_i18n["zh"],
                name_i18n["ja"],
                name_i18n["ko"],

                desc_i18n["th"],
                desc_i18n["en"],
                desc_i18n["zh"],
                desc_i18n["ja"],
                desc_i18n["ko"],
            ),
        )

        conn.commit()
        conn.close()

        return admin_redirect_with_message(
            "/admin/packages",
            success="เพิ่ม Package และแปลภาษา AI สำเร็จ",
        )

    except Exception as e:
        print("PACKAGE CREATE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/packages",
            error=f"เพิ่ม Package ไม่สำเร็จ: {str(e)}",
        )


@app.post("/admin/packages/{package_id}/update")
def admin_update_package(
    request: Request,
    package_id: int,
    name_th: str = Form(""),
    name_en: str = Form(""),
    description_th: str = Form(""),
    description_en: str = Form(""),
    slug: str = Form(...),
    price: str = Form(""),
    price_amount: float = Form(0),
    badge: str = Form(""),
    image_file_1: UploadFile = File(None),
    image_file_2: UploadFile = File(None),
    image_file_3: UploadFile = File(None),
    image_file_4: UploadFile = File(None),
    existing_image_file_1: str = Form(""),
    existing_image_file_2: str = Form(""),
    existing_image_file_3: str = Form(""),
    existing_image_file_4: str = Form(""),
    remove_image_1: str = Form("0"),
    remove_image_2: str = Form("0"),
    remove_image_3: str = Form("0"),
    remove_image_4: str = Form("0"),
    is_active: int = Form(1),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/packages", status_code=303)

    slug = normalize_slug(slug)

    if not name_th.strip() and not name_en.strip():
        return admin_redirect_with_message(
            "/admin/packages",
            error="ชื่อ Package ภาษาไทยหรืออังกฤษ ห้ามว่างทั้งหมด",
        )

    if not slug:
        return admin_redirect_with_message("/admin/packages", error="Slug ห้ามว่าง")

    try:
        conn = get_db()

        exists = conn.execute(
            """
            SELECT id FROM packages
            WHERE slug = ? AND id != ?
            """,
            (slug, package_id),
        ).fetchone()

        if exists:
            conn.close()
            return admin_redirect_with_message(
                "/admin/packages",
                error=f"Slug '{slug}' ซ้ำกับ Package อื่น",
            )

        name_i18n = ai_translate_5_languages(
            th_text=name_th,
            en_text=name_en,
        )

        desc_i18n = ai_translate_5_languages(
            th_text=description_th,
            en_text=description_en,
        )

        img1 = "" if remove_image_1 == "1" else existing_image_file_1
        img2 = "" if remove_image_2 == "1" else existing_image_file_2
        img3 = "" if remove_image_3 == "1" else existing_image_file_3
        img4 = "" if remove_image_4 == "1" else existing_image_file_4

        new_img1 = save_uploaded_image(PACKAGE_IMAGE_DIR, f"{slug}-1", image_file_1)
        new_img2 = save_uploaded_image(PACKAGE_IMAGE_DIR, f"{slug}-2", image_file_2)
        new_img3 = save_uploaded_image(PACKAGE_IMAGE_DIR, f"{slug}-3", image_file_3)
        new_img4 = save_uploaded_image(PACKAGE_IMAGE_DIR, f"{slug}-4", image_file_4)

        if new_img1:
            img1 = new_img1
        if new_img2:
            img2 = new_img2
        if new_img3:
            img3 = new_img3
        if new_img4:
            img4 = new_img4

        conn.execute(
            """
            UPDATE packages
            SET name = ?,
                slug = ?,
                description = ?,
                price = ?,
                price_amount = ?,
                badge = ?,
                image_file = ?,
                image_file_1 = ?,
                image_file_2 = ?,
                image_file_3 = ?,
                image_file_4 = ?,
                is_active = ?,

                name_th = ?,
                name_en = ?,
                name_zh = ?,
                name_ja = ?,
                name_ko = ?,

                description_th = ?,
                description_en = ?,
                description_zh = ?,
                description_ja = ?,
                description_ko = ?
            WHERE id = ?
            """,
            (
                name_i18n["th"],
                slug,
                desc_i18n["th"],
                price.strip(),
                price_amount,
                badge.strip(),
                img1,
                img1,
                img2,
                img3,
                img4,
                is_active,

                name_i18n["th"],
                name_i18n["en"],
                name_i18n["zh"],
                name_i18n["ja"],
                name_i18n["ko"],

                desc_i18n["th"],
                desc_i18n["en"],
                desc_i18n["zh"],
                desc_i18n["ja"],
                desc_i18n["ko"],

                package_id,
            ),
        )

        conn.commit()
        conn.close()

        return admin_redirect_with_message(
            "/admin/packages",
            success="บันทึก Package และแปลภาษา AI สำเร็จ",
        )

    except Exception as e:
        print("PACKAGE UPDATE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/packages",
            error=f"บันทึก Package ไม่สำเร็จ: {str(e)}",
        )


@app.get("/admin/promotions")
def admin_promotions(request: Request):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/promotions", status_code=303)

    conn = get_db()
    promotions = conn.execute(
        "SELECT * FROM promotions ORDER BY id DESC"
    ).fetchall()
    conn.close()

    messages = get_admin_messages(request)

    return templates.TemplateResponse(
        "admin_promotions.html",
        {
            "request": request,
            "user": admin,
            "promotions": promotions,
            "success": messages["success"],
            "error": messages["error"],
        },
    )


@app.post("/admin/promotions")
def admin_create_promotion(
    request: Request,
    title_th: str = Form(""),
    title_en: str = Form(""),
    description_th: str = Form(""),
    description_en: str = Form(""),
    slug: str = Form(...),
    price: str = Form(""),
    badge: str = Form(""),
    image_file_1: UploadFile = File(None),
    image_file_2: UploadFile = File(None),
    image_file_3: UploadFile = File(None),
    image_file_4: UploadFile = File(None),
    is_active: int = Form(1),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/promotions", status_code=303)

    slug = normalize_slug(slug)

    if not title_th.strip() and not title_en.strip():
        return admin_redirect_with_message(
            "/admin/promotions",
            error="กรุณาใส่ชื่อ Promotion ภาษาไทยหรืออังกฤษอย่างน้อย 1 ภาษา",
        )

    if not slug:
        return admin_redirect_with_message("/admin/promotions", error="กรุณาใส่ Slug")

    try:
        title_i18n = ai_translate_5_languages(
            th_text=title_th,
            en_text=title_en,
        )

        desc_i18n = ai_translate_5_languages(
            th_text=description_th,
            en_text=description_en,
        )

        img1 = save_uploaded_image(PROMOTION_IMAGE_DIR, f"{slug}-1", image_file_1)
        img2 = save_uploaded_image(PROMOTION_IMAGE_DIR, f"{slug}-2", image_file_2)
        img3 = save_uploaded_image(PROMOTION_IMAGE_DIR, f"{slug}-3", image_file_3)
        img4 = save_uploaded_image(PROMOTION_IMAGE_DIR, f"{slug}-4", image_file_4)

        conn = get_db()

        exists = conn.execute(
            "SELECT id FROM promotions WHERE slug = ?",
            (slug,),
        ).fetchone()

        if exists:
            conn.close()
            return admin_redirect_with_message(
                "/admin/promotions",
                error=f"Slug '{slug}' มีอยู่แล้ว กรุณาเปลี่ยนชื่อ",
            )

        conn.execute(
            """
            INSERT INTO promotions (
                title,
                slug,
                description,
                price,
                badge,
                image_file_1,
                image_file_2,
                image_file_3,
                image_file_4,
                is_active,
                created_at,

                title_th,
                title_en,
                title_zh,
                title_ja,
                title_ko,

                description_th,
                description_en,
                description_zh,
                description_ja,
                description_ko
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title_i18n["th"],
                slug,
                desc_i18n["th"],
                price.strip(),
                badge.strip(),
                img1,
                img2,
                img3,
                img4,
                is_active,
                datetime.now().isoformat(timespec="seconds"),

                title_i18n["th"],
                title_i18n["en"],
                title_i18n["zh"],
                title_i18n["ja"],
                title_i18n["ko"],

                desc_i18n["th"],
                desc_i18n["en"],
                desc_i18n["zh"],
                desc_i18n["ja"],
                desc_i18n["ko"],
            ),
        )

        conn.commit()
        conn.close()

        return admin_redirect_with_message(
            "/admin/promotions",
            success="เพิ่ม Promotion และแปลภาษา AI สำเร็จ",
        )

    except Exception as e:
        print("PROMOTION CREATE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/promotions",
            error=f"เพิ่ม Promotion ไม่สำเร็จ: {str(e)}",
        )


@app.post("/admin/promotions/{promotion_id}/update")
def admin_update_promotion(
    request: Request,
    promotion_id: int,
    title_th: str = Form(""),
    title_en: str = Form(""),
    description_th: str = Form(""),
    description_en: str = Form(""),
    slug: str = Form(...),
    price: str = Form(""),
    badge: str = Form(""),
    image_file_1: UploadFile = File(None),
    image_file_2: UploadFile = File(None),
    image_file_3: UploadFile = File(None),
    image_file_4: UploadFile = File(None),
    existing_image_file_1: str = Form(""),
    existing_image_file_2: str = Form(""),
    existing_image_file_3: str = Form(""),
    existing_image_file_4: str = Form(""),
    remove_image_1: str = Form("0"),
    remove_image_2: str = Form("0"),
    remove_image_3: str = Form("0"),
    remove_image_4: str = Form("0"),
    is_active: int = Form(1),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/promotions", status_code=303)

    slug = normalize_slug(slug)

    if not title_th.strip() and not title_en.strip():
        return admin_redirect_with_message(
            "/admin/promotions",
            error="ชื่อ Promotion ภาษาไทยหรืออังกฤษ ห้ามว่างทั้งหมด",
        )

    if not slug:
        return admin_redirect_with_message("/admin/promotions", error="Slug ห้ามว่าง")

    try:
        conn = get_db()

        exists = conn.execute(
            """
            SELECT id FROM promotions
            WHERE slug = ? AND id != ?
            """,
            (slug, promotion_id),
        ).fetchone()

        if exists:
            conn.close()
            return admin_redirect_with_message(
                "/admin/promotions",
                error=f"Slug '{slug}' ซ้ำกับ Promotion อื่น",
            )

        title_i18n = ai_translate_5_languages(
            th_text=title_th,
            en_text=title_en,
        )

        desc_i18n = ai_translate_5_languages(
            th_text=description_th,
            en_text=description_en,
        )

        img1 = "" if remove_image_1 == "1" else existing_image_file_1
        img2 = "" if remove_image_2 == "1" else existing_image_file_2
        img3 = "" if remove_image_3 == "1" else existing_image_file_3
        img4 = "" if remove_image_4 == "1" else existing_image_file_4

        new_img1 = save_uploaded_image(PROMOTION_IMAGE_DIR, f"{slug}-1", image_file_1)
        new_img2 = save_uploaded_image(PROMOTION_IMAGE_DIR, f"{slug}-2", image_file_2)
        new_img3 = save_uploaded_image(PROMOTION_IMAGE_DIR, f"{slug}-3", image_file_3)
        new_img4 = save_uploaded_image(PROMOTION_IMAGE_DIR, f"{slug}-4", image_file_4)

        if new_img1:
            img1 = new_img1
        if new_img2:
            img2 = new_img2
        if new_img3:
            img3 = new_img3
        if new_img4:
            img4 = new_img4

        conn.execute(
            """
            UPDATE promotions
            SET title = ?,
                slug = ?,
                description = ?,
                price = ?,
                badge = ?,
                image_file_1 = ?,
                image_file_2 = ?,
                image_file_3 = ?,
                image_file_4 = ?,
                is_active = ?,

                title_th = ?,
                title_en = ?,
                title_zh = ?,
                title_ja = ?,
                title_ko = ?,

                description_th = ?,
                description_en = ?,
                description_zh = ?,
                description_ja = ?,
                description_ko = ?
            WHERE id = ?
            """,
            (
                title_i18n["th"],
                slug,
                desc_i18n["th"],
                price.strip(),
                badge.strip(),
                img1,
                img2,
                img3,
                img4,
                is_active,

                title_i18n["th"],
                title_i18n["en"],
                title_i18n["zh"],
                title_i18n["ja"],
                title_i18n["ko"],

                desc_i18n["th"],
                desc_i18n["en"],
                desc_i18n["zh"],
                desc_i18n["ja"],
                desc_i18n["ko"],

                promotion_id,
            ),
        )

        conn.commit()
        conn.close()

        return admin_redirect_with_message(
            "/admin/promotions",
            success="บันทึก Promotion และแปลภาษา AI สำเร็จ",
        )

    except Exception as e:
        print("PROMOTION UPDATE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/promotions",
            error=f"บันทึก Promotion ไม่สำเร็จ: {str(e)}",
        )


@app.get("/admin/reviews")
def admin_reviews(request: Request):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/reviews", status_code=303)

    conn = get_db()
    reviews = conn.execute(
        "SELECT * FROM reviews ORDER BY id DESC"
    ).fetchall()
    conn.close()

    messages = get_admin_messages(request)

    return templates.TemplateResponse(
        "admin_reviews.html",
        {
            "request": request,
            "user": admin,
            "reviews": reviews,
            "success": messages["success"],
            "error": messages["error"],
        },
    )


@app.post("/admin/reviews")
def admin_create_review(
    request: Request,
    customer_name: str = Form(...),
    review_text: str = Form(...),
    rating: int = Form(5),
    image_file: UploadFile = File(None),
    is_active: int = Form(1),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/reviews", status_code=303)

    if not customer_name.strip():
        return admin_redirect_with_message("/admin/reviews", error="กรุณาใส่ชื่อ Review")

    if not review_text.strip():
        return admin_redirect_with_message("/admin/reviews", error="กรุณาใส่ข้อความ Review")

    try:
        img = save_uploaded_image(REVIEW_IMAGE_DIR, customer_name, image_file)

        conn = get_db()
        conn.execute(
            """
            INSERT INTO reviews (
                customer_name,
                review_text,
                rating,
                image_file,
                is_active,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                customer_name.strip(),
                review_text.strip(),
                rating,
                img,
                is_active,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        conn.close()

        return admin_redirect_with_message("/admin/reviews", success="เพิ่ม Review สำเร็จ")

    except Exception as e:
        print("REVIEW CREATE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/reviews",
            error=f"เพิ่ม Review ไม่สำเร็จ: {str(e)}",
        )


@app.post("/admin/reviews/{review_id}/update")
def admin_update_review(
    request: Request,
    review_id: int,
    customer_name: str = Form(...),
    review_text: str = Form(...),
    rating: int = Form(5),
    image_file: UploadFile = File(None),
    existing_image_file: str = Form(""),
    is_active: int = Form(1),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/reviews", status_code=303)

    if not customer_name.strip():
        return admin_redirect_with_message("/admin/reviews", error="ชื่อ Review ห้ามว่าง")

    if not review_text.strip():
        return admin_redirect_with_message("/admin/reviews", error="ข้อความ Review ห้ามว่าง")

    try:
        img = save_uploaded_image(REVIEW_IMAGE_DIR, customer_name, image_file) or existing_image_file

        conn = get_db()
        conn.execute(
            """
            UPDATE reviews
            SET customer_name = ?,
                review_text = ?,
                rating = ?,
                image_file = ?,
                is_active = ?
            WHERE id = ?
            """,
            (
                customer_name.strip(),
                review_text.strip(),
                rating,
                img,
                is_active,
                review_id,
            ),
        )
        conn.commit()
        conn.close()

        return admin_redirect_with_message("/admin/reviews", success="บันทึก Review สำเร็จ")

    except Exception as e:
        print("REVIEW UPDATE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/reviews",
            error=f"บันทึก Review ไม่สำเร็จ: {str(e)}",
        )


@app.get("/admin/doctors")
def admin_doctors(request: Request):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/doctors", status_code=303)

    conn = get_db()
    doctors = conn.execute(
        "SELECT * FROM doctors ORDER BY id DESC"
    ).fetchall()
    conn.close()

    messages = get_admin_messages(request)

    return templates.TemplateResponse(
        "admin_doctors.html",
        {
            "request": request,
            "user": admin,
            "doctors": doctors,
            "success": messages["success"],
            "error": messages["error"],
        },
    )


@app.post("/admin/doctors")
def admin_create_doctor(
    request: Request,
    name: str = Form(...),
    specialty: str = Form(""),
    expertise: str = Form(""),
    phone: str = Form(""),
    image_file: UploadFile = File(None),
    is_active: int = Form(1),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/doctors", status_code=303)

    if not name.strip():
        return admin_redirect_with_message("/admin/doctors", error="กรุณาใส่ชื่อคุณหมอ")

    try:
        name_i18n = ai_translate_5_languages(th_text=name, en_text="")
        specialty_i18n = ai_translate_5_languages(th_text=specialty, en_text="")
        expertise_i18n = ai_translate_5_languages(th_text=expertise, en_text="")

        img = save_uploaded_image(DOCTOR_IMAGE_DIR, f"doctor-{name}", image_file)

        conn = get_db()
        conn.execute(
            """
            INSERT INTO doctors (
                name,
                specialty,
                expertise,
                phone,
                image_file,
                is_active,
                created_at,

                name_th,
                name_en,
                name_zh,
                name_ja,
                name_ko,

                specialty_th,
                specialty_en,
                specialty_zh,
                specialty_ja,
                specialty_ko,

                expertise_th,
                expertise_en,
                expertise_zh,
                expertise_ja,
                expertise_ko
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name_i18n["th"],
                specialty_i18n["th"],
                expertise_i18n["th"],
                phone.strip(),
                img,
                is_active,
                datetime.now().isoformat(timespec="seconds"),

                name_i18n["th"],
                name_i18n["en"],
                name_i18n["zh"],
                name_i18n["ja"],
                name_i18n["ko"],

                specialty_i18n["th"],
                specialty_i18n["en"],
                specialty_i18n["zh"],
                specialty_i18n["ja"],
                specialty_i18n["ko"],

                expertise_i18n["th"],
                expertise_i18n["en"],
                expertise_i18n["zh"],
                expertise_i18n["ja"],
                expertise_i18n["ko"],
            ),
        )

        conn.commit()
        conn.close()

        return admin_redirect_with_message("/admin/doctors", success="เพิ่มคุณหมอและแปลภาษา AI สำเร็จ")

    except Exception as e:
        print("DOCTOR CREATE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/doctors",
            error=f"เพิ่มคุณหมอไม่สำเร็จ: {str(e)}",
        )


@app.post("/admin/doctors/{doctor_id}/update")
def admin_update_doctor(
    request: Request,
    doctor_id: int,
    name: str = Form(...),
    specialty: str = Form(""),
    expertise: str = Form(""),
    phone: str = Form(""),
    image_file: UploadFile = File(None),
    existing_image_file: str = Form(""),
    remove_image: str = Form("0"),
    is_active: int = Form(1),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/doctors", status_code=303)

    if not name.strip():
        return admin_redirect_with_message("/admin/doctors", error="ชื่อคุณหมอห้ามว่าง")

    try:
        name_i18n = ai_translate_5_languages(th_text=name, en_text="")
        specialty_i18n = ai_translate_5_languages(th_text=specialty, en_text="")
        expertise_i18n = ai_translate_5_languages(th_text=expertise, en_text="")

        img = "" if remove_image == "1" else existing_image_file
        new_img = save_uploaded_image(DOCTOR_IMAGE_DIR, f"doctor-{name}", image_file)

        if new_img:
            img = new_img

        conn = get_db()
        conn.execute(
            """
            UPDATE doctors
            SET name = ?,
                specialty = ?,
                expertise = ?,
                phone = ?,
                image_file = ?,
                is_active = ?,

                name_th = ?,
                name_en = ?,
                name_zh = ?,
                name_ja = ?,
                name_ko = ?,

                specialty_th = ?,
                specialty_en = ?,
                specialty_zh = ?,
                specialty_ja = ?,
                specialty_ko = ?,

                expertise_th = ?,
                expertise_en = ?,
                expertise_zh = ?,
                expertise_ja = ?,
                expertise_ko = ?
            WHERE id = ?
            """,
            (
                name_i18n["th"],
                specialty_i18n["th"],
                expertise_i18n["th"],
                phone.strip(),
                img,
                is_active,

                name_i18n["th"],
                name_i18n["en"],
                name_i18n["zh"],
                name_i18n["ja"],
                name_i18n["ko"],

                specialty_i18n["th"],
                specialty_i18n["en"],
                specialty_i18n["zh"],
                specialty_i18n["ja"],
                specialty_i18n["ko"],

                expertise_i18n["th"],
                expertise_i18n["en"],
                expertise_i18n["zh"],
                expertise_i18n["ja"],
                expertise_i18n["ko"],

                doctor_id,
            ),
        )

        conn.commit()
        conn.close()

        return admin_redirect_with_message("/admin/doctors", success="บันทึกข้อมูลคุณหมอและแปลภาษา AI สำเร็จ")

    except Exception as e:
        print("DOCTOR UPDATE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/doctors",
            error=f"บันทึกข้อมูลคุณหมอไม่สำเร็จ: {str(e)}",
        )


@app.post("/admin/doctors/{doctor_id}/delete")
def admin_delete_doctor(
    request: Request,
    doctor_id: int,
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/doctors", status_code=303)

    try:
        conn = get_db()

        conn.execute(
            "DELETE FROM doctor_schedules WHERE doctor_id = ?",
            (doctor_id,),
        )

        conn.execute(
            "DELETE FROM doctors WHERE id = ?",
            (doctor_id,),
        )

        conn.commit()
        conn.close()

        return admin_redirect_with_message("/admin/doctors", success="ลบคุณหมอสำเร็จ")

    except Exception as e:
        print("DOCTOR DELETE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/doctors",
            error=f"ลบคุณหมอไม่สำเร็จ: {str(e)}",
        )




def save_uploaded_video(folder_path: str, prefix: str, video_file: UploadFile | None):
    if not video_file or not video_file.filename:
        return None

    original_name = video_file.filename.lower()
    allowed = (".mp4", ".webm", ".mov")

    if not original_name.endswith(allowed):
        return None

    ext = os.path.splitext(original_name)[1].lower()
    safe_prefix = safe_filename(prefix)
    filename = f"{safe_prefix}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}{ext}"
    file_path = os.path.join(folder_path, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(video_file.file, buffer)

    return filename


@app.get("/admin/services")
def admin_services(request: Request):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/services", status_code=303)

    conn = get_db()
    services = conn.execute(
        "SELECT * FROM services ORDER BY id DESC"
    ).fetchall()

    programs = conn.execute(
        """
        SELECT *
        FROM service_programs
        ORDER BY service_id ASC, sort_order ASC, id ASC
        """
    ).fetchall()

    media_rows = conn.execute(
        """
        SELECT *
        FROM program_media
        ORDER BY program_id ASC, sort_order ASC, id ASC
        """
    ).fetchall()
    conn.close()

    programs_by_service = {}
    for row in programs:
        item = dict(row)
        if item.get("cover_image_file"):
            item["cover_image_url"] = f"/static/images/services/{item['cover_image_file']}"
        else:
            item["cover_image_url"] = ""
        programs_by_service.setdefault(item["service_id"], []).append(item)

    media_by_program = {}
    for row in media_rows:
        item = dict(row)
        if item.get("image_file"):
            item["image_url"] = f"/static/images/services/{item['image_file']}"
        else:
            item["image_url"] = ""

        if item.get("video_file"):
            item["video_src"] = f"/static/videos/services/{item['video_file']}"
        else:
            item["video_src"] = item.get("video_url") or ""

        media_by_program.setdefault(item["program_id"], []).append(item)

    messages = get_admin_messages(request)

    return templates.TemplateResponse(
        "admin_services.html",
        {
            "request": request,
            "user": admin,
            "services": services,
            "programs_by_service": programs_by_service,
            "media_by_program": media_by_program,
            "success": messages["success"],
            "error": messages["error"],
        },
    )


@app.post("/admin/services")
def admin_create_service(
    request: Request,
    title_th: str = Form(""),
    title_en: str = Form(""),
    description_th: str = Form(""),
    description_en: str = Form(""),
    detail_th: str = Form(""),
    detail_en: str = Form(""),
    slug: str = Form(...),
    image_file: UploadFile = File(None),
    is_active: int = Form(1),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/services", status_code=303)

    slug = normalize_slug(slug)

    if not title_th.strip() and not title_en.strip():
        return admin_redirect_with_message(
            "/admin/services",
            error="กรุณาใส่ชื่อหมวดบริการภาษาไทยหรืออังกฤษอย่างน้อย 1 ภาษา",
        )

    if not slug:
        return admin_redirect_with_message(
            "/admin/services",
            error="กรุณาใส่ Slug เป็นภาษาอังกฤษ เช่น skin-treatment",
        )

    try:
        title_i18n = ai_translate_5_languages(th_text=title_th, en_text=title_en)
        desc_i18n = ai_translate_5_languages(th_text=description_th, en_text=description_en)
        detail_i18n = ai_translate_5_languages(th_text=detail_th, en_text=detail_en)
        img = save_uploaded_image(SERVICE_IMAGE_DIR, f"service-{slug}", image_file)

        conn = get_db()
        exists = conn.execute("SELECT id FROM services WHERE slug = ?", (slug,)).fetchone()
        if exists:
            conn.close()
            return admin_redirect_with_message("/admin/services", error=f"Slug '{slug}' มีอยู่แล้ว กรุณาเปลี่ยนชื่อ")

        conn.execute(
            """
            INSERT INTO services (
                title, slug, description, detail, image_file, is_active, created_at,
                title_th, title_en, title_zh, title_ja, title_ko,
                description_th, description_en, description_zh, description_ja, description_ko,
                detail_th, detail_en, detail_zh, detail_ja, detail_ko
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title_i18n["th"], slug, desc_i18n["th"], detail_i18n["th"], img,
                is_active, datetime.now().isoformat(timespec="seconds"),
                title_i18n["th"], title_i18n["en"], title_i18n["zh"], title_i18n["ja"], title_i18n["ko"],
                desc_i18n["th"], desc_i18n["en"], desc_i18n["zh"], desc_i18n["ja"], desc_i18n["ko"],
                detail_i18n["th"], detail_i18n["en"], detail_i18n["zh"], detail_i18n["ja"], detail_i18n["ko"],
            ),
        )
        conn.commit()
        conn.close()

        return admin_redirect_with_message("/admin/services", success="เพิ่มหมวดบริการสำเร็จ")

    except Exception as e:
        print("SERVICE CREATE ERROR:", repr(e))
        return admin_redirect_with_message("/admin/services", error=f"เพิ่มหมวดบริการไม่สำเร็จ: {str(e)}")


@app.post("/admin/services/{service_id}/update")
def admin_update_service(
    request: Request,
    service_id: int,
    title_th: str = Form(""),
    title_en: str = Form(""),
    description_th: str = Form(""),
    description_en: str = Form(""),
    detail_th: str = Form(""),
    detail_en: str = Form(""),
    slug: str = Form(...),
    image_file: UploadFile = File(None),
    existing_image_file: str = Form(""),
    remove_image: str = Form("0"),
    is_active: int = Form(1),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/services", status_code=303)

    slug = normalize_slug(slug)

    if not title_th.strip() and not title_en.strip():
        return admin_redirect_with_message("/admin/services", error="ชื่อหมวดบริการห้ามว่างทั้งหมด")

    if not slug:
        return admin_redirect_with_message("/admin/services", error="Slug ห้ามว่าง")

    try:
        conn = get_db()
        exists = conn.execute(
            "SELECT id FROM services WHERE slug = ? AND id != ?",
            (slug, service_id),
        ).fetchone()

        if exists:
            conn.close()
            return admin_redirect_with_message("/admin/services", error=f"Slug '{slug}' ซ้ำกับหมวดบริการอื่น")

        title_i18n = ai_translate_5_languages(th_text=title_th, en_text=title_en)
        desc_i18n = ai_translate_5_languages(th_text=description_th, en_text=description_en)
        detail_i18n = ai_translate_5_languages(th_text=detail_th, en_text=detail_en)

        img = "" if remove_image == "1" else existing_image_file
        new_img = save_uploaded_image(SERVICE_IMAGE_DIR, f"service-{slug}", image_file)
        if new_img:
            img = new_img

        conn.execute(
            """
            UPDATE services
            SET title = ?, slug = ?, description = ?, detail = ?, image_file = ?, is_active = ?,
                title_th = ?, title_en = ?, title_zh = ?, title_ja = ?, title_ko = ?,
                description_th = ?, description_en = ?, description_zh = ?, description_ja = ?, description_ko = ?,
                detail_th = ?, detail_en = ?, detail_zh = ?, detail_ja = ?, detail_ko = ?
            WHERE id = ?
            """,
            (
                title_i18n["th"], slug, desc_i18n["th"], detail_i18n["th"], img, is_active,
                title_i18n["th"], title_i18n["en"], title_i18n["zh"], title_i18n["ja"], title_i18n["ko"],
                desc_i18n["th"], desc_i18n["en"], desc_i18n["zh"], desc_i18n["ja"], desc_i18n["ko"],
                detail_i18n["th"], detail_i18n["en"], detail_i18n["zh"], detail_i18n["ja"], detail_i18n["ko"],
                service_id,
            ),
        )
        conn.commit()
        conn.close()
        return admin_redirect_with_message("/admin/services", success="บันทึกหมวดบริการสำเร็จ")

    except Exception as e:
        print("SERVICE UPDATE ERROR:", repr(e))
        return admin_redirect_with_message("/admin/services", error=f"บันทึกหมวดบริการไม่สำเร็จ: {str(e)}")


@app.post("/admin/services/{service_id}/programs")
def admin_create_service_program(
    request: Request,
    service_id: int,
    title: str = Form(...),
    slug: str = Form(""),
    short_description: str = Form(""),
    detail: str = Form(""),
    cover_image_file: UploadFile = File(None),
    sort_order: int = Form(0),
    is_active: int = Form(1),
):
    admin = require_admin(request)
    if not admin:
        return RedirectResponse("/login?next=/admin/services", status_code=303)

    title = title.strip()
    if not title:
        return admin_redirect_with_message("/admin/services", error="กรุณาใส่ชื่อ Program")

    slug = normalize_slug(slug or title)

    try:
        cover = save_uploaded_image(SERVICE_IMAGE_DIR, f"program-{slug}", cover_image_file)
        conn = get_db()
        conn.execute(
            """
            INSERT INTO service_programs (
                service_id, title, slug, short_description, detail,
                cover_image_file, sort_order, is_active, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                service_id, title, slug, short_description.strip(), detail.strip(),
                cover or "", sort_order, is_active,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        conn.close()
        return admin_redirect_with_message("/admin/services", success="เพิ่ม Program สำเร็จ")

    except Exception as e:
        print("PROGRAM CREATE ERROR:", repr(e))
        return admin_redirect_with_message("/admin/services", error=f"เพิ่ม Program ไม่สำเร็จ: {str(e)}")


@app.post("/admin/service-programs/{program_id}/update")
def admin_update_service_program(
    request: Request,
    program_id: int,
    title: str = Form(...),
    slug: str = Form(""),
    short_description: str = Form(""),
    detail: str = Form(""),
    cover_image_file: UploadFile = File(None),
    existing_cover_image_file: str = Form(""),
    remove_cover: str = Form("0"),
    sort_order: int = Form(0),
    is_active: int = Form(1),
):
    admin = require_admin(request)
    if not admin:
        return RedirectResponse("/login?next=/admin/services", status_code=303)

    title = title.strip()
    if not title:
        return admin_redirect_with_message("/admin/services", error="ชื่อ Program ห้ามว่าง")

    slug = normalize_slug(slug or title)

    try:
        cover = "" if remove_cover == "1" else existing_cover_image_file
        new_cover = save_uploaded_image(SERVICE_IMAGE_DIR, f"program-{slug}", cover_image_file)
        if new_cover:
            cover = new_cover

        conn = get_db()
        conn.execute(
            """
            UPDATE service_programs
            SET title = ?, slug = ?, short_description = ?, detail = ?,
                cover_image_file = ?, sort_order = ?, is_active = ?
            WHERE id = ?
            """,
            (
                title, slug, short_description.strip(), detail.strip(),
                cover, sort_order, is_active, program_id,
            ),
        )
        conn.commit()
        conn.close()
        return admin_redirect_with_message("/admin/services", success="บันทึก Program สำเร็จ")

    except Exception as e:
        print("PROGRAM UPDATE ERROR:", repr(e))
        return admin_redirect_with_message("/admin/services", error=f"บันทึก Program ไม่สำเร็จ: {str(e)}")


@app.post("/admin/service-programs/{program_id}/media")
def admin_add_program_media(
    request: Request,
    program_id: int,
    media_type: str = Form("image"),
    image_file: UploadFile = File(None),
    video_file: UploadFile = File(None),
    video_url: str = Form(""),
    caption: str = Form(""),
    sort_order: int = Form(0),
    is_active: int = Form(1),
):
    admin = require_admin(request)
    if not admin:
        return RedirectResponse("/login?next=/admin/services", status_code=303)

    try:
        image_name = save_uploaded_image(SERVICE_IMAGE_DIR, f"program-media-{program_id}", image_file)
        video_name = save_uploaded_video(SERVICE_VIDEO_DIR, f"program-video-{program_id}", video_file)
        video_url = (video_url or "").strip()

        if image_name:
            media_type = "image"
        elif video_name or video_url:
            media_type = "video"
        else:
            return admin_redirect_with_message("/admin/services", error="กรุณาอัปโหลดรูป/วิดีโอ หรือใส่ลิงก์วิดีโอก่อน")

        conn = get_db()
        conn.execute(
            """
            INSERT INTO program_media (
                program_id, media_type, image_file, video_file, video_url,
                caption, sort_order, is_active, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                program_id, media_type, image_name or "", video_name or "",
                video_url, caption.strip(), sort_order, is_active,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        conn.close()
        return admin_redirect_with_message("/admin/services", success="เพิ่มรูป/วิดีโอให้ Program สำเร็จ")

    except Exception as e:
        print("PROGRAM MEDIA CREATE ERROR:", repr(e))
        return admin_redirect_with_message("/admin/services", error=f"เพิ่มรูป/วิดีโอไม่สำเร็จ: {str(e)}")


@app.post("/admin/program-media/{media_id}/update")
def admin_update_program_media(
    request: Request,
    media_id: int,
    caption: str = Form(""),
    sort_order: int = Form(0),
    is_active: int = Form(1),
):
    admin = require_admin(request)
    if not admin:
        return RedirectResponse("/login?next=/admin/services", status_code=303)

    conn = get_db()
    conn.execute(
        """
        UPDATE program_media
        SET caption = ?, sort_order = ?, is_active = ?
        WHERE id = ?
        """,
        (caption.strip(), sort_order, is_active, media_id),
    )
    conn.commit()
    conn.close()
    return admin_redirect_with_message("/admin/services", success="บันทึกรูป/วิดีโอสำเร็จ")


@app.post("/admin/program-media/{media_id}/delete")
def admin_delete_program_media(request: Request, media_id: int):
    admin = require_admin(request)
    if not admin:
        return RedirectResponse("/login?next=/admin/services", status_code=303)

    conn = get_db()
    conn.execute("DELETE FROM program_media WHERE id = ?", (media_id,))
    conn.commit()
    conn.close()
    return admin_redirect_with_message("/admin/services", success="ลบรูป/วิดีโอสำเร็จ")


@app.post("/admin/service-programs/{program_id}/delete")
def admin_delete_service_program(request: Request, program_id: int):
    admin = require_admin(request)
    if not admin:
        return RedirectResponse("/login?next=/admin/services", status_code=303)

    conn = get_db()
    conn.execute("DELETE FROM program_media WHERE program_id = ?", (program_id,))
    conn.execute("DELETE FROM service_programs WHERE id = ?", (program_id,))
    conn.commit()
    conn.close()
    return admin_redirect_with_message("/admin/services", success="ลบ Program สำเร็จ")


@app.post("/admin/services/{service_id}/delete")
def admin_delete_service(request: Request, service_id: int):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/services", status_code=303)

    try:
        conn = get_db()
        program_ids = [row["id"] for row in conn.execute("SELECT id FROM service_programs WHERE service_id = ?", (service_id,)).fetchall()]
        for pid in program_ids:
            conn.execute("DELETE FROM program_media WHERE program_id = ?", (pid,))
        conn.execute("DELETE FROM service_programs WHERE service_id = ?", (service_id,))
        conn.execute("DELETE FROM service_media WHERE service_id = ?", (service_id,))
        conn.execute("DELETE FROM services WHERE id = ?", (service_id,))
        conn.commit()
        conn.close()
        return admin_redirect_with_message("/admin/services", success="ลบหมวดบริการสำเร็จ")

    except Exception as e:
        print("SERVICE DELETE ERROR:", repr(e))
        return admin_redirect_with_message("/admin/services", error=f"ลบหมวดบริการไม่สำเร็จ: {str(e)}")



# =========================================================
# ADMIN HOMECARE CHANNEL

@app.get("/admin/homecare-channel")
def admin_homecare_channel(request: Request):
    admin = require_admin(request)
    if not admin:
        return RedirectResponse("/login?next=/admin/homecare-channel", status_code=303)

    conn = get_db()
    channels = conn.execute(
        """
        SELECT * FROM homecare_channels
        ORDER BY sort_order ASC, id DESC
        """
    ).fetchall()
    conn.close()

    messages = get_admin_messages(request)
    return templates.TemplateResponse(
        "admin_homecare_channel.html",
        {
            "request": request,
            "user": admin,
            "channels": channels,
            "success": messages["success"],
            "error": messages["error"],
        },
    )


@app.post("/admin/homecare-channel")
def admin_create_homecare_channel(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    video_url: str = Form(...),
    sort_order: int = Form(0),
    is_active: str = Form("1"),
    cover_image: UploadFile = File(None),
):
    admin = require_admin(request)
    if not admin:
        return RedirectResponse("/login?next=/admin/homecare-channel", status_code=303)

    try:
        title = (title or "").strip()
        video_url = (video_url or "").strip()
        if not title or not video_url:
            return admin_redirect_with_message(
                "/admin/homecare-channel",
                error="กรุณาใส่ชื่อคลิปและลิงก์วิดีโอ",
            )

        cover_image_file = save_uploaded_image(
            CHANNEL_IMAGE_DIR,
            f"channel-{title}",
            cover_image,
        )
        platform = detect_video_platform(video_url)

        conn = get_db()
        conn.execute(
            """
            INSERT INTO homecare_channels (
                title, description, video_url, platform,
                cover_image_file, is_active, sort_order, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                description,
                video_url,
                platform,
                cover_image_file or "",
                1 if is_active == "1" else 0,
                sort_order,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        conn.close()

        return admin_redirect_with_message(
            "/admin/homecare-channel",
            success="เพิ่มคลิป HomeCare Channel สำเร็จ",
        )

    except Exception as e:
        print("HOMECARE CHANNEL CREATE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/homecare-channel",
            error=f"เพิ่มคลิปไม่สำเร็จ: {str(e)}",
        )


@app.post("/admin/homecare-channel/{channel_id}/update")
def admin_update_homecare_channel(
    request: Request,
    channel_id: int,
    title: str = Form(...),
    description: str = Form(""),
    video_url: str = Form(...),
    sort_order: int = Form(0),
    is_active: str = Form("0"),
    cover_image: UploadFile = File(None),
):
    admin = require_admin(request)
    if not admin:
        return RedirectResponse("/login?next=/admin/homecare-channel", status_code=303)

    try:
        title = (title or "").strip()
        video_url = (video_url or "").strip()
        if not title or not video_url:
            return admin_redirect_with_message(
                "/admin/homecare-channel",
                error="กรุณาใส่ชื่อคลิปและลิงก์วิดีโอ",
            )

        conn = get_db()
        current = conn.execute(
            "SELECT * FROM homecare_channels WHERE id = ?",
            (channel_id,),
        ).fetchone()

        if not current:
            conn.close()
            return admin_redirect_with_message(
                "/admin/homecare-channel",
                error="ไม่พบคลิปนี้",
            )

        cover_image_file = current["cover_image_file"] or ""
        new_cover = save_uploaded_image(CHANNEL_IMAGE_DIR, f"channel-{title}", cover_image)
        if new_cover:
            cover_image_file = new_cover

        conn.execute(
            """
            UPDATE homecare_channels
            SET title = ?, description = ?, video_url = ?, platform = ?,
                cover_image_file = ?, is_active = ?, sort_order = ?
            WHERE id = ?
            """,
            (
                title,
                description,
                video_url,
                detect_video_platform(video_url),
                cover_image_file,
                1 if is_active == "1" else 0,
                sort_order,
                channel_id,
            ),
        )
        conn.commit()
        conn.close()

        return admin_redirect_with_message(
            "/admin/homecare-channel",
            success="อัปเดตคลิปสำเร็จ",
        )

    except Exception as e:
        print("HOMECARE CHANNEL UPDATE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/homecare-channel",
            error=f"อัปเดตคลิปไม่สำเร็จ: {str(e)}",
        )


@app.post("/admin/homecare-channel/{channel_id}/delete")
def admin_delete_homecare_channel(request: Request, channel_id: int):
    admin = require_admin(request)
    if not admin:
        return RedirectResponse("/login?next=/admin/homecare-channel", status_code=303)

    try:
        conn = get_db()
        conn.execute("DELETE FROM homecare_channels WHERE id = ?", (channel_id,))
        conn.commit()
        conn.close()
        return admin_redirect_with_message(
            "/admin/homecare-channel",
            success="ลบคลิปสำเร็จ",
        )
    except Exception as e:
        print("HOMECARE CHANNEL DELETE ERROR:", repr(e))
        return admin_redirect_with_message(
            "/admin/homecare-channel",
            error=f"ลบคลิปไม่สำเร็จ: {str(e)}",
        )


# ADMIN WEBSITE ENTRY POPUP
# ให้ Admin อัปโหลดรูป Popup เองได้จาก /admin/popup
# =========================================================
import base64


def ensure_entry_popup_table():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entry_popup_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            is_active INTEGER DEFAULT 0,
            show_once INTEGER DEFAULT 1,
            delay_ms INTEGER DEFAULT 500,
            image_data_url TEXT,
            image_filename TEXT,
            updated_at TEXT
        )
        """
    )

    row = conn.execute("SELECT id FROM entry_popup_settings WHERE id = 1").fetchone()
    if not row:
        conn.execute(
            """
            INSERT INTO entry_popup_settings (
                id,
                is_active,
                show_once,
                delay_ms,
                image_data_url,
                image_filename,
                updated_at
            )
            VALUES (1, 0, 1, 500, '', '', ?)
            """,
            (datetime.now().isoformat(timespec="seconds"),),
        )

    conn.commit()
    conn.close()


def get_entry_popup_settings():
    ensure_entry_popup_table()

    conn = get_db()
    row = conn.execute("SELECT * FROM entry_popup_settings WHERE id = 1").fetchone()
    conn.close()

    if not row:
        return {
            "is_active": 0,
            "show_once": 1,
            "delay_ms": 500,
            "image_data_url": "",
            "image_filename": "",
        }

    return dict(row)


def popup_upload_to_data_url(upload: UploadFile | None):
    if not upload or not upload.filename:
        return "", ""

    original_name = upload.filename or "popup.png"
    lower_name = original_name.lower()
    allowed = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }

    ext = os.path.splitext(lower_name)[1]
    content_type = allowed.get(ext)

    if not content_type:
        raise ValueError("ไฟล์รูปต้องเป็น JPG, PNG หรือ WEBP เท่านั้น")

    raw = upload.file.read()

    # กันอัปโหลดรูปใหญ่เกินไป เพราะเก็บใน SQLite เป็น base64
    max_size = 3 * 1024 * 1024
    if len(raw) > max_size:
        raise ValueError("รูปใหญ่เกินไป กรุณาใช้ไฟล์ไม่เกิน 3MB")

    encoded = base64.b64encode(raw).decode("utf-8")
    data_url = f"data:{content_type};base64,{encoded}"

    return data_url, original_name


@app.get("/api/entry-popup")
def api_entry_popup():
    popup = get_entry_popup_settings()
    image_data_url = popup.get("image_data_url") or ""

    return {
        "is_active": bool(popup.get("is_active") == 1 and image_data_url),
        "show_once": bool(popup.get("show_once") == 1),
        "delay_ms": int(popup.get("delay_ms") or 500),
        "image_url": image_data_url,
    }


@app.get("/admin/popup")
def admin_popup_page(request: Request):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/popup", status_code=303)

    popup = get_entry_popup_settings()

    return templates.TemplateResponse(
        "admin_popup.html",
        {
            "request": request,
            "user": admin,
            "popup": popup,
            "success": request.query_params.get("success", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/admin/popup")
def admin_popup_update(
    request: Request,
    image_file: UploadFile = File(None),
    is_active: str = Form("0"),
    show_once: str = Form("0"),
    delay_ms: int = Form(500),
    remove_image: str = Form("0"),
):
    admin = require_admin(request)

    if not admin:
        return RedirectResponse("/login?next=/admin/popup", status_code=303)

    ensure_entry_popup_table()

    try:
        current = get_entry_popup_settings()
        image_data_url = current.get("image_data_url") or ""
        image_filename = current.get("image_filename") or ""

        if remove_image == "1":
            image_data_url = ""
            image_filename = ""

        new_image_data_url, new_image_filename = popup_upload_to_data_url(image_file)
        if new_image_data_url:
            image_data_url = new_image_data_url
            image_filename = new_image_filename

        delay_ms = int(delay_ms or 500)
        if delay_ms < 0:
            delay_ms = 0
        if delay_ms > 10000:
            delay_ms = 10000

        conn = get_db()
        conn.execute(
            """
            UPDATE entry_popup_settings
            SET is_active = ?,
                show_once = ?,
                delay_ms = ?,
                image_data_url = ?,
                image_filename = ?,
                updated_at = ?
            WHERE id = 1
            """,
            (
                1 if is_active == "1" else 0,
                1 if show_once == "1" else 0,
                delay_ms,
                image_data_url,
                image_filename,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        conn.close()

        return RedirectResponse(
            "/admin/popup?success=บันทึก Popup สำเร็จ",
            status_code=303,
        )

    except Exception as e:
        print("ENTRY POPUP UPDATE ERROR:", repr(e))
        return RedirectResponse(
            "/admin/popup?error=" + urlencode({"": str(e)})[1:],
            status_code=303,
        )


@app.get("/packages/{slug}")
def package_detail(request: Request, slug: str):
    package = get_package_by_slug(slug)

    if not package:
        return RedirectResponse("/home", status_code=303)

    return templates.TemplateResponse(
        "package_detail.html",
        {
            "request": request,
            "user": current_user(request),
            "package": package,
        },
    )

# =========================
# WEBSITE AUTO TRANSLATE API
# แปลข้อความบนเว็บทั้งหน้าเป็น 5 ภาษา พร้อม cache ใน SQLite
# =========================
TRANSLATE_LANG_LABELS = {
    "th": "Thai",
    "en": "English",
    "zh": "Simplified Chinese",
    "ja": "Japanese",
    "ko": "Korean",
}


def ensure_translation_cache_table():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS translation_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_text TEXT NOT NULL,
            target_lang TEXT NOT NULL,
            translated_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_text, target_lang)
        )
        """
    )
    conn.commit()
    conn.close()


def get_cached_translations(texts, target_lang: str):
    ensure_translation_cache_table()
    result = {}
    if not texts:
        return result

    conn = get_db()
    for text in texts:
        row = conn.execute(
            """
            SELECT translated_text FROM translation_cache
            WHERE source_text = ? AND target_lang = ?
            """,
            (text, target_lang),
        ).fetchone()
        if row:
            result[text] = row["translated_text"]
    conn.close()
    return result


def save_translation_cache(translations: dict, target_lang: str):
    ensure_translation_cache_table()
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_db()
    for source_text, translated_text in translations.items():
        if not source_text or not translated_text:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO translation_cache
            (source_text, target_lang, translated_text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (source_text, target_lang, translated_text, now),
        )
    conn.commit()
    conn.close()


def ai_translate_text_batch(texts: list[str], target_lang: str):
    """Translate a batch of visible website texts.

    Fixes:
    - Uses Chat Completions, which is more compatible with installed OpenAI packages.
    - Does not permanently cache failed fallback translations.
    - If old cache contains source text = translated text for a non-Thai language,
      it treats that item as missing and translates again.
    """
    target_lang = (target_lang or "th").strip().lower()

    if target_lang == "th":
        return {text: text for text in texts}

    if target_lang not in TRANSLATE_LANG_LABELS:
        return {text: text for text in texts}

    clean_texts = []
    seen = set()
    for text in texts:
        text = (text or "").strip()
        if not text or text in seen:
            continue
        if len(text) > 900:
            continue
        seen.add(text)
        clean_texts.append(text)

    if not clean_texts:
        return {}

    cached = get_cached_translations(clean_texts, target_lang)

    # Important: older failed attempts may have cached the original Thai text.
    # For non-Thai languages, do not trust cache rows where translated == source.
    for source_text in list(cached.keys()):
        translated_text = (cached.get(source_text) or "").strip()
        if translated_text == source_text.strip():
            cached.pop(source_text, None)

    missing = [text for text in clean_texts if text not in cached]

    if not missing:
        return cached

    force_load_env()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_TRANSLATE_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

    if not api_key:
        print("PAGE TRANSLATE ERROR: Missing OPENAI_API_KEY")
        # Do NOT save fallback to cache.
        cached.update({text: text for text in missing})
        return cached

    translated = {}

    try:
        client = OpenAI(api_key=api_key)
        target_name = TRANSLATE_LANG_LABELS[target_lang]

        batch_size = 50
        for start in range(0, len(missing), batch_size):
            batch = missing[start:start + batch_size]

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a professional website translator for an aesthetic clinic. "
                        "Return valid JSON only. No markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Translate every item in this JSON array into {target_name}.\n"
                        "Rules:\n"
                        "- Keep brand names such as Home Care, LINE, Facebook, TikTok, Instagram unchanged.\n"
                        "- Keep URLs, phone numbers, email, prices, emojis, dates, and code-like words unchanged.\n"
                        "- Do not add new medical claims.\n"
                        "- Return ONLY JSON in this exact format: {\"items\":[\"...\",\"...\"]}\n"
                        "- The items array must have the same number of items and same order as input.\n\n"
                        f"Input JSON array:\n{json.dumps(batch, ensure_ascii=False)}"
                    ),
                },
            ]

            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.1,
                response_format={"type": "json_object"},
            )

            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw.strip())
            items = data.get("items", [])

            if not isinstance(items, list):
                items = []

            for i, source_text in enumerate(batch):
                value = items[i] if i < len(items) else source_text
                if not isinstance(value, str) or not value.strip():
                    value = source_text
                translated[source_text] = value.strip()

        # Save only after successful OpenAI response. This prevents failed Thai fallback cache.
        save_translation_cache(translated, target_lang)

    except Exception as e:
        print("PAGE TRANSLATE ERROR:", repr(e))
        # Do NOT save fallback to cache.
        translated = {text: text for text in missing}

    cached.update(translated)
    return cached


@app.post("/api/translate-page")
async def api_translate_page(request: Request):
    """Translate visible page text.

    Frontend versions may send the target language as `lang`, `target_lang`,
    or `language`, so this route accepts all three. This fixes the issue where
    the API always returned lang=th even when the page URL was ?lang=ko/zh/etc.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    lang = (
        body.get("target_lang")
        or body.get("lang")
        or body.get("language")
        or "th"
    )
    lang = str(lang).strip().lower()

    lang_aliases = {
        "cn": "zh",
        "zh-cn": "zh",
        "zh_cn": "zh",
        "chinese": "zh",
        "jp": "ja",
        "kr": "ko",
        "thai": "th",
        "english": "en",
    }
    lang = lang_aliases.get(lang, lang)

    texts = body.get("texts") or []

    if not isinstance(texts, list):
        texts = []

    texts = [str(text).strip() for text in texts if str(text).strip()]
    texts = texts[:700]

    if lang == "th":
        return {
            "ok": True,
            "lang": lang,
            "translations": {text: text for text in texts},
        }

    translations = ai_translate_text_batch(texts, lang)
    return {"ok": True, "lang": lang, "translations": translations}

