import json
import hashlib
import math
import os
import re
import secrets
import sqlite3
import time
import tempfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent


def load_local_env(path: Path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env(ROOT / ".env")

SEED_PLATFORM_FILE = ROOT / "seed_data" / "platform.json"
STORAGE_ROOT = Path(os.getenv("APP_STORAGE_DIR", "").strip() or (Path(tempfile.gettempdir()) / "smell-panties-preview")).expanduser()
try:
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
except OSError as exc:
    raise RuntimeError("Storage is not writable. Set APP_STORAGE_DIR=/tmp/smell-panties-preview for the no-disk preview.") from exc
DB_PATH = STORAGE_ROOT / "platform.sqlite3"
UPLOAD_ROOT = STORAGE_ROOT / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
secret_key = os.getenv("SECRET_KEY", "").strip()
if not secret_key:
    secret_path = STORAGE_ROOT / ".session-key"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_hex(32), encoding="utf-8")
    secret_key = secret_path.read_text(encoding="utf-8").strip()
if os.getenv("RENDER") and len(secret_key) < 32:
    raise RuntimeError("SECRET_KEY must contain at least 32 random characters.")
app.secret_key = secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true" if os.getenv("RENDER") else "false").lower() == "true",
    MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB", "100")) * 1024 * 1024,
)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "").strip()
BILLING_MODE = os.getenv("BILLING_MODE", "launch_free").strip().lower()
if os.getenv("FREE_RENDER_PREVIEW", "true").lower() == "true":
    BILLING_MODE = "launch_free"
if BILLING_MODE not in {"launch_free", "manual", "live"}:
    BILLING_MODE = "manual"
if os.getenv("RENDER") and len(ADMIN_PASSWORD) < 12:
    raise RuntimeError("Set a unique ADMIN_PASSWORD of at least 12 characters in Render.")
FOUNDER_LIMIT = int(os.getenv("FOUNDER_LIMIT", "1000"))
FOUNDER_FEE = float(os.getenv("FOUNDER_FEE", "9"))
DEFAULT_FEE = float(os.getenv("DEFAULT_PLATFORM_FEE", "12"))
LOGIN_WINDOW_SECONDS = 300
LOGIN_ATTEMPTS_LIMIT = 10
_login_attempts = {}

PUBLIC_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
MEDIA_EXTS = PUBLIC_IMAGE_EXTS | {"mp4", "webm", "mov", "m4v", "mp3", "wav", "m4a", "pdf", "zip"}
DELIVERY_EXTS = MEDIA_EXTS | {"txt", "doc", "docx"}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query_one(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def query_all(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def execute(sql, params=()):
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL CHECK(role IN ('fan','creator')),
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            adult_confirmed INTEGER NOT NULL DEFAULT 0,
            sp_nudes_balance INTEGER NOT NULL DEFAULT 9,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS creators (
            id TEXT PRIMARY KEY,
            owner_user_id TEXT UNIQUE REFERENCES users(id) ON DELETE SET NULL,
            slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL,
            handle TEXT NOT NULL,
            bio TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT 'Private',
            avatar_text TEXT NOT NULL DEFAULT 'SP',
            avatar_path TEXT,
            cover_path TEXT,
            business_email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            custom_domain TEXT NOT NULL DEFAULT '',
            token_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            token_symbol TEXT NOT NULL UNIQUE COLLATE NOCASE,
            sp_nudes_spent INTEGER NOT NULL DEFAULT 0,
            tokens_minted INTEGER NOT NULL DEFAULT 0,
            subscription_price REAL NOT NULL DEFAULT 19.99,
            subscription_currency TEXT NOT NULL DEFAULT 'USD',
            founder_slot INTEGER,
            platform_fee_percent REAL NOT NULL DEFAULT 9,
            legacy_subscriber_count INTEGER NOT NULL DEFAULT 0,
            identity_status TEXT NOT NULL DEFAULT 'unverified',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT 'digital',
            price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            edition TEXT NOT NULL DEFAULT '1/1',
            status TEXT NOT NULL DEFAULT 'available',
            image_path TEXT,
            delivery_path TEXT,
            delivery_note TEXT NOT NULL DEFAULT '',
            buyer_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            sold_at TEXT
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY,
            fan_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
            tier TEXT NOT NULL DEFAULT 'Subscriber',
            price REAL NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            billing_state TEXT NOT NULL DEFAULT 'not_connected',
            created_at TEXT NOT NULL,
            activated_at TEXT,
            canceled_at TEXT,
            UNIQUE(fan_user_id, creator_id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
            fan_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sender_actor TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read_at TEXT
        );

        CREATE TABLE IF NOT EXISTS offers (
            id TEXT PRIMARY KEY,
            creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
            fan_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS private_posts (
            id TEXT PRIMARY KEY,
            creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            media_path TEXT,
            media_kind TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS streams (
            id TEXT PRIMARY KEY,
            creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            audience TEXT NOT NULL DEFAULT 'subscribers',
            target_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            price_per_minute REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'scheduled',
            created_at TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT
        );

        CREATE TABLE IF NOT EXISTS stream_members (
            stream_id TEXT NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY(stream_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS stream_sessions (
            id TEXT PRIMARY KEY,
            stream_id TEXT NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
            viewer_actor TEXT NOT NULL,
            fan_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            seconds_used REAL NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            last_seen_epoch REAL NOT NULL,
            UNIQUE(stream_id, viewer_actor)
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_type TEXT NOT NULL,
            room_id TEXT NOT NULL,
            from_actor TEXT NOT NULL,
            to_actor TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signals_room_to ON signals(room_type, room_id, to_actor, id);

        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL UNIQUE,
            fan_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
            product_id TEXT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
            product_title TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            platform_fee_percent REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_payment',
            payment_reference TEXT,
            created_at TEXT NOT NULL,
            paid_at TEXT
        );

        CREATE TABLE IF NOT EXISTS token_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fan_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
            amount INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS creator_token_balances (
            fan_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
            balance INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(fan_user_id, creator_id)
        );

        CREATE TABLE IF NOT EXISTS token_redemptions (
            id TEXT PRIMARY KEY,
            fan_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
            amount INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'requested',
            created_at TEXT NOT NULL,
            fulfilled_at TEXT
        );

        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            reporter_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            reporter_email TEXT,
            creator_id TEXT REFERENCES creators(id) ON DELETE SET NULL,
            content_type TEXT NOT NULL DEFAULT 'profile',
            content_id TEXT,
            reason TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            event TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "sp_nudes_balance" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN sp_nudes_balance INTEGER NOT NULL DEFAULT 9")
    conn.commit()
    if os.getenv("SEED_DEMO_DATA", "false").lower() == "true":
        seed_creators(conn)
    conn.close()


def seed_creators(conn):
    existing = conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
    if existing:
        return
    try:
        payload = json.loads(SEED_PLATFORM_FILE.read_text(encoding="utf-8"))
    except Exception:
        payload = {"creators": []}
    for index, creator in enumerate(payload.get("creators", []), start=1):
        token = creator.get("token", {})
        cid = creator.get("id") or f"cr_seed_{index:03d}"
        conn.execute(
            """INSERT OR IGNORE INTO creators
            (id, owner_user_id, slug, display_name, handle, bio, location, avatar_text,
             business_email, custom_domain, token_name, token_symbol, sp_nudes_spent,
             tokens_minted, subscription_price, subscription_currency, founder_slot,
             platform_fee_percent, legacy_subscriber_count, identity_status, status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cid,
                None,
                creator.get("slug") or slugify(creator.get("display_name", "creator")),
                creator.get("display_name", "Creator"),
                creator.get("handle", "@creator"),
                creator.get("bio", ""),
                creator.get("location", "Private"),
                creator.get("avatar", "SP"),
                creator.get("business_email") or f"creator{index}@smellpanties.com",
                creator.get("custom_domain", ""),
                token.get("name") or f"Creator Token {index}",
                token.get("symbol") or f"SP{index}",
                int(token.get("sp_nudes_spent", 0)),
                int(token.get("tokens_minted", 0)),
                float(creator.get("subscription_price", 19.99)),
                creator.get("subscription_currency", "USD"),
                index,
                FOUNDER_FEE if index <= FOUNDER_LIMIT else DEFAULT_FEE,
                int(creator.get("subscriber_count", 0)),
                "seed-demo",
                "active",
                utcnow(),
            ),
        )
        for product in creator.get("products", []):
            pid = product.get("id") or "prd_" + secrets.token_hex(5)
            conn.execute(
                """INSERT OR IGNORE INTO products
                (id, creator_id, title, description, type, price, currency, edition, status,
                 image_path, delivery_path, delivery_note, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pid,
                    cid,
                    product.get("title", "Unique Drop"),
                    product.get("description", ""),
                    product.get("type", "digital"),
                    float(product.get("price", 0)),
                    product.get("currency", "USD"),
                    product.get("edition", "1/1"),
                    product.get("status", "available"),
                    None,
                    None,
                    product.get("delivery_note", ""),
                    utcnow(),
                ),
            )
        for post in creator.get("private_posts", []):
            conn.execute(
                """INSERT INTO private_posts (id, creator_id, title, body, media_path, media_kind, created_at)
                VALUES (?,?,?,?,?,?,?)""",
                ("post_" + secrets.token_hex(5), cid, post.get("title", "Private update"), post.get("body", ""), None, None, utcnow()),
            )
    conn.commit()


def brand_data():
    try:
        data = json.loads(SEED_PLATFORM_FILE.read_text(encoding="utf-8"))
        brand = data.get("brand", {})
    except Exception:
        brand = {}
    brand.setdefault("name", "Smell Panties")
    brand.setdefault("tagline", "More than a scent.")
    brand.setdefault("fee_percent", FOUNDER_FEE)
    brand.setdefault("founder_limit", FOUNDER_LIMIT)
    return brand


def slugify(value):
    value = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return value or "creator"


def make_symbol(token_name):
    base = re.sub(r"[^A-Z0-9]", "", (token_name or "TOKEN").upper())[:6] or "TOKEN"
    symbol = base
    i = 2
    while query_one("SELECT id FROM creators WHERE token_symbol = ? COLLATE NOCASE", (symbol,)):
        suffix = str(i)
        symbol = (base[: max(1, 6 - len(suffix))] + suffix)[:6]
        i += 1
    return symbol


def unique_slug(display_name):
    base = slugify(display_name)
    candidate = base
    i = 2
    while query_one("SELECT id FROM creators WHERE slug = ? COLLATE NOCASE", (candidate,)):
        candidate = f"{base}-{i}"
        i += 1
    return candidate


def next_founder_slot():
    row = query_one("SELECT COALESCE(MAX(founder_slot),0) AS m FROM creators")
    return int(row["m"] or 0) + 1


def current_user():
    if not hasattr(g, "_current_user"):
        user_id = session.get("user_id")
        g._current_user = query_one("SELECT * FROM users WHERE id = ?", (user_id,)) if user_id else None
    user = g._current_user
    if user and (user["status"] != "active" or session.get("auth_stamp") != hashlib.sha256(user["password_hash"].encode()).hexdigest()):
        session.clear()
        g._current_user = None
    return g._current_user


def current_role():
    if session.get("admin") and session.get("admin_stamp") == hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest() and ADMIN_PASSWORD:
        return "admin"
    if session.get("demo"):
        return "demo"
    user = current_user()
    return user["role"] if user else "guest"


def is_admin():
    return current_role() == "admin"


def is_demo():
    return current_role() == "demo"


def creator_for_user(user_id=None):
    user_id = user_id or (current_user()["id"] if current_user() else None)
    if not user_id:
        return None
    return query_one("SELECT * FROM creators WHERE owner_user_id = ?", (user_id,))


def get_creator(slug):
    creator = query_one("SELECT * FROM creators WHERE slug = ? COLLATE NOCASE AND status != 'deleted'", (slug,))
    if not creator:
        abort(404)
    if creator['owner_user_id'] and not is_admin():
        owner = query_one('SELECT status FROM users WHERE id=?',(creator['owner_user_id'],))
        if owner and owner['status'] != 'active':
            abort(404)
    return creator


def get_product(creator_id, product_id):
    product = query_one("SELECT * FROM products WHERE id = ? AND creator_id = ?", (product_id, creator_id))
    if not product:
        abort(404)
    return product


def can_manage_creator(creator):
    if is_admin():
        return True
    user = current_user()
    return bool(user and user["role"] == "creator" and creator["owner_user_id"] == user["id"] and user["status"] == "active")


def active_subscription(fan_user_id, creator_id):
    if not fan_user_id:
        return None
    return query_one(
        "SELECT * FROM subscriptions WHERE fan_user_id = ? AND creator_id = ? AND status = 'active' AND (expires_at IS NULL OR expires_at > ?)",
        (fan_user_id, creator_id, utcnow()),
    )


def any_subscription(fan_user_id, creator_id):
    if not fan_user_id:
        return None
    return query_one("SELECT * FROM subscriptions WHERE fan_user_id = ? AND creator_id = ?", (fan_user_id, creator_id))


def creator_token_balance(fan_user_id, creator_id):
    if not fan_user_id:
        return 0
    row = query_one("SELECT balance FROM creator_token_balances WHERE fan_user_id=? AND creator_id=?", (fan_user_id, creator_id))
    return int(row["balance"] if row else 0)


def creator_subscriber_count(creator):
    row = query_one("SELECT COUNT(*) AS n FROM subscriptions WHERE creator_id = ? AND status = 'active'", (creator["id"],))
    return int(creator["legacy_subscriber_count"] or 0) + int(row["n"] or 0)


def creator_rankings():
    return query_all("SELECT * FROM creators WHERE status='active' ORDER BY sp_nudes_spent DESC, created_at ASC LIMIT 9")


def audit(event, details=""):
    actor = "admin" if is_admin() else (f"user:{current_user()['id']}" if current_user() else "guest")
    execute("INSERT INTO audit_log (actor,event,details,created_at) VALUES (?,?,?,?)", (actor, event, details[:2000], utcnow()))


def ensure_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def safe_next_url(value, fallback="brand_profile"):
    value = (value or "").strip()
    if value.startswith("/") and not value.startswith("//") and "\\" not in value and not any(ord(c) < 32 for c in value):
        return value
    return url_for(fallback)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user() and not is_admin():
            flash("Log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        if current_user() and current_user()["status"] != "active":
            session.clear()
            flash("This account is not active.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def fan_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "fan":
            flash("A subscriber account is required for this action.", "error")
            return redirect(url_for("signup", next=request.path))
        if user["status"] != "active":
            session.clear()
            flash("This account is not active.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            flash("Administrator access required.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def creator_owner_required(view):
    @wraps(view)
    def wrapped(slug, *args, **kwargs):
        creator = get_creator(slug)
        if not can_manage_creator(creator):
            abort(403)
        return view(slug, creator, *args, **kwargs)
    return wrapped


def check_login_rate_limit(ip):
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    return len(attempts) < LOGIN_ATTEMPTS_LIMIT


def record_login_failure(ip):
    _login_attempts.setdefault(ip, []).append(time.time())


def ext_of(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def save_upload(file_obj, category, allowed_exts, public=True):
    if not file_obj or not file_obj.filename:
        return None
    ext = ext_of(file_obj.filename)
    if ext not in allowed_exts:
        raise ValueError(f"Unsupported file type: .{ext or 'unknown'}")
    original = secure_filename(file_obj.filename)
    stem = slugify(Path(original).stem)[:50]
    filename = f"{stem}-{secrets.token_hex(6)}.{ext}"
    prefix = "public" if public else "private"
    rel = Path(prefix) / category / filename
    target = UPLOAD_ROOT / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    file_obj.save(target)
    return rel.as_posix()


def actor_id_for_room(creator, as_host=False):
    if as_host and can_manage_creator(creator):
        return f"creator:{creator['id']}"
    user = current_user()
    if user:
        return f"user:{user['id']}"
    guest = session.get("guest_actor")
    if not guest:
        guest = "guest:" + secrets.token_hex(8)
        session["guest_actor"] = guest
    return guest


def stream_access(stream, creator):
    if can_manage_creator(creator):
        return True
    audience = stream["audience"]
    if audience == "public":
        return True
    user = current_user()
    if not user or user["role"] != "fan":
        return False
    if audience == "private":
        return stream["target_user_id"] == user["id"] and bool(active_subscription(user['id'], creator['id']))
    if audience == "group":
        member = query_one("SELECT 1 AS ok FROM stream_members WHERE stream_id=? AND user_id=?", (stream["id"], user["id"]))
        return bool(member) and bool(active_subscription(user['id'], creator['id']))
    return bool(active_subscription(user["id"], creator["id"]))


_last_signal_cleanup = 0.0


def cleanup_old_signals():
    global _last_signal_cleanup
    now = time.time()
    if now - _last_signal_cleanup < 900:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    execute("DELETE FROM signals WHERE created_at < ?", (cutoff,))
    _last_signal_cleanup = now


def touch_stream_session(stream_id, actor):
    if actor.startswith("creator:"):
        return
    now_epoch = time.time()
    user = current_user()
    fan_id = user["id"] if user and user["role"] == "fan" else None
    row = query_one("SELECT * FROM stream_sessions WHERE stream_id=? AND viewer_actor=?", (stream_id, actor))
    if not row:
        execute(
            "INSERT INTO stream_sessions (id,stream_id,viewer_actor,fan_user_id,seconds_used,started_at,last_seen_epoch) VALUES (?,?,?,?,?,?,?)",
            ("ss_" + secrets.token_hex(8), stream_id, actor, fan_id, 0, utcnow(), now_epoch),
        )
        return
    delta = max(0.0, min(5.0, now_epoch - float(row["last_seen_epoch"] or now_epoch)))
    execute("UPDATE stream_sessions SET seconds_used=seconds_used+?,last_seen_epoch=? WHERE id=?", (delta, now_epoch, row["id"]))


def finalize_order_paid(order_id, payment_reference="manual"):
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if not order:
        abort(404)
    if order["status"] == "paid":
        return order
    if order["status"] != "pending_payment":
        abort(409, description="This order cannot be marked paid after cancellation.")
    product = db.execute("SELECT * FROM products WHERE id = ?", (order["product_id"],)).fetchone()
    if not product:
        abort(404)
    if product["status"] != "reserved":
        abort(409, description="The product reservation is no longer available.")
    now = utcnow()
    db.execute("UPDATE orders SET status='paid', payment_reference=?, paid_at=? WHERE order_id=?", (payment_reference, now, order_id))
    db.execute("UPDATE products SET status='sold', buyer_user_id=?, sold_at=? WHERE id=?", (order["fan_user_id"], now, order["product_id"]))
    db.commit()
    return db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()


init_db()


@app.before_request
def age_gate_and_csrf():
    if request.endpoint in {"billing.webhook", "billing.paypal_return", "health", "static"}:
        return None
    ensure_csrf_token()
    execute("UPDATE subscriptions SET status='expired' WHERE status='active' AND expires_at IS NOT NULL AND expires_at<=?", (utcnow(),))
    if request.endpoint not in {'static', 'health'}:
        billing.expire_unstarted()
    gate_exempt = {"static", "health", "age_gate_page", "age_check", "payment_webhook", "robots"}
    user = current_user()
    if session.get("user_id") and not user:
        session.clear()
    adult_ok = bool(session.get("adult_gate") or (user and user["adult_confirmed"]))
    if request.endpoint not in gate_exempt and not adult_ok:
        next_url = request.full_path if request.query_string else request.path
        return render_template("age.html", next_url=next_url), 200
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint not in {"payment_webhook"}:
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token", "")
        if not supplied or not expected or not secrets.compare_digest(supplied, expected):
            abort(400, description="Invalid or missing request token.")


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(self), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if request.endpoint != "static":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.context_processor
def inject_globals():
    user = current_user()
    owned = creator_for_user(user["id"]) if user and user["role"] == "creator" else None
    return {
        "brand": brand_data(),
        "csrf_token": ensure_csrf_token(),
        "auth_user": user,
        "auth_role": current_role(),
        "is_admin": is_admin(),
        "is_demo": is_demo(),
        "owned_creator": owned,
        "now_year": datetime.now().year,
        "billing_mode": BILLING_MODE,
        "temporary_preview": os.getenv("FREE_RENDER_PREVIEW", "true").lower() == "true",
        "can_manage_creator": can_manage_creator,
        "adult_gate": bool(session.get("adult_gate") or (user and user["adult_confirmed"])),
    }


@app.route("/age")
def age_gate_page():
    return render_template("age.html", next_url=safe_next_url(request.args.get("next")))


@app.route("/age-check", methods=["POST"])
def age_check():
    if request.form.get("adult_confirm") != "yes":
        return redirect("https://www.google.com")
    session["adult_gate"] = True
    return redirect(safe_next_url(request.form.get("next")))


@app.route("/")
def brand_profile():
    # When a creator custom domain is mapped to this Render service, its root URL
    # resolves directly to that creator profile. DNS/Render domain attachment is external.
    host = (request.host or "").split(":", 1)[0].lower().rstrip(".")
    if host:
        custom = query_one(
            "SELECT slug FROM creators WHERE lower(custom_domain)=? AND custom_domain!='' AND status='active'",
            (host,),
        )
        if custom:
            return creator_public(custom["slug"])
    creators = query_all("SELECT * FROM creators WHERE status='active' ORDER BY sp_nudes_spent DESC, created_at ASC")
    return render_template("brand.html", rankings=creator_rankings(), creators=creators)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.form.get("email", "").strip().lower()[:254]
        if not check_login_rate_limit(ip):
            flash("Too many login attempts. Try again in a few minutes.", "error")
            return render_template("login.html", next_url=safe_next_url(request.form.get("next"))), 429
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        next_url = safe_next_url(request.form.get("next"))
        if ADMIN_PASSWORD and email in {"admin", "developer", "owner"} and secrets.compare_digest(password, ADMIN_PASSWORD):
            session.clear()
            session["admin"] = True
            session["admin_stamp"] = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
            session["adult_gate"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            audit("admin_login")
            return redirect(next_url if next_url != url_for("brand_profile") else url_for("admin_dashboard"))
        if DEMO_PASSWORD and email == "demo" and secrets.compare_digest(password, DEMO_PASSWORD):
            session.clear()
            session["demo"] = True
            session["adult_gate"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(next_url)
        user = query_one("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,))
        if not user or not check_password_hash(user["password_hash"], password) or user["status"] != "active":
            record_login_failure(ip)
            flash("Email or password not recognized.", "error")
            return render_template("login.html", next_url=next_url), 401
        _login_attempts.pop(ip, None)
        session.clear()
        session["user_id"] = user["id"]
        session["auth_stamp"] = hashlib.sha256(user["password_hash"].encode()).hexdigest()
        session["adult_gate"] = bool(user["adult_confirmed"])
        session["csrf_token"] = secrets.token_urlsafe(32)
        execute("UPDATE users SET last_login_at=? WHERE id=?", (utcnow(), user["id"]))
        audit("user_login", user["email"])
        if user["role"] == "creator":
            creator = creator_for_user(user["id"])
            return redirect(next_url if next_url != url_for("brand_profile") else (url_for("creator_dashboard", slug=creator["slug"]) if creator else url_for("brand_profile")))
        return redirect(next_url)
    return render_template("login.html", next_url=safe_next_url(request.args.get("next")))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("brand_profile"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        next_url = safe_next_url(request.form.get("next"))
        errors = []
        if not request.form.get("adult_confirm"):
            errors.append("You must confirm that you are 18 or older.")
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            errors.append("Enter a valid email address.")
        if len(display_name) < 2 or len(display_name) > 80:
            errors.append("Display name must be 2–80 characters.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if query_one("SELECT id FROM users WHERE email=? COLLATE NOCASE", (email,)):
            errors.append("An account with that email already exists.")
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("fan_signup.html", next_url=next_url), 400
        user_id = "usr_" + secrets.token_hex(8)
        execute(
            "INSERT INTO users (id,role,email,password_hash,display_name,adult_confirmed,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, "fan", email, generate_password_hash(password), display_name, 1, "active", utcnow()),
        )
        session.clear()
        session["user_id"] = user_id
        session["auth_stamp"] = hashlib.sha256(query_one("SELECT password_hash FROM users WHERE id=?", (user_id,))["password_hash"].encode()).hexdigest()
        session["adult_gate"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        audit("fan_signup", email)
        flash("Subscriber account created.", "success")
        return redirect(next_url)
    return render_template("fan_signup.html", next_url=safe_next_url(request.args.get("next")))


@app.route("/creator-signup", methods=["GET", "POST"])
@app.route("/join", methods=["GET", "POST"])
def creator_signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        token_name = request.form.get("token_name", "").strip()
        email_handle = slugify(request.form.get("email_handle", "") or display_name).replace("-", ".")
        errors = []
        if not request.form.get("adult_confirm"):
            errors.append("Creator onboarding is 18+ only.")
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            errors.append("Enter a valid account email.")
        if len(display_name) < 2 or len(display_name) > 80:
            errors.append("Creator name must be 2–80 characters.")
        if len(token_name) < 2 or len(token_name) > 40:
            errors.append("Token name must be 2–40 characters.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if query_one("SELECT id FROM users WHERE email=? COLLATE NOCASE", (email,)):
            errors.append("That login email is already registered.")
        if query_one("SELECT id FROM creators WHERE token_name=? COLLATE NOCASE", (token_name,)):
            errors.append("That creator token name is already taken.")
        business_email = f"{email_handle}@smellpanties.com"
        if query_one("SELECT id FROM creators WHERE business_email=? COLLATE NOCASE", (business_email,)):
            errors.append("That Smell Panties business-email identity is already taken.")
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("signup.html"), 400
        user_id = "usr_" + secrets.token_hex(8)
        creator_id = "cr_" + secrets.token_hex(8)
        slug = unique_slug(display_name)
        symbol = make_symbol(token_name)
        slot = next_founder_slot()
        fee = FOUNDER_FEE if slot <= FOUNDER_LIMIT else DEFAULT_FEE
        initials = "".join(part[0].upper() for part in display_name.split()[:2]) or "SP"
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (id,role,email,password_hash,display_name,adult_confirmed,sp_nudes_balance,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, "creator", email, generate_password_hash(password), display_name, 1, 0, "active", utcnow()),
            )
            db.execute(
                """INSERT INTO creators
                (id,owner_user_id,slug,display_name,handle,bio,location,avatar_text,business_email,token_name,token_symbol,
                 subscription_price,subscription_currency,founder_slot,platform_fee_percent,identity_status,status,created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    creator_id,
                    user_id,
                    slug,
                    display_name,
                    "@" + slug.replace("-", ""),
                    "Welcome to my private creator space.",
                    "Private",
                    initials,
                    business_email,
                    token_name,
                    symbol,
                    19.99,
                    "USD",
                    slot,
                    fee,
                    "unverified",
                    "active",
                    utcnow(),
                ),
            )
            db.execute(
                "INSERT INTO private_posts (id,creator_id,title,body,created_at) VALUES (?,?,?,?,?)",
                ("post_" + secrets.token_hex(6), creator_id, "Welcome room", "Your subscriber-only feed is ready.", utcnow()),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        session.clear()
        session["user_id"] = user_id
        session["auth_stamp"] = hashlib.sha256(query_one("SELECT password_hash FROM users WHERE id=?", (user_id,))["password_hash"].encode()).hexdigest()
        session["adult_gate"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        audit("creator_signup", f"{slug} founder_slot={slot}")
        flash("Creator account is live. Finish your profile and publish your first product.", "success")
        return redirect(url_for("creator_dashboard", slug=slug))
    return render_template("signup.html")


@app.route("/account")
@fan_required
def fan_account():
    user = current_user()
    subscriptions = query_all(
        """SELECT s.*, c.slug, c.display_name AS creator_name, c.avatar_text
        FROM subscriptions s JOIN creators c ON c.id=s.creator_id
        WHERE s.fan_user_id=? ORDER BY s.created_at DESC""",
        (user["id"],),
    )
    orders = query_all(
        """SELECT o.*, c.slug, c.display_name AS creator_name FROM orders o
        JOIN creators c ON c.id=o.creator_id WHERE o.fan_user_id=? ORDER BY o.created_at DESC""",
        (user["id"],),
    )
    token_balances = query_all(
        """SELECT b.balance,c.slug,c.display_name AS creator_name,c.token_name,c.token_symbol FROM creator_token_balances b
        JOIN creators c ON c.id=b.creator_id WHERE b.fan_user_id=? AND b.balance>0 ORDER BY b.balance DESC""",
        (user["id"],),
    )
    conversations = query_all(
        """SELECT c.slug, c.display_name AS creator_name, c.avatar_text, MAX(m.created_at) AS last_message,
        COUNT(m.id) AS message_count FROM messages m JOIN creators c ON c.id=m.creator_id
        WHERE m.fan_user_id=? GROUP BY c.id ORDER BY last_message DESC""",
        (user["id"],),
    )
    invoices = query_all("SELECT * FROM billing_invoices WHERE buyer_id=? ORDER BY created DESC LIMIT 100", (user['id'],))
    return render_template("fan_account.html", subscriptions=subscriptions, orders=orders, conversations=conversations, token_balances=token_balances, invoices=invoices)


@app.route("/creator/<slug>")
def creator_public(slug):
    creator = get_creator(slug)
    products = query_all("SELECT * FROM products WHERE creator_id=? AND status!='archived' ORDER BY created_at DESC", (creator["id"],))
    ranks = creator_rankings()
    rank = next((i + 1 for i, item in enumerate(ranks) if item["id"] == creator["id"]), None)
    user = current_user()
    subscription = any_subscription(user["id"], creator["id"]) if user and user["role"] == "fan" else None
    token_balance = creator_token_balance(user["id"], creator["id"]) if user and user["role"] == "fan" else 0
    candidate_streams = query_all("SELECT * FROM streams WHERE creator_id=? AND status IN ('scheduled','live') ORDER BY created_at DESC LIMIT 20", (creator["id"],))
    streams = [stream for stream in candidate_streams if stream_access(stream, creator)][:5]
    return render_template(
        "creator_public.html",
        creator=creator,
        products=products,
        rank=rank,
        subscriber_count=creator_subscriber_count(creator),
        subscription=subscription,
        token_balance=token_balance,
        streams=streams,
    )


@app.route("/creator/<slug>/token/spend", methods=["POST"])
@fan_required
def token_spend(slug):
    creator = get_creator(slug)
    user = current_user()
    amount = 1
    db = get_db()
    try:
        debited = db.execute("UPDATE users SET sp_nudes_balance=sp_nudes_balance-? WHERE id=? AND sp_nudes_balance>=?", (amount, user["id"], amount))
        if debited.rowcount != 1:
            db.rollback()
            flash("You do not have enough SP-Nudes. Wallet top-up will connect to the payment processor later.", "error")
            return redirect(url_for("creator_public", slug=slug))
        db.execute("UPDATE creators SET sp_nudes_spent=sp_nudes_spent+?,tokens_minted=tokens_minted+? WHERE id=?", (amount, amount, creator["id"]))
        db.execute(
            """INSERT INTO creator_token_balances (fan_user_id,creator_id,balance) VALUES (?,?,?)
            ON CONFLICT(fan_user_id,creator_id) DO UPDATE SET balance=balance+excluded.balance""",
            (user["id"], creator["id"], amount),
        )
        db.execute("INSERT INTO token_ledger (fan_user_id,creator_id,amount,created_at) VALUES (?,?,?,?)", (user["id"], creator["id"], amount, utcnow()))
        db.commit()
    except Exception:
        db.rollback()
        raise
    audit("sp_nude_spent", f"creator={slug} amount={amount}")
    flash(f"1 SP-Nude spent. You received 1 ${creator['token_symbol']} creator token.", "success")
    return redirect(url_for("creator_public", slug=slug))


@app.route("/creator/<slug>/token/redeem", methods=["POST"])
@fan_required
def token_redeem(slug):
    creator = get_creator(slug)
    user = current_user()
    if creator_token_balance(user["id"], creator["id"]) < 1:
        flash(f"You do not have a ${creator['token_symbol']} token to redeem.", "error")
        return redirect(url_for("creator_public", slug=slug))
    db = get_db()
    try:
        changed = db.execute(
            "UPDATE creator_token_balances SET balance=balance-1 WHERE fan_user_id=? AND creator_id=? AND balance>=1",
            (user["id"], creator["id"]),
        )
        if changed.rowcount != 1:
            db.rollback()
            flash("Token balance changed. Please try again.", "error")
            return redirect(url_for("creator_public", slug=slug))
        redemption_id = "red_" + secrets.token_hex(8)
        db.execute(
            "INSERT INTO token_redemptions (id,fan_user_id,creator_id,amount,status,created_at) VALUES (?,?,?,?,?,?)",
            (redemption_id, user["id"], creator["id"], 1, "requested", utcnow()),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    audit("creator_token_redeemed", f"creator={slug} redemption={redemption_id}")
    flash("Redemption request sent to the creator.", "success")
    return redirect(url_for("fan_account"))


@app.route("/creator/<slug>/redemption/<redemption_id>/fulfill", methods=["POST"])
@creator_owner_required
def fulfill_redemption(slug, creator, redemption_id):
    redemption = query_one("SELECT * FROM token_redemptions WHERE id=? AND creator_id=?", (redemption_id, creator["id"]))
    if not redemption:
        abort(404)
    execute("UPDATE token_redemptions SET status='fulfilled',fulfilled_at=? WHERE id=?", (utcnow(), redemption_id))
    audit("redemption_fulfilled", redemption_id)
    flash("Redemption marked fulfilled.", "success")
    return redirect(url_for("creator_dashboard", slug=slug) + "#redemptions")


@app.route("/creator/<slug>/subscribe", methods=["GET", "POST"])
def subscribe_creator(slug):
    creator = get_creator(slug)
    user = current_user()
    if not user:
        return redirect(url_for("signup", next=url_for("subscribe_creator", slug=slug)))
    if user["role"] != "fan":
        flash("Use a subscriber account to subscribe to a creator.", "error")
        return redirect(url_for("creator_public", slug=slug))
    existing = any_subscription(user["id"], creator["id"])
    if request.method == "POST":
        if BILLING_MODE == "live":
            return billing.subscription_checkout(creator, user)
        now = utcnow()
        if BILLING_MODE == "launch_free":
            status, billing_state, activated = "active", "launch_access_no_charge", now
            notice = "Subscription activated with launch access. Billing is not connected yet."
        else:
            status, billing_state, activated = "pending", ("awaiting_manual_payment" if BILLING_MODE == "manual" else "processor_integration_required"), None
            notice = "Subscription created. Access will activate when payment is confirmed."
        if existing:
            execute(
                "UPDATE subscriptions SET status=?,billing_state=?,price=?,currency=?,activated_at=?,canceled_at=NULL WHERE id=?",
                (status, billing_state, creator["subscription_price"], creator["subscription_currency"], activated, existing["id"]),
            )
        else:
            execute(
                """INSERT INTO subscriptions (id,fan_user_id,creator_id,tier,price,currency,status,billing_state,created_at,activated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "sub_" + secrets.token_hex(8),
                    user["id"],
                    creator["id"],
                    "Subscriber",
                    creator["subscription_price"],
                    creator["subscription_currency"],
                    status,
                    billing_state,
                    now,
                    activated,
                ),
            )
        audit("subscription_created", f"creator={creator['slug']} mode={BILLING_MODE} status={status}")
        flash(notice, "success" if status == "active" else "info")
        return redirect(url_for("creator_private", slug=slug) if status == "active" else url_for("fan_account"))
    return render_template("subscribe.html", creator=creator, subscription=existing)


@app.route("/subscription/<subscription_id>/cancel", methods=["POST"])
@fan_required
def cancel_subscription(subscription_id):
    user = current_user()
    sub = query_one("SELECT * FROM subscriptions WHERE id=? AND fan_user_id=?", (subscription_id, user["id"]))
    if not sub:
        abort(404)
    execute("UPDATE subscriptions SET status='canceled',canceled_at=? WHERE id=?", (utcnow(), subscription_id))
    audit("subscription_canceled", subscription_id)
    flash("Subscription canceled.", "success")
    return redirect(url_for("fan_account"))


@app.route("/creator/<slug>/private")
def creator_private(slug):
    creator = get_creator(slug)
    user = current_user()
    allowed = can_manage_creator(creator) or bool(user and user["role"] == "fan" and active_subscription(user["id"], creator["id"]))
    if not allowed:
        flash("An active subscription is required for the private room.", "error")
        return redirect(url_for("subscribe_creator", slug=slug))
    posts = query_all("SELECT * FROM private_posts WHERE creator_id=? ORDER BY created_at DESC", (creator["id"],))
    candidate_streams = query_all("SELECT * FROM streams WHERE creator_id=? AND status IN ('scheduled','live') ORDER BY created_at DESC", (creator["id"],))
    streams = [stream for stream in candidate_streams if stream_access(stream, creator)]
    return render_template("creator_private.html", creator=creator, posts=posts, streams=streams)


@app.route("/creator/<slug>/dashboard")
@creator_owner_required
def creator_dashboard(slug, creator):
    products = query_all("SELECT * FROM products WHERE creator_id=? ORDER BY created_at DESC", (creator["id"],))
    subscribers = query_all(
        """SELECT s.*, u.email, u.display_name FROM subscriptions s JOIN users u ON u.id=s.fan_user_id
        WHERE s.creator_id=? ORDER BY CASE WHEN s.status='active' THEN 0 ELSE 1 END, s.created_at DESC""",
        (creator["id"],),
    )
    conversations = query_all(
        """SELECT u.id AS fan_id,u.display_name,u.email,MAX(m.created_at) AS last_message,COUNT(m.id) AS message_count
        FROM messages m JOIN users u ON u.id=m.fan_user_id WHERE m.creator_id=?
        GROUP BY u.id ORDER BY last_message DESC""",
        (creator["id"],),
    )
    posts = query_all("SELECT * FROM private_posts WHERE creator_id=? ORDER BY created_at DESC", (creator["id"],))
    streams = query_all("SELECT * FROM streams WHERE creator_id=? ORDER BY created_at DESC LIMIT 30", (creator["id"],))
    stream_usage = query_all(
        """SELECT ss.*,s.title,s.price_per_minute,u.display_name AS fan_name,u.email AS fan_email
        FROM stream_sessions ss JOIN streams s ON s.id=ss.stream_id
        LEFT JOIN users u ON u.id=ss.fan_user_id WHERE s.creator_id=? ORDER BY ss.started_at DESC LIMIT 100""",
        (creator["id"],),
    )
    redemptions = query_all(
        """SELECT r.*,u.display_name AS fan_name,u.email AS fan_email FROM token_redemptions r
        JOIN users u ON u.id=r.fan_user_id WHERE r.creator_id=? ORDER BY r.created_at DESC LIMIT 50""",
        (creator["id"],),
    )
    orders = query_all(
        """SELECT o.*,u.display_name AS buyer_name FROM orders o JOIN users u ON u.id=o.fan_user_id
        WHERE o.creator_id=? ORDER BY o.created_at DESC LIMIT 50""",
        (creator["id"],),
    )
    return render_template(
        "creator_dashboard.html",
        creator=creator,
        products=products,
        subscribers=subscribers,
        conversations=conversations,
        posts=posts,
        streams=streams,
        stream_usage=stream_usage,
        orders=orders,
        redemptions=redemptions,
        subscriber_count=creator_subscriber_count(creator),
    )


@app.route("/studio")
@login_required
def studio():
    user = current_user()
    if not user or user["role"] != "creator":
        return redirect(url_for("brand_profile"))
    creator = creator_for_user(user["id"])
    if not creator:
        abort(404)
    return redirect(url_for("creator_dashboard", slug=creator["slug"]))


@app.route("/creator/<slug>/settings", methods=["POST"])
@creator_owner_required
def creator_settings(slug, creator):
    display_name = request.form.get("display_name", "").strip()[:80]
    bio = request.form.get("bio", "").strip()[:1200]
    location = request.form.get("location", "").strip()[:120]
    custom_domain = request.form.get("custom_domain", "").strip().lower()[:253]
    if not is_admin():
        custom_domain = creator['custom_domain']
    custom_domain = re.sub(r"^https?://", "", custom_domain).split("/", 1)[0].split(":", 1)[0].rstrip(".")
    if custom_domain and (not re.match(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", custom_domain)):
        flash("Enter a valid custom domain such as creator.example.com.", "error")
        return redirect(url_for("creator_dashboard", slug=slug) + "#profile")
    domain_owner = query_one("SELECT id FROM creators WHERE custom_domain=? COLLATE NOCASE AND id!=?", (custom_domain, creator["id"])) if custom_domain else None
    if domain_owner:
        flash("That custom domain is already assigned to another creator.", "error")
        return redirect(url_for("creator_dashboard", slug=slug) + "#profile")
    try:
        price = round(float(request.form.get("subscription_price", creator["subscription_price"])), 2)
    except ValueError:
        price = float(creator["subscription_price"])
    price = max(0, min(price, 100000)) if math.isfinite(price) else float(creator["subscription_price"])
    currency = request.form.get("subscription_currency", "USD").upper()
    if currency not in {"USD", "CAD", "EUR", "GBP"}:
        currency = "USD"
    avatar_path = creator["avatar_path"]
    cover_path = creator["cover_path"]
    try:
        avatar_path = save_upload(request.files.get("avatar"), f"creators/{creator['id']}", PUBLIC_IMAGE_EXTS, public=True) or avatar_path
        cover_path = save_upload(request.files.get("cover"), f"creators/{creator['id']}", PUBLIC_IMAGE_EXTS, public=True) or cover_path
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("creator_dashboard", slug=slug) + "#profile")
    execute(
        """UPDATE creators SET display_name=?,handle=?,bio=?,location=?,custom_domain=?,subscription_price=?,subscription_currency=?,avatar_path=?,cover_path=? WHERE id=?""",
        (display_name or creator["display_name"], "@" + slug.replace("-", ""), bio, location or "Private", custom_domain, price, currency, avatar_path, cover_path, creator["id"]),
    )
    flash("Profile settings saved.", "success")
    audit("creator_settings", slug)
    return redirect(url_for("creator_dashboard", slug=slug) + "#profile")


@app.route("/creator/<slug>/product/create", methods=["POST"])
@creator_owner_required
def create_product(slug, creator):
    title = request.form.get("title", "").strip()[:120]
    description = request.form.get("description", "").strip()[:2000]
    product_type = request.form.get("type", "digital")
    if product_type not in {"digital", "physical"}:
        product_type = "digital"
    try:
        price = round(float(request.form.get("price", "0")), 2)
    except ValueError:
        price = 0
    if not title or not math.isfinite(price) or price <= 0 or price > 100000:
        flash("Product name and a positive price are required.", "error")
        return redirect(url_for("creator_dashboard", slug=slug) + "#boutique")
    currency = request.form.get("currency", "USD").upper()
    if currency not in {"USD", "CAD", "EUR", "GBP"}:
        currency = "USD"
    image_path = None
    delivery_path = None
    try:
        image_path = save_upload(request.files.get("image"), f"products/{creator['id']}", PUBLIC_IMAGE_EXTS, public=True)
        if product_type == "digital":
            delivery_path = save_upload(request.files.get("delivery_file"), f"delivery/{creator['id']}", DELIVERY_EXTS, public=False)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("creator_dashboard", slug=slug) + "#boutique")
    execute(
        """INSERT INTO products
        (id,creator_id,title,description,type,price,currency,edition,status,image_path,delivery_path,delivery_note,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "prd_" + secrets.token_hex(8),
            creator["id"],
            title,
            description,
            product_type,
            price,
            currency,
            "1/1",
            "available",
            image_path,
            delivery_path,
            request.form.get("delivery_note", "").strip()[:500],
            utcnow(),
        ),
    )
    audit("product_created", f"creator={slug} title={title}")
    flash("Product published to your boutique.", "success")
    return redirect(url_for("creator_dashboard", slug=slug) + "#boutique")


@app.route("/creator/<slug>/product/<product_id>/status", methods=["POST"])
@creator_owner_required
def product_status(slug, creator, product_id):
    product = get_product(creator["id"], product_id)
    status = request.form.get("status", "available")
    if status not in {"available", "archived"} or product["status"] in {"sold", "reserved"}:
        status = product["status"]
    db = get_db()
    if product["status"] == "reserved" and status in {"available", "archived"}:
        db.execute("UPDATE orders SET status='canceled_by_creator' WHERE product_id=? AND status='pending_payment'", (product_id,))
    db.execute("UPDATE products SET status=? WHERE id=?", (status, product_id))
    db.commit()
    flash("Product status updated.", "success")
    return redirect(url_for("creator_dashboard", slug=slug) + "#boutique")


@app.route("/creator/<slug>/post/create", methods=["POST"])
@creator_owner_required
def create_private_post(slug, creator):
    title = request.form.get("title", "").strip()[:160]
    body = request.form.get("body", "").strip()[:5000]
    if not title:
        flash("Post title is required.", "error")
        return redirect(url_for("creator_dashboard", slug=slug) + "#private-feed")
    media_path = None
    media_kind = None
    file_obj = request.files.get("media")
    if file_obj and file_obj.filename:
        try:
            media_path = save_upload(file_obj, f"posts/{creator['id']}", MEDIA_EXTS, public=False)
            media_kind = "image" if ext_of(file_obj.filename) in PUBLIC_IMAGE_EXTS else ("video" if ext_of(file_obj.filename) in {"mp4", "webm", "mov", "m4v"} else "file")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("creator_dashboard", slug=slug) + "#private-feed")
    execute(
        "INSERT INTO private_posts (id,creator_id,title,body,media_path,media_kind,created_at) VALUES (?,?,?,?,?,?,?)",
        ("post_" + secrets.token_hex(8), creator["id"], title, body, media_path, media_kind, utcnow()),
    )
    audit("private_post_created", f"creator={slug} title={title}")
    flash("Private post published.", "success")
    return redirect(url_for("creator_dashboard", slug=slug) + "#private-feed")


@app.route("/creator/<slug>/messages")
@fan_required
def fan_creator_messages(slug):
    return redirect(url_for("message_thread", slug=slug, fan_id=current_user()["id"]))


@app.route("/creator/<slug>/messages/<fan_id>", methods=["GET", "POST"])
@login_required
def message_thread(slug, fan_id):
    creator = get_creator(slug)
    fan = query_one("SELECT * FROM users WHERE id=? AND role='fan'", (fan_id,))
    if not fan:
        abort(404)
    user = current_user()
    fan_side = bool(user and user["role"] == "fan" and user["id"] == fan_id)
    creator_side = can_manage_creator(creator)
    if not fan_side and not creator_side:
        abort(403)
    if fan_side and not active_subscription(fan_id, creator["id"]):
        flash("Messaging opens after your subscription is active.", "error")
        return redirect(url_for("subscribe_creator", slug=slug))
    if request.method == "POST":
        body = request.form.get("body", "").strip()[:4000]
        if not body:
            flash("Message cannot be empty.", "error")
        else:
            sender_actor = f"user:{fan_id}" if fan_side else f"creator:{creator['id']}"
            execute(
                "INSERT INTO messages (creator_id,fan_user_id,sender_actor,body,created_at) VALUES (?,?,?,?,?)",
                (creator["id"], fan_id, sender_actor, body, utcnow()),
            )
            audit("message_sent", f"creator={slug} fan={fan_id}")
            return redirect(url_for("message_thread", slug=slug, fan_id=fan_id))
    messages = query_all("SELECT * FROM messages WHERE creator_id=? AND fan_user_id=? ORDER BY id ASC", (creator["id"], fan_id))
    offers = query_all("SELECT * FROM offers WHERE creator_id=? AND fan_user_id=? ORDER BY created_at DESC", (creator["id"], fan_id))
    return render_template("chat.html", creator=creator, fan=fan, messages=messages, offers=offers, creator_side=creator_side)


@app.route("/creator/<slug>/offer/<fan_id>", methods=["POST"])
@creator_owner_required
def create_offer(slug, creator, fan_id):
    fan = query_one("SELECT * FROM users WHERE id=? AND role='fan'", (fan_id,))
    if not fan:
        abort(404)
    title = request.form.get("title", "").strip()[:140]
    description = request.form.get("description", "").strip()[:1200]
    try:
        price = round(float(request.form.get("price", "0")), 2)
    except ValueError:
        price = 0
    currency = request.form.get("currency", "USD").upper()
    if not title or not math.isfinite(price) or price < 0 or price > 100000:
        flash("Add an offer title and valid price.", "error")
        return redirect(url_for("message_thread", slug=slug, fan_id=fan_id))
    execute(
        "INSERT INTO offers (id,creator_id,fan_user_id,title,description,price,currency,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("off_" + secrets.token_hex(8), creator["id"], fan_id, title, description, price, currency, "open", utcnow(), utcnow()),
    )
    audit("offer_created", f"creator={slug} fan={fan_id} price={price}")
    flash("Custom offer sent.", "success")
    return redirect(url_for("message_thread", slug=slug, fan_id=fan_id))


@app.route("/offer/<offer_id>/<action>", methods=["POST"])
@fan_required
def offer_action(offer_id, action):
    user = current_user()
    offer = query_one("SELECT * FROM offers WHERE id=? AND fan_user_id=?", (offer_id, user["id"]))
    if not offer:
        abort(404)
    if action not in {"accept", "decline"}:
        abort(400)
    if offer["status"] == "accepted":
        return redirect(url_for("fan_account"))
    if BILLING_MODE == "live" and action == "accept" and offer["price"] > 0:
        return billing.offer_checkout(offer)
    if action == "decline":
        if query_one("SELECT id FROM billing_invoices WHERE target_id=? AND status IN ('pending','paid')", (offer_id,)):
            abort(409, description="A checkout is already in progress for this offer.")
        status = "declined"
    elif float(offer["price"]) == 0:
        status = "accepted"
    else:
        status = "accepted_payment_pending"
    execute("UPDATE offers SET status=?,updated_at=? WHERE id=?", (status, utcnow(), offer_id))
    creator = query_one("SELECT * FROM creators WHERE id=?", (offer["creator_id"],))
    flash("Offer updated." if status != "accepted_payment_pending" else "Offer accepted. Payment processing still needs to be connected.", "success")
    return redirect(url_for("message_thread", slug=creator["slug"], fan_id=user["id"]))


@app.route("/creator/<slug>/stream/create", methods=["POST"])
@creator_owner_required
def create_stream(slug, creator):
    title = request.form.get("title", "").strip()[:160] or "Live session"
    audience = request.form.get("audience", "subscribers")
    if audience not in {"public", "subscribers", "group", "private"}:
        audience = "subscribers"
    target_user_id = None
    group_user_ids = []
    target = request.form.get("target", "").strip()
    if audience == "private":
        target_user = query_one("SELECT * FROM users WHERE role='fan' AND (id=? OR email=? COLLATE NOCASE)", (target, target))
        if not target_user:
            flash("Private live requires a valid subscriber email or user ID.", "error")
            return redirect(url_for("creator_dashboard", slug=slug) + "#live")
        target_user_id = target_user["id"]
    elif audience == "group":
        raw_members = [part.strip() for part in re.split(r"[,\n;]+", target) if part.strip()][:50]
        for member in raw_members:
            member_user = query_one(
                """SELECT u.* FROM users u JOIN subscriptions s ON s.fan_user_id=u.id
                WHERE u.role='fan' AND s.creator_id=? AND s.status='active' AND (u.id=? OR u.email=? COLLATE NOCASE)""",
                (creator["id"], member, member),
            )
            if member_user and member_user["id"] not in group_user_ids:
                group_user_ids.append(member_user["id"])
        if not group_user_ids:
            flash("Group live requires at least one active subscriber email or user ID.", "error")
            return redirect(url_for("creator_dashboard", slug=slug) + "#live")
    try:
        ppm = round(float(request.form.get("price_per_minute", "0")), 2)
    except ValueError:
        ppm = 0
    if not math.isfinite(ppm) or ppm != 0:
        flash("Live rooms are included in access. Per-minute charging is not enabled.", "error")
        return redirect(url_for("creator_dashboard", slug=slug) + "#live")
    stream_id = "live_" + secrets.token_hex(8)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO streams (id,creator_id,title,audience,target_user_id,price_per_minute,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (stream_id, creator["id"], title, audience, target_user_id, max(0, ppm), "scheduled", utcnow()),
        )
        for member_id in group_user_ids:
            db.execute("INSERT INTO stream_members (stream_id,user_id) VALUES (?,?)", (stream_id, member_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    audit("stream_created", f"creator={slug} stream={stream_id}")
    flash("Live room created. Open it and press Go Live.", "success")
    return redirect(url_for("stream_room", stream_id=stream_id))


@app.route("/stream/<stream_id>")
def stream_room(stream_id):
    stream = query_one("SELECT * FROM streams WHERE id=?", (stream_id,))
    if not stream:
        abort(404)
    creator = query_one("SELECT * FROM creators WHERE id=?", (stream["creator_id"],))
    if not stream_access(stream, creator):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        abort(403)
    host = can_manage_creator(creator)
    actor = actor_id_for_room(creator, as_host=host)
    host_actor = f"creator:{creator['id']}"
    return render_template("stream.html", stream=stream, creator=creator, is_host=host, actor_id=actor, host_actor=host_actor)


@app.route("/stream/<stream_id>/status", methods=["POST"])
def stream_status(stream_id):
    stream = query_one("SELECT * FROM streams WHERE id=?", (stream_id,))
    if not stream:
        abort(404)
    creator = query_one("SELECT * FROM creators WHERE id=?", (stream["creator_id"],))
    if not can_manage_creator(creator):
        abort(403)
    status = request.form.get("status")
    if status == "live":
        execute("UPDATE streams SET status='live',started_at=COALESCE(started_at,?) WHERE id=?", (utcnow(), stream_id))
    elif status == "ended":
        execute("UPDATE streams SET status='ended',ended_at=? WHERE id=?", (utcnow(), stream_id))
    else:
        abort(400)
    if request.headers.get("X-RTC-Request") == "1":
        return jsonify({"ok": True, "status": status})
    return redirect(url_for("stream_room", stream_id=stream_id))


@app.route("/api/stream/<stream_id>/signal", methods=["GET", "POST"])
def stream_signal(stream_id):
    stream = query_one("SELECT * FROM streams WHERE id=?", (stream_id,))
    if not stream:
        abort(404)
    creator = query_one("SELECT * FROM creators WHERE id=?", (stream["creator_id"],))
    if not stream_access(stream, creator):
        abort(403)
    host = can_manage_creator(creator)
    actor = actor_id_for_room(creator, as_host=host)
    host_actor = f"creator:{creator['id']}"
    cleanup_old_signals()
    touch_stream_session(stream_id, actor)
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        target = str(data.get("to") or (host_actor if not host else ""))[:200]
        payload = data.get("payload")
        if not target or not isinstance(payload, dict) or (not host and target != host_actor) or len(json.dumps(payload)) > 65536:
            return jsonify({"ok": False, "error": "invalid signal"}), 400
        execute(
            "INSERT INTO signals (room_type,room_id,from_actor,to_actor,payload,created_at) VALUES (?,?,?,?,?,?)",
            ("stream", stream_id, actor, target, json.dumps(payload), utcnow()),
        )
        return jsonify({"ok": True})
    after = request.args.get("after", "0")
    try:
        after_id = int(after)
    except ValueError:
        after_id = 0
    rows = query_all(
        "SELECT * FROM signals WHERE room_type='stream' AND room_id=? AND to_actor=? AND id>? ORDER BY id ASC LIMIT 100",
        (stream_id, actor, after_id),
    )
    return jsonify({"signals": [{"id": r["id"], "from": r["from_actor"], "payload": json.loads(r["payload"])} for r in rows]})


@app.route("/creator/<slug>/video/<fan_id>")
@login_required
def video_call(slug, fan_id):
    creator = get_creator(slug)
    fan = query_one("SELECT * FROM users WHERE id=? AND role='fan'", (fan_id,))
    if not fan:
        abort(404)
    user = current_user()
    fan_side = bool(user and user["role"] == "fan" and user["id"] == fan_id and active_subscription(fan_id, creator["id"]))
    creator_side = can_manage_creator(creator)
    if not fan_side and not creator_side:
        abort(403)
    room_id = f"{creator['id']}:{fan_id}"
    actor = f"creator:{creator['id']}" if creator_side else f"user:{fan_id}"
    peer_actor = f"user:{fan_id}" if creator_side else f"creator:{creator['id']}"
    return render_template("video_call.html", creator=creator, fan=fan, room_id=room_id, actor_id=actor, peer_actor=peer_actor, is_host=creator_side)


@app.route("/api/call/<path:room_id>/signal", methods=["GET", "POST"])
@login_required
def call_signal(room_id):
    try:
        creator_id, fan_id = room_id.split(":", 1)
    except ValueError:
        abort(400)
    creator = query_one("SELECT * FROM creators WHERE id=?", (creator_id,))
    fan = query_one("SELECT * FROM users WHERE id=? AND role='fan'", (fan_id,))
    if not creator or not fan:
        abort(404)
    user = current_user()
    creator_side = can_manage_creator(creator)
    fan_side = bool(user and user["role"] == "fan" and user["id"] == fan_id and active_subscription(fan_id, creator_id))
    if not creator_side and not fan_side:
        abort(403)
    actor = f"creator:{creator_id}" if creator_side else f"user:{fan_id}"
    peer = f"user:{fan_id}" if creator_side else f"creator:{creator_id}"
    cleanup_old_signals()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        payload = data.get("payload")
        if not isinstance(payload, dict) or len(json.dumps(payload)) > 65536:
            return jsonify({"ok": False}), 400
        execute(
            "INSERT INTO signals (room_type,room_id,from_actor,to_actor,payload,created_at) VALUES (?,?,?,?,?,?)",
            ("call", room_id, actor, peer, json.dumps(payload), utcnow()),
        )
        return jsonify({"ok": True})
    try:
        after = int(request.args.get("after", "0"))
    except ValueError:
        after = 0
    rows = query_all(
        "SELECT * FROM signals WHERE room_type='call' AND room_id=? AND to_actor=? AND id>? ORDER BY id ASC LIMIT 100",
        (room_id, actor, after),
    )
    return jsonify({"signals": [{"id": r["id"], "from": r["from_actor"], "payload": json.loads(r["payload"])} for r in rows]})


@app.route("/checkout/<slug>/<product_id>", methods=["POST"])
@fan_required
def checkout(slug, product_id):
    creator = get_creator(slug)
    product = get_product(creator["id"], product_id)
    if product["status"] != "available":
        flash("That one-of-one item is no longer available.", "error")
        return redirect(url_for("creator_public", slug=slug) + "#shop")
    user = current_user()
    if query_one("SELECT COUNT(*) AS n FROM orders WHERE fan_user_id=? AND status='pending_payment'",(user['id'],))['n'] >= 3:
        abort(409,description='Complete or cancel an existing order before reserving another item.')
    order_id = "SP-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3).upper()
    db = get_db()
    try:
        reserved = db.execute("UPDATE products SET status='reserved' WHERE id=? AND creator_id=? AND status='available'", (product["id"], creator["id"]))
        if reserved.rowcount != 1:
            db.rollback()
            flash("That one-of-one item was just claimed by another buyer.", "error")
            return redirect(url_for("creator_public", slug=slug) + "#shop")
        db.execute(
            """INSERT INTO orders
            (id,order_id,fan_user_id,creator_id,product_id,product_title,amount,currency,platform_fee_percent,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "ord_" + secrets.token_hex(8),
                order_id,
                user["id"],
                creator["id"],
                product["id"],
                product["title"],
                product["price"],
                product["currency"],
                creator["platform_fee_percent"],
                "pending_payment",
                utcnow(),
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    audit("order_created", order_id)
    flash("Order reserved. Continue to secure checkout to pay.", "info")
    return redirect(url_for("order_status", order_id=order_id))


@app.route("/order/<order_id>")
@login_required
def order_status(order_id):
    order = query_one(
        """SELECT o.*,c.slug,c.display_name AS creator_name,p.delivery_path,p.delivery_note,p.type AS product_type
        FROM orders o JOIN creators c ON c.id=o.creator_id JOIN products p ON p.id=o.product_id WHERE o.order_id=?""",
        (order_id,),
    )
    if not order:
        abort(404)
    user = current_user()
    creator = query_one("SELECT * FROM creators WHERE id=?", (order["creator_id"],))
    if not is_admin() and not can_manage_creator(creator) and not (user and user["id"] == order["fan_user_id"]):
        abort(403)
    return render_template("order.html", order=order, shipping=billing.order_shipping(order), invoice=billing.order_invoice(order) if order["status"] == "pending_payment" else None)


@app.route("/order/<order_id>/cancel", methods=["POST"])
@login_required
def cancel_order(order_id):
    order = query_one("SELECT * FROM orders WHERE order_id=?", (order_id,))
    if not order:
        abort(404)
    creator = query_one("SELECT * FROM creators WHERE id=?", (order["creator_id"],))
    user = current_user()
    authorized = is_admin() or can_manage_creator(creator) or bool(user and user["id"] == order["fan_user_id"])
    if not authorized:
        abort(403)
    if query_one("SELECT id FROM billing_invoices WHERE target_id=? AND provider IS NOT NULL AND status='pending'", (order_id,)):
        flash("Check payment status before canceling. An open provider checkout must expire first.", "error")
        return redirect(url_for("order_status", order_id=order_id))
    if order["status"] != "pending_payment":
        flash("Only pending orders can be canceled.", "error")
        return redirect(url_for("order_status", order_id=order_id))
    db = get_db()
    db.execute('BEGIN IMMEDIATE')
    open_payment = db.execute("SELECT id FROM billing_invoices WHERE target_id=? AND provider IS NOT NULL AND status='pending'", (order_id,)).fetchone()
    fresh = db.execute('SELECT status FROM orders WHERE order_id=?', (order_id,)).fetchone()
    if open_payment or fresh['status'] != 'pending_payment':
        db.rollback()
        abort(409, description='Payment or order state changed; refresh the order.')
    db.execute("UPDATE billing_invoices SET status='expired' WHERE kind='order' AND target_id=? AND status='pending'",(order_id,))
    db.execute("UPDATE orders SET status='canceled' WHERE order_id=?", (order_id,))
    db.execute("UPDATE products SET status='available' WHERE id=? AND status='reserved'", (order["product_id"],))
    db.commit()
    audit("order_canceled", order_id)
    flash("Order canceled and the one-of-one item was released.", "success")
    return redirect(url_for("creator_public", slug=creator["slug"]))


@app.route("/protected/<path:relpath>")
@login_required
def protected_media(relpath):
    relpath = relpath.replace("\\", "/")
    if not relpath.startswith("private/") or ".." in Path(relpath).parts:
        abort(404)
    user = current_user()
    post = query_one("SELECT p.*,c.slug,c.owner_user_id FROM private_posts p JOIN creators c ON c.id=p.creator_id WHERE p.media_path=?", (relpath,))
    if post:
        creator = query_one("SELECT * FROM creators WHERE id=?", (post["creator_id"],))
        if not can_manage_creator(creator) and not (user and user["role"] == "fan" and active_subscription(user["id"], creator["id"])):
            abort(403)
        return send_from_directory(UPLOAD_ROOT, relpath, as_attachment=False)
    product = query_one("SELECT p.*,c.owner_user_id FROM products p JOIN creators c ON c.id=p.creator_id WHERE p.delivery_path=?", (relpath,))
    if product:
        creator = query_one("SELECT * FROM creators WHERE id=?", (product["creator_id"],))
        if can_manage_creator(creator):
            return send_from_directory(UPLOAD_ROOT, relpath, as_attachment=True)
        order = query_one("SELECT * FROM orders WHERE product_id=? AND fan_user_id=? AND status='paid'", (product["id"], user["id"] if user else ""))
        if not order:
            abort(403)
        return send_from_directory(UPLOAD_ROOT, relpath, as_attachment=True)
    abort(404)


@app.route("/media/<path:relpath>")
def public_media(relpath):
    relpath = relpath.replace("\\", "/")
    if not relpath.startswith("public/") or ".." in Path(relpath).parts:
        abort(404)
    return send_from_directory(UPLOAD_ROOT, relpath, as_attachment=False)


@app.route("/report", methods=["GET", "POST"])
def report_content():
    creator_slug = (request.values.get("creator") or "").strip()
    creator = query_one("SELECT * FROM creators WHERE slug=? COLLATE NOCASE", (creator_slug,)) if creator_slug else None
    if request.method == "POST":
        reason = request.form.get("reason", "").strip()[:120]
        details = request.form.get("details", "").strip()[:4000]
        reporter_email = request.form.get("reporter_email", "").strip().lower()[:254]
        content_type = request.form.get("content_type", "profile").strip()[:40]
        content_id = request.form.get("content_id", "").strip()[:120]
        if not reason:
            flash("Choose a report reason.", "error")
            return render_template("report.html", creator=creator, content_type=content_type, content_id=content_id), 400
        user = current_user()
        if user and not reporter_email:
            reporter_email = user["email"]
        execute(
            """INSERT INTO reports (id,reporter_user_id,reporter_email,creator_id,content_type,content_id,reason,details,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "rep_" + secrets.token_hex(8),
                user["id"] if user else None,
                reporter_email or None,
                creator["id"] if creator else None,
                content_type or "profile",
                content_id or None,
                reason,
                details,
                "open",
                utcnow(),
            ),
        )
        audit("content_report", f"creator={creator_slug} reason={reason}")
        flash("Report received. The platform administrator can now review it.", "success")
        return redirect(url_for("creator_public", slug=creator_slug) if creator else url_for("brand_profile"))
    return render_template(
        "report.html",
        creator=creator,
        content_type=request.args.get("content_type", "profile"),
        content_id=request.args.get("content_id", ""),
    )


@app.route("/admin")
@admin_required
def admin_dashboard():
    creators = query_all(
        """SELECT c.*,u.email AS owner_email FROM creators c LEFT JOIN users u ON u.id=c.owner_user_id ORDER BY c.created_at DESC"""
    )
    users = query_all("SELECT * FROM users ORDER BY created_at DESC LIMIT 100")
    subscriptions = query_all(
        """SELECT s.*,u.display_name AS fan_name,u.email AS fan_email,c.display_name AS creator_name,c.slug
        FROM subscriptions s JOIN users u ON u.id=s.fan_user_id JOIN creators c ON c.id=s.creator_id
        ORDER BY s.created_at DESC LIMIT 100"""
    )
    orders = query_all(
        """SELECT o.*,u.display_name AS fan_name,c.display_name AS creator_name,c.slug
        FROM orders o JOIN users u ON u.id=o.fan_user_id JOIN creators c ON c.id=o.creator_id
        ORDER BY o.created_at DESC LIMIT 100"""
    )
    reports = query_all(
        """SELECT r.*,c.display_name AS creator_name,c.slug FROM reports r
        LEFT JOIN creators c ON c.id=r.creator_id ORDER BY CASE WHEN r.status='open' THEN 0 ELSE 1 END,r.created_at DESC LIMIT 100"""
    )
    metrics = {
        "users": query_one("SELECT COUNT(*) AS n FROM users")["n"],
        "creators": query_one("SELECT COUNT(*) AS n FROM creators")["n"],
        "active_subscriptions": query_one("SELECT COUNT(*) AS n FROM subscriptions WHERE status='active'")["n"],
        "pending_orders": query_one("SELECT COUNT(*) AS n FROM orders WHERE status='pending_payment'")["n"],
        "open_reports": query_one("SELECT COUNT(*) AS n FROM reports WHERE status='open'")["n"],
    }
    return render_template("admin.html", creators=creators, users=users, subscriptions=subscriptions, orders=orders, reports=reports, metrics=metrics)


@app.route("/admin/report/<report_id>/resolve", methods=["POST"])
@admin_required
def admin_resolve_report(report_id):
    report = query_one("SELECT * FROM reports WHERE id=?", (report_id,))
    if not report:
        abort(404)
    execute("UPDATE reports SET status='resolved',resolved_at=? WHERE id=?", (utcnow(), report_id))
    audit("admin_report_resolved", report_id)
    flash("Report marked resolved.", "success")
    return redirect(url_for("admin_dashboard") + "#reports")


@app.route("/admin/subscription/<subscription_id>/activate", methods=["POST"])
@admin_required
def admin_activate_subscription(subscription_id):
    if BILLING_MODE == 'live':
        abort(409, description='Live access is activated only after verified payment.')
    sub = query_one("SELECT * FROM subscriptions WHERE id=?", (subscription_id,))
    if not sub:
        abort(404)
    execute("UPDATE subscriptions SET status='active',billing_state='manual_confirmed',activated_at=?,expires_at=? WHERE id=?", (utcnow(), (datetime.now(timezone.utc)+timedelta(days=30)).isoformat(), subscription_id))
    audit("admin_subscription_activate", subscription_id)
    flash("Subscription activated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/order/<order_id>/mark-paid", methods=["POST"])
@admin_required
def admin_mark_order_paid(order_id):
    if BILLING_MODE == 'live':
        abort(409, description='Live orders are fulfilled only after verified payment.')
    finalize_order_paid(order_id, payment_reference="admin_manual_confirmation")
    audit("admin_order_paid", order_id)
    flash("Order marked paid and product retired as sold.", "success")
    return redirect(url_for("order_status", order_id=order_id))


@app.route("/admin/user/<user_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(user_id):
    user = query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not user:
        abort(404)
    new_status = "suspended" if user["status"] == "active" else "active"
    execute("UPDATE users SET status=? WHERE id=?", (new_status, user_id))
    audit("admin_user_toggle", f"{user_id}:{new_status}")
    flash(f"User is now {new_status}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/creator/<creator_id>/assign", methods=["POST"])
@admin_required
def admin_assign_creator(creator_id):
    creator = query_one("SELECT * FROM creators WHERE id=?", (creator_id,))
    if not creator:
        abort(404)
    if creator["owner_user_id"]:
        flash("That creator already has a login owner.", "error")
        return redirect(url_for("admin_dashboard"))
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(password) < 8:
        flash("Enter a valid email and a password of at least 8 characters.", "error")
        return redirect(url_for("admin_dashboard"))
    if query_one("SELECT id FROM users WHERE email=? COLLATE NOCASE", (email,)):
        flash("That login email is already in use.", "error")
        return redirect(url_for("admin_dashboard"))
    user_id = "usr_" + secrets.token_hex(8)
    db = get_db()
    db.execute(
        "INSERT INTO users (id,role,email,password_hash,display_name,adult_confirmed,sp_nudes_balance,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, "creator", email, generate_password_hash(password), creator["display_name"], 1, 0, "active", utcnow()),
    )
    db.execute("UPDATE creators SET owner_user_id=?,identity_status='assigned' WHERE id=?", (user_id, creator_id))
    db.commit()
    audit("admin_creator_assigned", f"creator={creator['slug']} email={email}")
    flash("Creator login assigned.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<user_id>/credit-sp", methods=["POST"])
@admin_required
def admin_credit_sp(user_id):
    user = query_one("SELECT * FROM users WHERE id=? AND role='fan'", (user_id,))
    if not user:
        abort(404)
    try:
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        amount = 0
    if amount < 1 or amount > 1000000:
        flash("Credit amount must be between 1 and 1,000,000 SP-Nudes.", "error")
        return redirect(url_for("admin_dashboard"))
    execute("UPDATE users SET sp_nudes_balance=sp_nudes_balance+? WHERE id=?", (amount, user_id))
    audit("admin_credit_sp", f"user={user_id} amount={amount}")
    flash(f"Credited {amount} SP-Nudes.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/webhooks/payment", methods=["POST"])
def payment_webhook():
    # Deliberate integration boundary. Replace with verified payment-provider callback logic.
    return jsonify({"ok": False, "error": "payment provider not connected"}), 501


@app.route("/robots.txt")
def robots():
    return app.response_class("User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /account\nDisallow: /creator/*/dashboard\n", mimetype="text/plain")


@app.route("/health")
def health():
    try:
        query_one("SELECT 1 AS ok")
        database = "ok"
    except Exception:
        database = "error"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "service": "smell-panties-platform",
        "database": database,
        "storage": str(STORAGE_ROOT),
        "billing_mode": BILLING_MODE,
        "temporary_preview": os.getenv("FREE_RENDER_PREVIEW", "true").lower() == "true",
        "admin_configured": bool(ADMIN_PASSWORD),
    }, (200 if database == "ok" and ADMIN_PASSWORD else 503)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(500)
def polished_error(error):
    code = getattr(error, "code", 500) or 500
    description = getattr(error, "description", "Something went wrong.")
    return render_template("error.html", code=code, description=description), code


import billing
import sys
billing.install(app, sys.modules[__name__])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
