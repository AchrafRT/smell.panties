"""Affiliate accounting independent of the host application's user tables.

Use a sqlite3.Connection whose row_factory is sqlite3.Row. Except for
``init_schema``, mutations never commit: the caller owns the transaction.
``record_verified_sale`` is a server-side adapter boundary, not a public checkout
API. Its caller must authenticate the payment provider and verify settlement.
Amounts are integer minor units; taxes are excluded from the commission basis.
"""

from __future__ import annotations

import calendar
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash


UTC = timezone.utc
MAX_CENTS = 1_000_000_000_000


class AffiliateError(ValueError):
    """A safe business-rule error with a stable ``code`` for HTTP adapters."""

    def __init__(self, message: str, code: str = "invalid_input"):
        super().__init__(message)
        self.code = code


DomainError = AffiliateError


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _dt(value: Any, name: str = "timestamp") -> datetime:
    try:
        if isinstance(value, bool):
            raise ValueError()
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, (int, float)):
            result = datetime.fromtimestamp(value, UTC)
        elif isinstance(value, str):
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            raise ValueError()
        if result.tzinfo is None:
            result = result.replace(tzinfo=UTC)
        return result.astimezone(UTC).replace(microsecond=0)
    except (ValueError, OverflowError, OSError, TypeError) as exc:
        raise AffiliateError(f"Invalid {name}.") from exc


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _year_later(value: datetime) -> datetime:
    year = value.year + 1
    return value.replace(year=year, day=min(value.day, calendar.monthrange(year, value.month)[1]))


def _identifier(value: Any, name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AffiliateError(f"{name} is required.")
    result = str(value).strip()
    if not result or len(result) > 255 or any(ord(c) < 32 for c in result):
        raise AffiliateError(f"Invalid {name}.")
    return result


def _integer(value: Any, name: str, *, minimum: int = 0, maximum: int = MAX_CENTS) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AffiliateError(f"{name} must be an integer between {minimum} and {maximum}.")
    return value


def _currency(value: Any) -> str:
    if not isinstance(value, str):
        raise AffiliateError("A three-letter currency code is required.")
    value = value.strip().upper()
    if len(value) != 3 or not value.isascii() or not value.isalpha():
        raise AffiliateError("A three-letter currency code is required.")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise AffiliateError(f"{name} must be true or false.")
    return value


def _round_bps(amount: int, bps: int) -> int:
    return (amount * bps + 5000) // 10000


def _dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("is_demo", "allowed", "program_enabled", "cancel_at_period_end", "active"):
        if key in result:
            result[key] = bool(result[key])
    return result


@contextmanager
def _atomic(conn: sqlite3.Connection):
    # Releasing an outermost SQLite savepoint would commit. Start a caller-owned
    # outer transaction first so even standalone mutations remain uncommitted.
    if not conn.in_transaction:
        conn.execute("BEGIN")
    name = "aff_" + secrets.token_hex(8)
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {name}")


def init_schema(conn: sqlite3.Connection) -> None:
    """Create additive aff_* tables and defaults; commits setup only.

    Call this outside a host-application transaction. There are no foreign keys
    to host tables and no changes to the host's SQLite connection PRAGMAs.
    """
    if conn.in_transaction:
        raise AffiliateError("Initialize affiliate schema outside an existing transaction.", "transaction_active")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS aff_settings (
      id INTEGER PRIMARY KEY CHECK(id=1), annual_price_cents INTEGER NOT NULL,
      currency TEXT NOT NULL, creator_rate_bps INTEGER NOT NULL, fan_rate_bps INTEGER NOT NULL,
      attribution_window_days INTEGER NOT NULL, program_enabled INTEGER NOT NULL,
      version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS aff_affiliates (
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
      password_hash TEXT NOT NULL, display_name TEXT NOT NULL, code TEXT NOT NULL UNIQUE,
      linked_user_id TEXT UNIQUE, status TEXT NOT NULL CHECK(status IN ('active','suspended','disabled')),
      is_demo INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS aff_creator_preferences (
      creator_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, allowed INTEGER NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS aff_account_referrals (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL UNIQUE,
      affiliate_id INTEGER NOT NULL REFERENCES aff_affiliates(id), rate_bps INTEGER NOT NULL,
      terms_version INTEGER NOT NULL, membership_eligible INTEGER,
      membership_locked_at TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS aff_subscription_referrals (
      id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id TEXT NOT NULL UNIQUE,
      fan_user_id TEXT NOT NULL, creator_id TEXT NOT NULL,
      affiliate_id INTEGER NOT NULL REFERENCES aff_affiliates(id), rate_bps INTEGER NOT NULL,
      terms_version INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
      CHECK(status IN ('pending','locked','ineligible')), consent_at TEXT NOT NULL,
      locked_at TEXT, ineligible_reason TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS aff_clicks (
      id INTEGER PRIMARY KEY AUTOINCREMENT, affiliate_id INTEGER NOT NULL REFERENCES aff_affiliates(id),
      creator_id TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS aff_sales (
      id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, payment_id TEXT NOT NULL,
      event_id TEXT, kind TEXT NOT NULL CHECK(kind IN ('membership','subscription')),
      user_id TEXT NOT NULL, creator_id TEXT NOT NULL, subscription_id TEXT,
      amount_cents INTEGER NOT NULL CHECK(amount_cents>0), currency TEXT NOT NULL,
      affiliate_id INTEGER REFERENCES aff_affiliates(id), referral_id INTEGER,
      commission_rate_bps INTEGER NOT NULL, commission_cents INTEGER NOT NULL,
      creator_deduction_cents INTEGER NOT NULL, platform_fee_bps INTEGER NOT NULL,
      platform_fee_cents INTEGER NOT NULL, creator_net_cents INTEGER NOT NULL,
      platform_net_cents INTEGER NOT NULL, period_start TEXT, period_end TEXT,
      is_demo INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
      UNIQUE(provider,payment_id)
    );
    CREATE INDEX IF NOT EXISTS aff_sales_subscription ON aff_sales(subscription_id,kind);
    CREATE INDEX IF NOT EXISTS aff_sales_creator ON aff_sales(creator_id,kind,is_demo);
    CREATE TABLE IF NOT EXISTS aff_memberships (
      creator_id TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0,
      provider TEXT NOT NULL, subscription_id TEXT, period_start TEXT NOT NULL,
      period_end TEXT NOT NULL, cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
      source_sale_id INTEGER NOT NULL REFERENCES aff_sales(id), updated_at TEXT NOT NULL,
      PRIMARY KEY(creator_id,is_demo)
    );
    CREATE TABLE IF NOT EXISTS aff_refunds (
      id INTEGER PRIMARY KEY AUTOINCREMENT, sale_id INTEGER NOT NULL REFERENCES aff_sales(id),
      provider TEXT NOT NULL, refund_id TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
      commission_reversed_cents INTEGER NOT NULL, creator_deduction_reversed_cents INTEGER NOT NULL,
      platform_fee_reversed_cents INTEGER NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(provider,refund_id)
    );
    CREATE TABLE IF NOT EXISTS aff_commission_ledger (
      id INTEGER PRIMARY KEY AUTOINCREMENT, affiliate_id INTEGER NOT NULL REFERENCES aff_affiliates(id),
      sale_id INTEGER NOT NULL REFERENCES aff_sales(id), refund_id INTEGER REFERENCES aff_refunds(id),
      entry_type TEXT NOT NULL CHECK(entry_type IN ('commission','reversal')),
      amount_cents INTEGER NOT NULL, creator_deduction_cents INTEGER NOT NULL,
      currency TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS aff_one_commission_per_sale
      ON aff_commission_ledger(sale_id) WHERE entry_type='commission';
    CREATE UNIQUE INDEX IF NOT EXISTS aff_one_reversal_per_refund
      ON aff_commission_ledger(refund_id) WHERE entry_type='reversal';
    CREATE TABLE IF NOT EXISTS aff_payment_holds (
      sale_id INTEGER PRIMARY KEY REFERENCES aff_sales(id), active INTEGER NOT NULL,
      reason TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS aff_payouts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, affiliate_id INTEGER NOT NULL REFERENCES aff_affiliates(id),
      amount_cents INTEGER NOT NULL CHECK(amount_cents>0), currency TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('pending','held','paid','failed')),
      provider_reference TEXT UNIQUE, failure_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS aff_audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL, action TEXT NOT NULL,
      entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS aff_ledger_no_update BEFORE UPDATE ON aff_commission_ledger
      BEGIN SELECT RAISE(ABORT,'Affiliate ledger is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS aff_ledger_no_delete BEFORE DELETE ON aff_commission_ledger
      BEGIN SELECT RAISE(ABORT,'Affiliate ledger is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS aff_sales_no_update BEFORE UPDATE ON aff_sales
      BEGIN SELECT RAISE(ABORT,'Affiliate sales are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS aff_sales_no_delete BEFORE DELETE ON aff_sales
      BEGIN SELECT RAISE(ABORT,'Affiliate sales are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS aff_refunds_no_update BEFORE UPDATE ON aff_refunds
      BEGIN SELECT RAISE(ABORT,'Affiliate refunds are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS aff_refunds_no_delete BEFORE DELETE ON aff_refunds
      BEGIN SELECT RAISE(ABORT,'Affiliate refunds are append-only'); END;
    """)
    conn.execute("""INSERT OR IGNORE INTO aff_settings
        (id,annual_price_cents,currency,creator_rate_bps,fan_rate_bps,attribution_window_days,
         program_enabled,version,updated_at) VALUES (1,5400,'USD',5000,300,30,1,1,?)""", (_stamp(),))
    conn.commit()


def _audit(conn, actor_id, action, entity_type, entity_id, details=None):
    actor = _identifier(actor_id if actor_id is not None else "system", "actor_id")
    conn.execute("""INSERT INTO aff_audit_log
        (actor_id,action,entity_type,entity_id,details_json,created_at) VALUES (?,?,?,?,?,?)""",
        (actor, action, entity_type, str(entity_id), json.dumps(details or {}, sort_keys=True), _stamp()))


def get_settings(conn: sqlite3.Connection) -> dict:
    """Return annual_price_cents, currency, rates, attribution window and state."""
    result = _dict(conn.execute("SELECT * FROM aff_settings WHERE id=1").fetchone())
    if result is None:
        raise AffiliateError("Affiliate schema has not been initialized.", "not_initialized")
    return result


def update_settings(conn: sqlite3.Connection, *, actor_id: Any, **changes) -> dict:
    """Update named program settings; old attribution/sale snapshots are retained."""
    allowed = {"annual_price_cents", "currency", "creator_rate_bps", "fan_rate_bps",
               "attribution_window_days", "program_enabled"}
    if set(changes) - allowed:
        raise AffiliateError("Unknown affiliate setting.")
    data = {}
    for key, value in changes.items():
        if key == "currency":
            data[key] = _currency(value)
        elif key == "program_enabled":
            data[key] = int(_boolean(value, key))
        elif key.endswith("rate_bps"):
            data[key] = _integer(value, key, maximum=10000)
        elif key == "attribution_window_days":
            data[key] = _integer(value, key, minimum=1, maximum=365)
        else:
            data[key] = _integer(value, key, minimum=1)
    with _atomic(conn):
        old = get_settings(conn)
        if data.get("currency", old["currency"]) != old["currency"] and conn.execute("SELECT 1 FROM aff_sales LIMIT 1").fetchone():
            raise AffiliateError("Currency cannot change after financial activity; use a separate program.", "currency_locked")
        if data:
            fields = ", ".join(f"{key}=?" for key in data)
            conn.execute(f"UPDATE aff_settings SET {fields},version=version+1,updated_at=? WHERE id=1",
                         [*data.values(), _stamp()])
            _audit(conn, actor_id, "settings.updated", "settings", 1, data)
    return get_settings(conn)


def _affiliate_row(conn, affiliate_id=None, *, code=None, username=None):
    if code is not None:
        row = conn.execute("SELECT * FROM aff_affiliates WHERE code=?", (_identifier(code, "affiliate_code"),)).fetchone()
    elif username is not None:
        row = conn.execute("SELECT * FROM aff_affiliates WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM aff_affiliates WHERE id=?", (affiliate_id,)).fetchone()
    if row is None:
        raise AffiliateError("Affiliate account was not found.", "not_found")
    return row


def _public_affiliate(row) -> dict:
    result = _dict(row)
    result.pop("password_hash", None)
    return result


def get_affiliate(conn: sqlite3.Connection, affiliate_id=None, *, code=None) -> dict:
    """Return one affiliate without password material, selected by ID or code."""
    return _public_affiliate(_affiliate_row(conn, affiliate_id, code=code))


def _hash_password(password=None, password_hash=None) -> str:
    if (password is None) == (password_hash is None):
        raise AffiliateError("Supply exactly one password or precomputed password hash.")
    if password_hash is not None:
        if not isinstance(password_hash, str) or len(password_hash) < 30 or "$" not in password_hash or len(password_hash) > 2048:
            raise AffiliateError("Invalid password hash.")
        return password_hash
    if not isinstance(password, str) or not 4 <= len(password) <= 1024:
        raise AffiliateError("Password must be between 4 and 1024 characters.")
    return generate_password_hash(password)


def create_affiliate(conn: sqlite3.Connection, *, username: str, password=None, password_hash=None,
                     display_name=None, linked_user_id=None, code=None, is_demo=False, actor_id=None) -> dict:
    """Create an account with an opaque URL-safe code; return its public fields."""
    username = _identifier(username, "username").lower()
    if any(c.isspace() for c in username):
        raise AffiliateError("Username cannot contain whitespace.")
    name = _identifier(display_name or username, "display_name")
    linked = _identifier(linked_user_id, "linked_user_id", optional=True)
    code = code or secrets.token_urlsafe(18)
    if not isinstance(code, str) or not 12 <= len(code) <= 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in code):
        raise AffiliateError("Referral code must be 12–128 URL-safe characters.")
    hashed = _hash_password(password, password_hash)
    is_demo = _boolean(is_demo, "is_demo")
    with _atomic(conn):
        try:
            now = _stamp()
            cursor = conn.execute("""INSERT INTO aff_affiliates
                (username,password_hash,display_name,code,linked_user_id,status,is_demo,created_at,updated_at)
                VALUES (?,?,?,?,?,'active',?,?,?)""", (username, hashed, name, code, linked, int(is_demo), now, now))
        except sqlite3.IntegrityError as exc:
            raise AffiliateError("Username, linked user, or referral code already exists.", "already_exists") from exc
        affiliate_id = cursor.lastrowid
        _audit(conn, actor_id, "affiliate.created", "affiliate", affiliate_id, {"username": username, "is_demo": is_demo})
    return get_affiliate(conn, affiliate_id)


def authenticate_affiliate(conn: sqlite3.Connection, username: str, password: str) -> dict | None:
    """Return an active public affiliate, or None; caller applies rate limiting."""
    if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
        return None
    row = conn.execute("SELECT * FROM aff_affiliates WHERE username=? COLLATE NOCASE", (username.strip(),)).fetchone()
    if row is None:
        return None
    try:
        valid = check_password_hash(row["password_hash"], password)
    except (ValueError, TypeError):
        return None
    if not valid or row["status"] != "active":
        return None
    return _public_affiliate(row)


def update_affiliate(conn: sqlite3.Connection, affiliate_id, *, actor_id, **changes) -> dict:
    """Edit display_name, linked_user_id, status, password or password_hash."""
    if set(changes) - {"display_name", "linked_user_id", "status", "password", "password_hash"}:
        raise AffiliateError("Unknown affiliate field.")
    fields = {}
    if "display_name" in changes:
        fields["display_name"] = _identifier(changes["display_name"], "display_name")
    if "linked_user_id" in changes:
        fields["linked_user_id"] = _identifier(changes["linked_user_id"], "linked_user_id", optional=True)
    if "status" in changes:
        if changes["status"] not in ("active", "suspended", "disabled"):
            raise AffiliateError("Invalid affiliate status.")
        fields["status"] = changes["status"]
    if "password" in changes or "password_hash" in changes:
        fields["password_hash"] = _hash_password(changes.get("password"), changes.get("password_hash"))
    with _atomic(conn):
        _affiliate_row(conn, affiliate_id)
        if fields:
            try:
                conn.execute(f"UPDATE aff_affiliates SET {','.join(k+'=?' for k in fields)},updated_at=? WHERE id=?",
                             [*fields.values(), _stamp(), affiliate_id])
            except sqlite3.IntegrityError as exc:
                raise AffiliateError("Linked user already belongs to another affiliate.", "already_exists") from exc
            _audit(conn, actor_id, "affiliate.updated", "affiliate", affiliate_id, {"fields": sorted(fields)})
    return get_affiliate(conn, affiliate_id)


def disable_affiliate(conn: sqlite3.Connection, affiliate_id, *, actor_id) -> dict:
    """Soft-delete access while retaining earned balances and audit history."""
    return update_affiliate(conn, affiliate_id, actor_id=actor_id, status="disabled")


def seed_demo_affiliates(conn: sqlite3.Connection, *, password_hash: str, actor_id="system") -> list[dict]:
    """Idempotently create affiliate1/affiliate2 with zero initial earnings."""
    _hash_password(password_hash=password_hash)
    result = []
    with _atomic(conn):
        for i in (1, 2):
            username = f"affiliate{i}"
            row = conn.execute("SELECT * FROM aff_affiliates WHERE username=?", (username,)).fetchone()
            if row:
                if not row["is_demo"]:
                    raise AffiliateError("Demo username conflicts with a live affiliate.", "mode_mismatch")
                result.append(_public_affiliate(row))
            else:
                result.append(create_affiliate(conn, username=username, password_hash=password_hash,
                    display_name=f"Demo affiliate {i}", linked_user_id=f"demo-affiliate-{i}",
                    is_demo=True, actor_id=actor_id))
    return result


def get_creator_preference(conn: sqlite3.Connection, creator_id) -> dict:
    creator_id = _identifier(creator_id, "creator_id")
    row = conn.execute("SELECT * FROM aff_creator_preferences WHERE creator_id=?", (creator_id,)).fetchone()
    return _dict(row) if row else {"creator_id": creator_id, "owner_user_id": creator_id, "allowed": False, "updated_at": None}


def set_creator_opt_in(conn: sqlite3.Connection, creator_id, allowed: bool, *, actor_id, owner_user_id=None) -> dict:
    """Record the creator's consent and owner identity; defaults to opted out.

    Caller must authorize the actor as owner/admin. Opt-out stops new referrals;
    an attribution locked by an already-paid subscription remains valid.
    """
    creator_id = _identifier(creator_id, "creator_id")
    allowed = _boolean(allowed, "allowed")
    with _atomic(conn):
        old = get_creator_preference(conn, creator_id)
        owner = _identifier(owner_user_id if owner_user_id is not None else old["owner_user_id"], "owner_user_id")
        conn.execute("""INSERT INTO aff_creator_preferences(creator_id,owner_user_id,allowed,updated_at)
            VALUES (?,?,?,?) ON CONFLICT(creator_id) DO UPDATE SET owner_user_id=excluded.owner_user_id,
            allowed=excluded.allowed,updated_at=excluded.updated_at""", (creator_id, owner, int(allowed), _stamp()))
        _audit(conn, actor_id, "creator.consent_changed", "creator", creator_id,
               {"allowed": allowed, "previous_allowed": old["allowed"], "owner_user_id": owner})
    return get_creator_preference(conn, creator_id)


def _eligible_affiliate(conn, code):
    affiliate = _affiliate_row(conn, code=code)
    if affiliate["status"] != "active":
        raise AffiliateError("This affiliate cannot accept new referrals.", "affiliate_inactive")
    if not get_settings(conn)["program_enabled"]:
        raise AffiliateError("The affiliate program is paused.", "program_paused")
    return affiliate


def get_account_referral(conn: sqlite3.Connection, user_id) -> dict | None:
    return _dict(conn.execute("SELECT * FROM aff_account_referrals WHERE user_id=?",
                             (_identifier(user_id, "user_id"),)).fetchone())


def attribute_account(conn: sqlite3.Connection, user_id, affiliate_code, *, actor_id=None) -> dict:
    """Lock the first signup attribution; this creates no commission or payout."""
    user_id = _identifier(user_id, "user_id")
    with _atomic(conn):
        old = get_account_referral(conn, user_id)
        if old:
            return {**old, "created": False}
        affiliate = _eligible_affiliate(conn, affiliate_code)
        if affiliate["linked_user_id"] == user_id:
            raise AffiliateError("Self-referrals are not eligible.", "self_referral")
        # Account attribution is for new accounts, never retroactive membership fees.
        if conn.execute("SELECT 1 FROM aff_sales WHERE kind='membership' AND user_id=? LIMIT 1", (user_id,)).fetchone():
            raise AffiliateError("A paid creator membership cannot be referred retroactively.", "already_paid")
        settings = get_settings(conn)
        conn.execute("""INSERT INTO aff_account_referrals
            (user_id,affiliate_id,rate_bps,terms_version,created_at) VALUES (?,?,?,?,?)""",
            (user_id, affiliate["id"], settings["creator_rate_bps"], settings["version"], _stamp()))
        _audit(conn, actor_id, "account.attributed", "user", user_id, {"affiliate_id": affiliate["id"]})
    return {**get_account_referral(conn, user_id), "created": True}


def get_subscription_referral(conn: sqlite3.Connection, subscription_id) -> dict | None:
    return _dict(conn.execute("SELECT * FROM aff_subscription_referrals WHERE subscription_id=?",
                             (_identifier(subscription_id, "subscription_id"),)).fetchone())


def attribute_subscription(conn: sqlite3.Connection, subscription_id, fan_user_id, creator_id,
                           affiliate_code, *, actor_id=None) -> dict:
    """Capture a pending checkout referral; first verified payment locks consent.

    Capture only after validating the signed, unexpired creator-specific cookie.
    Never infer this attribution from a generic platform signup referral.
    """
    subscription_id = _identifier(subscription_id, "subscription_id")
    fan_user_id = _identifier(fan_user_id, "fan_user_id")
    creator_id = _identifier(creator_id, "creator_id")
    with _atomic(conn):
        old = get_subscription_referral(conn, subscription_id)
        if old:
            if old["fan_user_id"] != fan_user_id or old["creator_id"] != creator_id:
                raise AffiliateError("Subscription attribution does not match this customer and creator.", "identity_mismatch")
            return {**old, "created": False}
        if conn.execute("SELECT 1 FROM aff_sales WHERE kind='subscription' AND subscription_id=? LIMIT 1", (subscription_id,)).fetchone():
            raise AffiliateError("A paid subscription cannot be referred retroactively.", "already_paid")
        affiliate = _eligible_affiliate(conn, affiliate_code)
        preference = get_creator_preference(conn, creator_id)
        if not preference["allowed"]:
            raise AffiliateError("This creator has not opted into affiliate promotion.", "creator_opted_out")
        if fan_user_id == preference["owner_user_id"] or affiliate["linked_user_id"] in (fan_user_id, creator_id, preference["owner_user_id"]):
            raise AffiliateError("Self-referrals are not eligible.", "self_referral")
        settings = get_settings(conn)
        now = _stamp()
        conn.execute("""INSERT INTO aff_subscription_referrals
            (subscription_id,fan_user_id,creator_id,affiliate_id,rate_bps,terms_version,consent_at,created_at)
            VALUES (?,?,?,?,?,?,?,?)""", (subscription_id, fan_user_id, creator_id, affiliate["id"],
            settings["fan_rate_bps"], settings["version"], preference["updated_at"] or now, now))
        _audit(conn, actor_id, "subscription.attribution_captured", "subscription", subscription_id,
               {"affiliate_id": affiliate["id"], "creator_id": creator_id})
    return {**get_subscription_referral(conn, subscription_id), "created": True}


def record_click(conn: sqlite3.Connection, affiliate_code, *, creator_id=None) -> dict:
    """Count an eligible referral click without storing IP, device or identity."""
    with _atomic(conn):
        affiliate = _eligible_affiliate(conn, affiliate_code)
        creator_id = _identifier(creator_id, "creator_id", optional=True)
        if creator_id is not None and not get_creator_preference(conn, creator_id)["allowed"]:
            raise AffiliateError("This creator has not opted into affiliate promotion.", "creator_opted_out")
        now = _stamp()
        cursor = conn.execute("INSERT INTO aff_clicks(affiliate_id,creator_id,created_at) VALUES (?,?,?)",
                              (affiliate["id"], creator_id, now))
    return {"id": cursor.lastrowid, "affiliate_id": affiliate["id"], "creator_id": creator_id, "created_at": now}


def _public_sale(row) -> dict:
    sale = _dict(row)
    sale["gross_cents"] = sale["amount_cents"]
    sale["affiliate_cents"] = sale["commission_cents"]
    return sale


def get_sale(conn: sqlite3.Connection, provider, payment_id) -> dict | None:
    row = conn.execute("SELECT * FROM aff_sales WHERE provider=? AND payment_id=?", (provider, str(payment_id))).fetchone()
    return _public_sale(row) if row else None


def _payment_period(conn, creator_id, is_demo, kind, start, end):
    if kind == "membership":
        if start is None:
            current = conn.execute("""SELECT MAX(s.period_end) AS period_end FROM aff_sales s
              WHERE s.creator_id=? AND s.is_demo=? AND s.kind='membership'
              AND s.amount_cents>COALESCE((SELECT SUM(r.amount_cents) FROM aff_refunds r WHERE r.sale_id=s.id),0)
              AND NOT EXISTS(SELECT 1 FROM aff_payment_holds h WHERE h.sale_id=s.id AND h.active=1)""",
              (creator_id, int(is_demo))).fetchone()
            start_dt = max(_now(), _dt(current["period_end"])) if current and current["period_end"] else _now()
        else:
            start_dt = _dt(start, "period_start")
        end_dt = _dt(end, "period_end") if end is not None else _year_later(start_dt)
        days = (end_dt - start_dt).total_seconds() / 86400
        if not 330 <= days <= 370:
            raise AffiliateError("Creator membership payment must cover an annual billing period.", "invalid_period")
        return _stamp(start_dt), _stamp(end_dt)
    if (start is None) != (end is None):
        raise AffiliateError("Supply both subscription period timestamps or neither.")
    if start is None:
        return None, None
    start_dt, end_dt = _dt(start, "period_start"), _dt(end, "period_end")
    if end_dt <= start_dt:
        raise AffiliateError("period_end must follow period_start.", "invalid_period")
    return _stamp(start_dt), _stamp(end_dt)


def _sale_referral(conn, kind, user_id, creator_id, subscription_id, is_demo):
    """Resolve financial eligibility; a revoked pending referral does not fail a paid invoice."""
    settings = get_settings(conn)
    referral = get_account_referral(conn, user_id) if kind == "membership" else get_subscription_referral(conn, subscription_id)
    if referral is None:
        return None, None, 0
    if kind == "subscription" and (referral["fan_user_id"] != user_id or referral["creator_id"] != creator_id):
        raise AffiliateError("Paid subscription does not match its captured referral.", "identity_mismatch")
    affiliate = _affiliate_row(conn, referral["affiliate_id"])
    if bool(affiliate["is_demo"]) != is_demo:
        raise AffiliateError("Demo and live financial activity cannot be mixed.", "mode_mismatch")
    preference = get_creator_preference(conn, creator_id)
    self_ref = affiliate["linked_user_id"] in (user_id, creator_id, preference["owner_user_id"])
    if kind == "membership":
        eligible = referral["membership_eligible"]
        if eligible is None:
            eligible = affiliate["status"] == "active" and settings["program_enabled"] and not self_ref
            conn.execute("UPDATE aff_account_referrals SET membership_eligible=?,membership_locked_at=? WHERE id=?",
                         (int(eligible), _stamp(), referral["id"]))
        if not eligible or self_ref:
            return None, referral["id"], 0
    else:
        if referral["status"] == "pending":
            reason = ("self_referral" if self_ref else "creator_opted_out" if not preference["allowed"] else
                      "affiliate_inactive" if affiliate["status"] != "active" else
                      "program_paused" if not settings["program_enabled"] else None)
            status = "ineligible" if reason else "locked"
            conn.execute("UPDATE aff_subscription_referrals SET status=?,locked_at=?,ineligible_reason=? WHERE id=?",
                         (status, _stamp(), reason, referral["id"]))
            referral["status"] = status
        if referral["status"] != "locked" or self_ref:
            return None, referral["id"], 0
    return affiliate["id"], referral["id"], referral["rate_bps"]


def record_verified_sale(conn: sqlite3.Connection, *, provider, payment_id, kind, user_id,
                         amount_cents, currency, subscription_id=None, creator_id=None,
                         period_start=None, period_end=None, event_id=None,
                         actor_id="payment-provider", is_demo=False, platform_fee_bps=0) -> dict:
    """Record a SETTLED provider payment and its split exactly once.

    Return sale fields plus ``created``. ``(provider,payment_id)`` identifies the
    payment across different delivery event IDs. Duplicate matching deliveries
    do not extend membership or duplicate earnings. Platform fees are applied
    to fan subscriptions alongside, and separately from, the 3% affiliate fee.
    No bank transfer occurs here.
    """
    provider = _identifier(provider, "provider").lower()
    payment_id = _identifier(payment_id, "payment_id")
    user_id = _identifier(user_id, "user_id")
    if kind not in ("membership", "subscription"):
        raise AffiliateError("Only creator memberships and fan subscriptions earn commissions.", "invalid_kind")
    creator_id = _identifier(creator_id if creator_id is not None else user_id if kind == "membership" else None, "creator_id")
    subscription_id = _identifier(subscription_id, "subscription_id", optional=kind == "membership")
    event_id = _identifier(event_id, "event_id", optional=True)
    amount_cents = _integer(amount_cents, "amount_cents", minimum=1)
    currency = _currency(currency)
    is_demo = _boolean(is_demo, "is_demo")
    platform_fee_bps = _integer(platform_fee_bps, "platform_fee_bps", maximum=10000)
    with _atomic(conn):
        old = get_sale(conn, provider, payment_id)
        if old:
            expected = {"kind": kind, "user_id": user_id, "creator_id": creator_id,
                "subscription_id": subscription_id, "amount_cents": amount_cents,
                "currency": currency, "is_demo": is_demo, "platform_fee_bps": platform_fee_bps}
            if any(old[key] != value for key, value in expected.items()):
                raise AffiliateError("Payment ID was already recorded with different financial details.", "idempotency_conflict")
            for key, value in (("period_start", period_start), ("period_end", period_end)):
                if value is not None and old[key] != _stamp(_dt(value, key)):
                    raise AffiliateError("Payment period conflicts with the recorded payment.", "idempotency_conflict")
            return {**old, "created": False}
        settings = get_settings(conn)
        if currency != settings["currency"]:
            raise AffiliateError("Payment currency does not match the affiliate program.", "currency_mismatch")
        if kind == "membership" and amount_cents != settings["annual_price_cents"]:
            raise AffiliateError("Creator membership subtotal does not match the configured annual price.", "amount_mismatch")
        if kind == "membership" and platform_fee_bps:
            raise AffiliateError("Fan platform fees do not apply to creator membership sales.")
        if kind == "subscription" and user_id in (creator_id, get_creator_preference(conn, creator_id)["owner_user_id"]):
            raise AffiliateError("Creators cannot purchase their own subscription.", "self_referral")
        start, end = _payment_period(conn, creator_id, is_demo, kind, period_start, period_end)
        affiliate_id, referral_id, rate = _sale_referral(conn, kind, user_id, creator_id, subscription_id, is_demo)
        commission = _round_bps(amount_cents, rate)
        platform_fee = _round_bps(amount_cents, platform_fee_bps)
        if rate + platform_fee_bps > 10000 or commission + platform_fee > amount_cents:
            raise AffiliateError("Combined platform and affiliate fees exceed the payment.", "fees_exceed_payment")
        deduction = commission if kind == "subscription" else 0
        creator_net = amount_cents - platform_fee - commission if kind == "subscription" else 0
        platform_net = platform_fee if kind == "subscription" else amount_cents - commission
        now = _stamp()
        cursor = conn.execute("""INSERT INTO aff_sales
          (provider,payment_id,event_id,kind,user_id,creator_id,subscription_id,amount_cents,currency,
           affiliate_id,referral_id,commission_rate_bps,commission_cents,creator_deduction_cents,
           platform_fee_bps,platform_fee_cents,creator_net_cents,platform_net_cents,period_start,period_end,is_demo,created_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (provider,payment_id,event_id,kind,user_id,
          creator_id,subscription_id,amount_cents,currency,affiliate_id,referral_id,rate,commission,deduction,
          platform_fee_bps,platform_fee,creator_net,platform_net,start,end,int(is_demo),now))
        sale_id = cursor.lastrowid
        if affiliate_id is not None:
            conn.execute("""INSERT INTO aff_commission_ledger
              (affiliate_id,sale_id,entry_type,amount_cents,creator_deduction_cents,currency,created_at)
              VALUES (?,?,'commission',?,?,?,?)""", (affiliate_id,sale_id,commission,deduction,currency,now))
        if kind == "membership":
            existing = conn.execute("SELECT * FROM aff_memberships WHERE creator_id=? AND is_demo=?",
                                    (creator_id,int(is_demo))).fetchone()
            if existing is None:
                conn.execute("""INSERT INTO aff_memberships
                  (creator_id,is_demo,provider,subscription_id,period_start,period_end,source_sale_id,updated_at)
                  VALUES (?,?,?,?,?,?,?,?)""", (creator_id,int(is_demo),provider,subscription_id,start,end,sale_id,now))
            elif end > existing["period_end"]:
                coverage_start = min(start,existing["period_start"]) if start <= existing["period_end"] else start
                conn.execute("""UPDATE aff_memberships SET provider=?,subscription_id=?,period_start=?,period_end=?,
                  cancel_at_period_end=0,source_sale_id=?,updated_at=? WHERE creator_id=? AND is_demo=?""",
                  (provider,subscription_id,coverage_start,end,sale_id,now,creator_id,int(is_demo)))
        _audit(conn, actor_id, "payment.recorded", "sale", sale_id,
               {"provider": provider, "payment_id": payment_id, "kind": kind, "commission_cents": commission})
    return {**get_sale(conn, provider, payment_id), "created": True}


def creator_entitlement(conn: sqlite3.Connection, creator_id, *, at=None, is_demo=False) -> dict:
    """Derive paid creator access at a UTC time; cancellation retains paid access.

    Full refunds remove that payment's access period; partial refunds do not.
    A provider's cancellation webhook must call cancel_membership separately.
    """
    creator_id = _identifier(creator_id, "creator_id")
    is_demo = _boolean(is_demo, "is_demo")
    moment = _stamp(_dt(at) if at is not None else _now())
    membership = _dict(conn.execute("SELECT * FROM aff_memberships WHERE creator_id=? AND is_demo=?",
                                    (creator_id,int(is_demo))).fetchone())
    base = {"creator_id": creator_id, "is_demo": is_demo, "active": False, "can_publish": False,
            "status": "unpaid", "period_start": None, "period_end": None, "cancel_at_period_end": False}
    if membership is None:
        return base
    periods = conn.execute("""SELECT s.*,
      COALESCE((SELECT SUM(r.amount_cents) FROM aff_refunds r WHERE r.sale_id=s.id),0) AS refunded_cents,
      COALESCE((SELECT h.active FROM aff_payment_holds h WHERE h.sale_id=s.id),0) AS held
      FROM aff_sales s WHERE s.creator_id=? AND s.kind='membership' AND s.is_demo=?
      ORDER BY s.period_end DESC,s.id DESC""", (creator_id,int(is_demo))).fetchall()
    usable = [p for p in periods if p["refunded_cents"] < p["amount_cents"]]
    active_periods = [p for p in usable if p["period_start"] <= moment < p["period_end"]]
    chosen = active_periods[0] if active_periods else usable[0] if usable else periods[0]
    active = bool(active_periods) and not chosen["held"]
    status = "payment_review" if active_periods and chosen["held"] else (
        ("canceling" if membership["cancel_at_period_end"] else "active") if active else (
        "refunded" if not usable else "scheduled" if chosen["period_start"] > moment else "expired")
    )
    return {**base, **membership, "active": active, "can_publish": active, "status": status,
            "period_start": chosen["period_start"], "period_end": chosen["period_end"]}


def cancel_membership(conn: sqlite3.Connection, creator_id, *, actor_id, is_demo=False) -> dict:
    """Record confirmed cancellation at period end; does not call the provider."""
    creator_id = _identifier(creator_id, "creator_id")
    is_demo = _boolean(is_demo, "is_demo")
    with _atomic(conn):
        row = conn.execute("SELECT 1 FROM aff_memberships WHERE creator_id=? AND is_demo=?", (creator_id,int(is_demo))).fetchone()
        if row is None:
            raise AffiliateError("Creator has no paid membership.", "not_found")
        conn.execute("UPDATE aff_memberships SET cancel_at_period_end=1,updated_at=? WHERE creator_id=? AND is_demo=?",
                     (_stamp(),creator_id,int(is_demo)))
        _audit(conn, actor_id, "membership.cancellation_recorded", "creator", creator_id, {"is_demo": is_demo})
    return creator_entitlement(conn, creator_id, is_demo=is_demo)


def refund_payment(conn: sqlite3.Connection, *, provider, payment_id, refund_id, amount_cents, actor_id) -> dict:
    """Append an exact verified refund and a proportional, capped commission reversal."""
    provider = _identifier(provider, "provider").lower()
    payment_id = _identifier(payment_id, "payment_id")
    refund_id = _identifier(refund_id, "refund_id")
    amount_cents = _integer(amount_cents, "amount_cents", minimum=1)
    with _atomic(conn):
        sale = get_sale(conn, provider, payment_id)
        if sale is None:
            raise AffiliateError("Original settled payment was not found.", "not_found")
        old = _dict(conn.execute("SELECT * FROM aff_refunds WHERE provider=? AND refund_id=?", (provider,refund_id)).fetchone())
        if old:
            if old["sale_id"] != sale["id"] or old["amount_cents"] != amount_cents:
                raise AffiliateError("Refund ID was already recorded with different details.", "idempotency_conflict")
            return {**old, "created": False}
        totals = conn.execute("""SELECT COALESCE(SUM(amount_cents),0) AS amount,
          COALESCE(SUM(commission_reversed_cents),0) AS commission,
          COALESCE(SUM(platform_fee_reversed_cents),0) AS platform_fee FROM aff_refunds WHERE sale_id=?""", (sale["id"],)).fetchone()
        cumulative = totals["amount"] + amount_cents
        if cumulative > sale["amount_cents"]:
            raise AffiliateError("Refund exceeds the remaining payment amount.", "refund_exceeds_payment")
        def proportional(total):
            return min(total, (total * cumulative + sale["amount_cents"] // 2) // sale["amount_cents"])
        reversal = proportional(sale["commission_cents"]) - totals["commission"]
        platform_reversal = proportional(sale["platform_fee_cents"]) - totals["platform_fee"]
        deduction_reversal = reversal if sale["kind"] == "subscription" else 0
        now = _stamp()
        cursor = conn.execute("""INSERT INTO aff_refunds
          (sale_id,provider,refund_id,amount_cents,commission_reversed_cents,
           creator_deduction_reversed_cents,platform_fee_reversed_cents,created_at) VALUES (?,?,?,?,?,?,?,?)""",
          (sale["id"],provider,refund_id,amount_cents,reversal,deduction_reversal,platform_reversal,now))
        refund_row_id = cursor.lastrowid
        if sale["affiliate_id"] is not None and reversal:
            conn.execute("""INSERT INTO aff_commission_ledger
              (affiliate_id,sale_id,refund_id,entry_type,amount_cents,creator_deduction_cents,currency,created_at)
              VALUES (?,?,?,'reversal',?,?,?,?)""", (sale["affiliate_id"],sale["id"],refund_row_id,-reversal,
              -deduction_reversal,sale["currency"],now))
        _audit(conn, actor_id, "payment.refunded", "sale", sale["id"],
               {"refund_id": refund_id, "amount_cents": amount_cents, "commission_reversed_cents": reversal})
    result = _dict(conn.execute("SELECT * FROM aff_refunds WHERE id=?", (refund_row_id,)).fetchone())
    return {**result, "created": True}


def hold_payment(conn: sqlite3.Connection, provider, payment_id, *, reason, actor_id) -> dict:
    """Hold commission on an unresolved dispute/refund; never invent refund amounts."""
    reason = _identifier(reason, "reason")
    with _atomic(conn):
        sale = get_sale(conn, _identifier(provider,"provider").lower(), _identifier(payment_id,"payment_id"))
        if sale is None:
            raise AffiliateError("Original settled payment was not found.", "not_found")
        now = _stamp()
        conn.execute("""INSERT INTO aff_payment_holds(sale_id,active,reason,created_at,updated_at)
          VALUES (?,1,?,?,?) ON CONFLICT(sale_id) DO UPDATE SET active=1,reason=excluded.reason,
          updated_at=excluded.updated_at""", (sale["id"],reason,now,now))
        if sale["affiliate_id"] is not None:
            conn.execute("UPDATE aff_payouts SET status='held',failure_reason=?,updated_at=? WHERE affiliate_id=? AND status='pending'",
                         ("Payment reconciliation required",now,sale["affiliate_id"]))
        _audit(conn, actor_id, "payment.held", "sale", sale["id"], {"reason": reason})
    return _dict(conn.execute("SELECT * FROM aff_payment_holds WHERE sale_id=?", (sale["id"],)).fetchone())


def release_payment_hold(conn: sqlite3.Connection, provider, payment_id, *, reason, actor_id) -> dict:
    """Record authorized reconciliation; pending held payouts need a fresh request."""
    reason = _identifier(reason, "reason")
    with _atomic(conn):
        sale = get_sale(conn, _identifier(provider,"provider").lower(), _identifier(payment_id,"payment_id"))
        if sale is None:
            raise AffiliateError("Original settled payment was not found.", "not_found")
        if not conn.execute("SELECT 1 FROM aff_payment_holds WHERE sale_id=?", (sale["id"],)).fetchone():
            raise AffiliateError("Payment has no reconciliation hold.", "not_found")
        conn.execute("UPDATE aff_payment_holds SET active=0,reason=?,updated_at=? WHERE sale_id=?", (reason,_stamp(),sale["id"]))
        _audit(conn, actor_id, "payment.hold_released", "sale", sale["id"], {"reason": reason})
    return _dict(conn.execute("SELECT * FROM aff_payment_holds WHERE sale_id=?", (sale["id"],)).fetchone())


def get_balances(conn: sqlite3.Connection, affiliate_id, *, currency=None) -> dict:
    """Return earned/reversed/paid/reserved/held/available cents; deficits remain visible."""
    affiliate = _affiliate_row(conn, affiliate_id)
    currency = _currency(currency or get_settings(conn)["currency"])
    ledger = conn.execute("""SELECT
      COALESCE(SUM(CASE WHEN entry_type='commission' THEN amount_cents ELSE 0 END),0) AS earned,
      COALESCE(SUM(CASE WHEN entry_type='reversal' THEN -amount_cents ELSE 0 END),0) AS reversed,
      COALESCE(SUM(amount_cents),0) AS net FROM aff_commission_ledger WHERE affiliate_id=? AND currency=?""",
      (affiliate_id,currency)).fetchone()
    payouts = conn.execute("""SELECT
      COALESCE(SUM(CASE WHEN status='paid' THEN amount_cents ELSE 0 END),0) AS paid,
      COALESCE(SUM(CASE WHEN status IN ('pending','held') THEN amount_cents ELSE 0 END),0) AS pending,
      COALESCE(SUM(CASE WHEN status='held' THEN amount_cents ELSE 0 END),0) AS held_payout
      FROM aff_payouts WHERE affiliate_id=? AND currency=?""", (affiliate_id,currency)).fetchone()
    hold = conn.execute("""SELECT COALESCE(SUM(MAX(remaining,0)),0) AS held FROM
      (SELECT SUM(l.amount_cents) AS remaining FROM aff_commission_ledger l
       JOIN aff_payment_holds h ON h.sale_id=l.sale_id AND h.active=1
       WHERE l.affiliate_id=? AND l.currency=? GROUP BY l.sale_id)""", (affiliate_id,currency)).fetchone()["held"]
    unpaid = ledger["net"] - payouts["paid"]
    held = min(max(unpaid,0),hold) if affiliate["status"] == "active" else max(unpaid,0)
    available = max(0,unpaid-held-payouts["pending"])
    return {"affiliate_id": affiliate["id"], "currency": currency, "earned_cents": ledger["earned"],
      "reversed_cents": ledger["reversed"], "net_commission_cents": ledger["net"],
      "paid_cents": payouts["paid"], "pending_payout_cents": payouts["pending"],
      "held_payout_cents": payouts["held_payout"], "held_cents": held,
      "available_cents": available, "balance_cents": unpaid, "negative_cents": max(0,-unpaid),
      "status": affiliate["status"], "is_demo": bool(affiliate["is_demo"])}


def request_payout(conn: sqlite3.Connection, affiliate_id, amount_cents, *, currency, actor_id) -> dict:
    """Reserve available earnings for an external payout adapter; no funds are sent."""
    amount_cents = _integer(amount_cents,"amount_cents",minimum=1)
    currency = _currency(currency)
    with _atomic(conn):
        affiliate = _affiliate_row(conn,affiliate_id)
        if affiliate["is_demo"]:
            raise AffiliateError("Demo accounts cannot request real payouts.", "demo_payout_disabled")
        if affiliate["status"] != "active":
            raise AffiliateError("Payouts are suspended for this affiliate.", "affiliate_inactive")
        if currency != get_settings(conn)["currency"]:
            raise AffiliateError("Payout currency does not match the program.", "currency_mismatch")
        balance = get_balances(conn,affiliate_id,currency=currency)
        if amount_cents > balance["available_cents"]:
            raise AffiliateError("Payout exceeds available, unreserved earnings.", "insufficient_balance")
        now = _stamp()
        cursor = conn.execute("""INSERT INTO aff_payouts
          (affiliate_id,amount_cents,currency,status,created_at,updated_at) VALUES (?,?,?,'pending',?,?)""",
          (affiliate_id,amount_cents,currency,now,now))
        payout_id = cursor.lastrowid
        _audit(conn,actor_id,"payout.requested","payout",payout_id,{"amount_cents": amount_cents,"currency": currency})
    return get_payout(conn,payout_id)


def get_payout(conn: sqlite3.Connection,payout_id) -> dict:
    row = _dict(conn.execute("SELECT * FROM aff_payouts WHERE id=?",(payout_id,)).fetchone())
    if row is None:
        raise AffiliateError("Payout was not found.","not_found")
    return row


def confirm_payout(conn: sqlite3.Connection,payout_id,*,provider_reference,actor_id) -> dict:
    """Record a provider-confirmed transfer, never a requested or assumed one.

    The privileged adapter must verify provider success before calling. An
    immutable, globally unique transfer reference is required. Use payout ID as
    the external provider idempotency key when sending the actual transfer.
    """
    reference = _identifier(provider_reference,"provider_reference")
    with _atomic(conn):
        payout = get_payout(conn,payout_id)
        if payout["status"] == "paid":
            if payout["provider_reference"] != reference:
                raise AffiliateError("Payout already has a different transfer reference.","idempotency_conflict")
            return {**payout,"created": False}
        if payout["status"] != "pending":
            raise AffiliateError("Only a pending, unheld payout can be confirmed.","payout_not_pending")
        affiliate = _affiliate_row(conn,payout["affiliate_id"])
        if affiliate["status"] != "active" or affiliate["is_demo"]:
            raise AffiliateError("This affiliate cannot receive a payout.","affiliate_inactive")
        balance = get_balances(conn,payout["affiliate_id"],currency=payout["currency"])
        # Include this payout's own reservation, while respecting newer refunds
        # or disputes that can have reduced its funds since it was requested.
        capacity = balance["balance_cents"] - balance["held_cents"] - (balance["pending_payout_cents"]-payout["amount_cents"])
        if capacity < payout["amount_cents"]:
            raise AffiliateError("Reserved earnings changed; reconcile before confirming payout.","insufficient_balance")
        try:
            conn.execute("UPDATE aff_payouts SET status='paid',provider_reference=?,updated_at=? WHERE id=?",
                         (reference,_stamp(),payout_id))
        except sqlite3.IntegrityError as exc:
            raise AffiliateError("Transfer reference is already assigned to another payout.","idempotency_conflict") from exc
        _audit(conn,actor_id,"payout.confirmed","payout",payout_id,{"provider_reference": reference})
    return {**get_payout(conn,payout_id),"created": True}


def fail_payout(conn: sqlite3.Connection,payout_id,*,reason,actor_id) -> dict:
    """Release a pending/held reservation after verified failure or cancellation."""
    reason = _identifier(reason,"reason")
    with _atomic(conn):
        payout = get_payout(conn,payout_id)
        if payout["status"] == "paid":
            raise AffiliateError("A completed payout cannot be changed to failed.","payout_already_paid")
        conn.execute("UPDATE aff_payouts SET status='failed',failure_reason=?,updated_at=? WHERE id=?",(reason,_stamp(),payout_id))
        _audit(conn,actor_id,"payout.failed","payout",payout_id,{"reason": reason})
    return get_payout(conn,payout_id)


def _list_filter(alias="a",*,affiliate_id=None,is_demo=None):
    filters,params = [],[]
    if affiliate_id is not None:
        filters.append(f"{alias}.id=?")
        params.append(affiliate_id)
    if is_demo is not None:
        filters.append(f"{alias}.is_demo=?")
        params.append(int(_boolean(is_demo,"is_demo")))
    return (" WHERE "+" AND ".join(filters) if filters else ""),params


def _limit(value):
    return _integer(value,"limit",minimum=1,maximum=1000)


def list_affiliates(conn: sqlite3.Connection,*,is_demo=None,limit=100) -> list[dict]:
    where,params = _list_filter(is_demo=is_demo)
    return [_public_affiliate(r) for r in conn.execute(f"SELECT a.* FROM aff_affiliates a{where} ORDER BY a.id LIMIT ?",[*params,_limit(limit)])]


def list_creator_preferences(conn: sqlite3.Connection,*,allowed=None,limit=100) -> list[dict]:
    params=[]
    where=""
    if allowed is not None:
        where=" WHERE allowed=?"
        params.append(int(_boolean(allowed,"allowed")))
    return [_dict(r) for r in conn.execute(f"SELECT * FROM aff_creator_preferences{where} ORDER BY creator_id LIMIT ?",[*params,_limit(limit)])]


def list_sales(conn: sqlite3.Connection,*,affiliate_id=None,is_demo=None,limit=100) -> list[dict]:
    filters,params=[],[]
    if affiliate_id is not None:
        filters.append("s.affiliate_id=?");params.append(affiliate_id)
    if is_demo is not None:
        filters.append("s.is_demo=?");params.append(int(_boolean(is_demo,"is_demo")))
    where=" WHERE "+" AND ".join(filters) if filters else ""
    return [_public_sale(r) for r in conn.execute(f"""SELECT s.*,
      COALESCE((SELECT SUM(r.amount_cents) FROM aff_refunds r WHERE r.sale_id=s.id),0) AS refunded_cents,
      COALESCE((SELECT h.active FROM aff_payment_holds h WHERE h.sale_id=s.id),0) AS held
      FROM aff_sales s{where} ORDER BY s.id DESC LIMIT ?""",[*params,_limit(limit)])]


def list_commissions(conn: sqlite3.Connection,*,affiliate_id=None,is_demo=None,limit=100) -> list[dict]:
    where,params=_list_filter(affiliate_id=affiliate_id,is_demo=is_demo)
    rows = [_dict(r) for r in conn.execute(f"""SELECT l.*,a.username,s.kind,s.payment_id,s.creator_id,
      CASE WHEN l.entry_type='reversal' THEN 'reversed'
        WHEN a.status!='active' OR EXISTS(SELECT 1 FROM aff_payment_holds h WHERE h.sale_id=s.id AND h.active=1)
        THEN 'held' ELSE 'earned' END AS state,
      a.display_name,a.is_demo FROM aff_commission_ledger l JOIN aff_affiliates a ON a.id=l.affiliate_id
      JOIN aff_sales s ON s.id=l.sale_id{where} ORDER BY l.id DESC LIMIT ?""",[*params,_limit(limit)])]
    return [{**r,"status":r["state"]} for r in rows]


def list_payouts(conn: sqlite3.Connection,*,affiliate_id=None,is_demo=None,status=None,limit=100) -> list[dict]:
    where,params=_list_filter(affiliate_id=affiliate_id,is_demo=is_demo)
    if status is not None:
        if status not in ("pending","held","paid","failed"):
            raise AffiliateError("Invalid payout status.")
        where+=(" AND " if where else " WHERE ")+"p.status=?";params.append(status)
    return [_dict(r) for r in conn.execute(f"""SELECT p.*,a.username,a.display_name,a.is_demo
      FROM aff_payouts p JOIN aff_affiliates a ON a.id=p.affiliate_id{where}
      ORDER BY p.id DESC LIMIT ?""",[*params,_limit(limit)])]


def list_referrals(conn: sqlite3.Connection,*,affiliate_id=None,is_demo=None,limit=100) -> list[dict]:
    """Combined account and subscription referrals with a ``kind`` discriminator."""
    where,params=_list_filter(affiliate_id=affiliate_id,is_demo=is_demo)
    account=[{**_dict(r),"kind":"account"} for r in conn.execute(f"""SELECT r.*,a.username,a.is_demo
      FROM aff_account_referrals r JOIN aff_affiliates a ON a.id=r.affiliate_id{where}
      ORDER BY r.id DESC LIMIT ?""",[*params,_limit(limit)])]
    subs=[{**_dict(r),"kind":"subscription"} for r in conn.execute(f"""SELECT r.*,a.username,a.is_demo
      FROM aff_subscription_referrals r JOIN aff_affiliates a ON a.id=r.affiliate_id{where}
      ORDER BY r.id DESC LIMIT ?""",[*params,_limit(limit)])]
    return sorted(account+subs,key=lambda r:(r["created_at"],r["id"]),reverse=True)[:limit]


def list_audit(conn: sqlite3.Connection,*,limit=100) -> list[dict]:
    result=[]
    for row in conn.execute("SELECT * FROM aff_audit_log ORDER BY id DESC LIMIT ?",(_limit(limit),)):
        item=_dict(row);item["details"]=json.loads(item.pop("details_json"));result.append(item)
    return result


def get_dashboard(conn: sqlite3.Connection,*,affiliate_id=None,is_demo=None) -> dict:
    """Return aggregate counts, money totals, balances, and ten recent records.

    For one affiliate, all financial and referral data are scoped to that ID.
    ``commission_cents`` is net of reversals; gross_cents is before refunds.
    """
    where,params=_list_filter(affiliate_id=affiliate_id,is_demo=is_demo)
    ids=[r["id"] for r in conn.execute(f"SELECT a.id FROM aff_affiliates a{where}",params)]
    balances=[get_balances(conn,ident) for ident in ids]
    def count(table):
        return conn.execute(f"SELECT COUNT(*) FROM {table} r JOIN aff_affiliates a ON a.id=r.affiliate_id{where}",params).fetchone()[0]
    sale_filters,sale_params=[],[]
    if affiliate_id is not None:
        sale_filters.append("s.affiliate_id=?");sale_params.append(affiliate_id)
    if is_demo is not None:
        sale_filters.append("s.is_demo=?");sale_params.append(int(is_demo))
    sale_where=" WHERE "+" AND ".join(sale_filters) if sale_filters else ""
    sales=conn.execute(f"SELECT COUNT(*) AS count,COALESCE(SUM(s.amount_cents),0) AS gross FROM aff_sales s{sale_where}",sale_params).fetchone()
    refunds=conn.execute(f"SELECT COALESCE(SUM(r.amount_cents),0) FROM aff_refunds r JOIN aff_sales s ON s.id=r.sale_id{sale_where}",sale_params).fetchone()[0]
    result={"currency":get_settings(conn)["currency"],"affiliates":len(ids),"clicks":count("aff_clicks"),
      "signups":count("aff_account_referrals"),"subscription_referrals":count("aff_subscription_referrals"),
      "creators_opted_in":conn.execute("SELECT COUNT(*) FROM aff_creator_preferences WHERE allowed=1").fetchone()[0],
      "sales":sales["count"],"gross_cents":sales["gross"],"refunded_cents":refunds,
      "commission_cents":sum(b["net_commission_cents"] for b in balances),
      "gross_commission_cents":sum(b["earned_cents"] for b in balances),
      "paid_cents":sum(b["paid_cents"] for b in balances),
      "available_cents":sum(b["available_cents"] for b in balances),
      "held_cents":sum(b["held_cents"] for b in balances),
      "pending_payout_cents":sum(b["pending_payout_cents"] for b in balances),
      "negative_cents":sum(b["negative_cents"] for b in balances),
      "recent_sales":list_sales(conn,affiliate_id=affiliate_id,is_demo=is_demo,limit=10),
      "recent_commissions":list_commissions(conn,affiliate_id=affiliate_id,is_demo=is_demo,limit=10),
      "recent_referrals":list_referrals(conn,affiliate_id=affiliate_id,is_demo=is_demo,limit=10)}
    if affiliate_id is not None:
        result["balance"]=get_balances(conn,affiliate_id)
    return result
