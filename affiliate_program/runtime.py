"""Host application adapter. All monetary records come from verified invoices."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from flask import abort, g, has_request_context, request, session, redirect
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import HTTPException

from . import core


class MembershipRequired(HTTPException):
    code = 402
    description = "An active $54 annual creator membership is required. Open creator membership to pay or renew."


@contextmanager
def transaction(db):
    """A savepoint supports host transactions without prematurely committing them."""
    nested = db.in_transaction
    marker = "aff_" + secrets.token_hex(6)
    db.execute("SAVEPOINT " + marker if nested else "BEGIN IMMEDIATE")
    try:
        yield db
        db.execute("RELEASE SAVEPOINT " + marker) if nested else db.commit()
    except BaseException:
        if nested:
            db.execute("ROLLBACK TO SAVEPOINT " + marker)
            db.execute("RELEASE SAVEPOINT " + marker)
        else:
            db.rollback()
        raise


def money(cents, currency="USD"):
    signs = {"USD": "$", "CAD": "CA$", "EUR": "€", "GBP": "£"}
    amount = int(cents or 0)
    return ("−" if amount < 0 else "") + signs.get(currency, "") + f"{abs(amount)/100:,.2f} {currency}"


def parse_cents(raw):
    try:
        value = Decimal(str(raw))
        if not value.is_finite() or value <= 0 or value > 100000:
            raise ValueError("Amount must be between 0.01 and 100,000.")
        cents = value * 100
        if cents != cents.to_integral_value():
            raise ValueError("Use at most two decimal places.")
        return int(cents)
    except InvalidOperation as exc:
        raise ValueError("Enter a valid amount.") from exc


class AffiliateRuntime:
    def __init__(self, app, platform=None, *, billing_module=None, demo=False, db_path=None):
        self.app, self.platform, self.billing = app, platform, billing_module
        self.demo = bool(demo)
        if self.demo and platform is not None:
            raise RuntimeError("Demo mode must use a separate application and database.")
        self.path = str(db_path or app.config.get("AFFILIATE_DB_PATH", ""))
        if platform is None and not self.path:
            raise RuntimeError("Provide an explicit persistent affiliate database path.")
        self.require_membership = os.getenv("AFFILIATE_REQUIRE_MEMBERSHIP", "true").lower() == "true"
        self.referral_cookie = "sp_aff_demo_ref" if self.demo else "sp_aff_ref"
        self.signer = URLSafeTimedSerializer(app.secret_key, salt="sp-affiliate-demo-referrals-v1" if self.demo else "sp-affiliate-referrals-v1")
        configured = os.getenv("AFFILIATE_ADMIN_PASSWORD", "")
        if not configured and platform is not None:
            configured = getattr(platform, "ADMIN_PASSWORD", "")
        if self.demo:
            configured = "admin"
        # The requested short password exists only in the isolated demo.
        self.owner_hash = generate_password_hash(configured) if configured and (self.demo or len(configured) >= 12) else None
        self.owner_stamp = hmac.new(str(app.secret_key).encode(),
                                    ("affiliate-owner-session:"+configured).encode(), hashlib.sha256).hexdigest()
        app.teardown_appcontext(self.close_db)
        with app.app_context():
            db = self.db()
            core.init_schema(db)
            db.executescript("""
              CREATE TABLE IF NOT EXISTS aff_login_limits (
                subject TEXT PRIMARY KEY, started INTEGER NOT NULL, failures INTEGER NOT NULL
              );
              CREATE TABLE IF NOT EXISTS aff_invoice_terms (
                invoice_id TEXT PRIMARY KEY, kind TEXT NOT NULL, user_id TEXT NOT NULL,
                creator_id TEXT NOT NULL, subscription_id TEXT, amount_cents INTEGER NOT NULL,
                currency TEXT NOT NULL, platform_fee_bps INTEGER NOT NULL DEFAULT 0,
                sale_provider TEXT, sale_payment_id TEXT
              );
            """)
            db.commit()
            if self.demo:
                self.seed_demo(db)

    def db(self):
        if self.platform is not None:
            return self.platform.get_db()
        if "aff_db" not in g:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            g.aff_db = db
        return g.aff_db

    @staticmethod
    def close_db(_error=None):
        db = g.pop("aff_db", None)
        if db is not None:
            db.close()

    def seed_demo(self, db):
        db.executescript("""
          CREATE TABLE IF NOT EXISTS aff_demo_creators (
            id TEXT PRIMARY KEY, owner_user_id TEXT UNIQUE NOT NULL, slug TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL, avatar_text TEXT NOT NULL,
            subscription_price REAL NOT NULL, subscription_currency TEXT NOT NULL,
            platform_fee_percent REAL NOT NULL DEFAULT 9, status TEXT NOT NULL DEFAULT 'active'
          );
        """)
        db.commit()
        with transaction(db):
            core.seed_demo_affiliates(db, password_hash=generate_password_hash("admin"))
            for number, name, initials, price, opted_in in (
                (1, "Creator One", "C1", 20, True),
                (2, "Creator Two", "C2", 30, True),
                (3, "Creator Three", "C3", 25, False),
            ):
                cid, uid = f"demo_creator_{number}", f"demo_creator_user_{number}"
                added = db.execute("""INSERT OR IGNORE INTO aff_demo_creators
                    (id,owner_user_id,slug,display_name,avatar_text,subscription_price,subscription_currency)
                    VALUES(?,?,?,?,?,?,?)""", (cid, uid, f"creator-{number}", name, initials, price, "USD"))
                if added.rowcount:
                    core.set_creator_opt_in(db, cid, opted_in, owner_user_id=uid, actor_id="demo-setup")

    def creator_rows(self):
        table = "aff_demo_creators" if self.demo else "creators"
        return [dict(r) for r in self.db().execute(f"""SELECT id,owner_user_id,slug,display_name,
            avatar_text,subscription_price,subscription_currency,platform_fee_percent,status
            FROM {table} WHERE status IN ('active','membership_pending') ORDER BY display_name""").fetchall()]

    def creator(self, creator_id=None, slug=None):
        return next((c for c in self.creator_rows() if
                     (creator_id and c["id"] == creator_id) or (slug and c["slug"] == slug)), None)

    def is_owner(self):
        if self.platform is not None and self.platform.is_admin():
            return True
        return bool(self.owner_hash and session.get("aff_owner_stamp") == self.owner_stamp)

    def owner_login(self, username, password):
        return bool(username == "root" and self.owner_hash and check_password_hash(self.owner_hash, password))

    def affiliate(self):
        aid = session.get("aff_id")
        if not aid:
            return None
        try:
            account = core.get_affiliate(self.db(), aid)
        except ValueError:
            return None
        if not account or account.get("status") != "active":
            return None
        stamp = self.affiliate_stamp(aid)
        if not secrets.compare_digest(str(session.get("aff_stamp", "")), stamp):
            return None
        if bool(account.get("is_demo")) != self.demo:
            return None
        return account

    def affiliate_stamp(self, affiliate_id):
        row = self.db().execute("SELECT password_hash FROM aff_affiliates WHERE id=?", (affiliate_id,)).fetchone()
        return hashlib.sha256(row["password_hash"].encode()).hexdigest() if row else ""

    def current_creator(self):
        if self.demo and self.is_owner():
            return self.creator(request.values.get("creator_id") or "demo_creator_1")
        user = self.platform.current_user() if self.platform is not None else None
        if user and user["role"] == "creator":
            creator = self.platform.creator_for_user(user["id"])
            return dict(creator) if creator else None
        return None

    def public_base(self):
        configured = self.app.config.get("AFFILIATE_PUBLIC_BASE_URL") or os.getenv("PUBLIC_BASE_URL", "https://smellpanties.com")
        parts = urlsplit(configured)
        allowed = (parts.scheme == "https" or (self.demo and parts.scheme == "http"))
        if not allowed or not parts.netloc or parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
            raise RuntimeError("PUBLIC_BASE_URL must be an HTTPS origin, without a path.")
        return configured.rstrip("/")

    def referral(self, kind="platform", creator_id=None):
        if not has_request_context():
            return None
        token = request.cookies.get(self.referral_cookie, "")
        if not token:
            return None
        try:
            days = int(core.get_settings(self.db())["attribution_window_days"])
            payload = self.signer.loads(token, max_age=days * 86400)
            if not isinstance(payload, dict):
                return None
            entry = payload.get("platform") if kind == "platform" else payload.get("creators", {}).get(str(creator_id))
            if not isinstance(entry, dict) or not isinstance(entry.get("at"), int):
                return None
            if not 0 <= int(time.time()) - entry["at"] <= days * 86400:
                return None
            return entry.get("code")
        except (BadSignature, SignatureExpired, TypeError, ValueError):
            return None

    def set_referral(self, response, code, creator_id=None):
        days = int(core.get_settings(self.db())["attribution_window_days"])
        payload = {}
        token = request.cookies.get(self.referral_cookie, "")
        try:
            payload = self.signer.loads(token, max_age=days * 86400) if token else {}
        except (BadSignature, SignatureExpired):
            pass
        if not isinstance(payload, dict):
            payload = {}
        now = int(time.time())
        first = payload.get("platform", {})
        if not isinstance(first, dict) or not isinstance(first.get("at"), int) or not 0 <= now-first["at"] <= days*86400:
            payload["platform"] = {"code": code, "at": now}
        creators = payload.setdefault("creators", {})
        creators = {key: value for key, value in creators.items()
                    if isinstance(value, dict) and isinstance(value.get("at"), int)
                    and 0 <= now-value["at"] <= days*86400}
        payload["creators"] = creators
        if creator_id and (str(creator_id) in creators or len(creators) < 20):
            creators.setdefault(str(creator_id), {"code": code, "at": now})
        response.set_cookie(self.referral_cookie, self.signer.dumps(payload), max_age=days*86400,
                            httponly=True, secure=not self.demo or request.is_secure,
                            samesite="Lax", path="/")
        return response

    def capture_account(self, user_id, *, role, db=None, creator_id=None):
        def capture(conn):
            if creator_id:
                core.set_creator_opt_in(conn, creator_id, False, owner_user_id=user_id, actor_id=user_id)
            code = self.referral()
            if code:
                # An expired/suspended code cannot break otherwise valid signup.
                try:
                    core.attribute_account(conn, user_id, code, actor_id=user_id)
                except ValueError:
                    pass
        if db is not None:
            capture(db)
        else:
            with transaction(self.db()) as conn:
                capture(conn)

    def capture_subscription(self, subscription_id, fan_user_id, creator):
        code = self.referral("creator", creator["id"])
        if code:
            with transaction(self.db()) as db:
                try:
                    core.attribute_subscription(db, subscription_id, fan_user_id, creator["id"], code, actor_id=fan_user_id)
                except ValueError:
                    pass

    def snapshot_invoice(self, db, invoice):
        invoice = dict(invoice)
        kind = invoice["kind"]
        if kind not in {"subscription", "creator_membership"}:
            return
        self.validate_checkout_invoice(invoice)
        subscription_id, fee_bps = None, 0
        if kind == "subscription":
            sub = db.execute("SELECT * FROM subscriptions WHERE id=?", (invoice["target_id"],)).fetchone()
            if not sub or sub["fan_user_id"] != invoice["buyer_id"]:
                raise ValueError("Subscription ownership mismatch.")
            creator = db.execute("SELECT * FROM creators WHERE id=?", (sub["creator_id"],)).fetchone()
            if not creator:
                raise ValueError("Unknown subscription creator.")
            creator_id, subscription_id = creator["id"], sub["id"]
            fee_bps = int(Decimal(str(creator["platform_fee_percent"])) * 100)
        else:
            creator_id = invoice["target_id"]
            creator = db.execute("SELECT * FROM creators WHERE id=? AND owner_user_id=?", (creator_id, invoice["buyer_id"])).fetchone()
            if not creator:
                raise ValueError("Creator membership ownership mismatch.")
        db.execute("""INSERT OR IGNORE INTO aff_invoice_terms
            (invoice_id,kind,user_id,creator_id,subscription_id,amount_cents,currency,platform_fee_bps)
            VALUES(?,?,?,?,?,?,?,?)""", (invoice["id"],kind,invoice["buyer_id"],creator_id,
                                    subscription_id,invoice["total"],invoice["currency"],fee_bps))

    def record_invoice_result(self, db, invoice, state, reference):
        """Called inside billing.apply_result's transaction, after provider validation."""
        invoice = dict(invoice)
        if invoice["kind"] not in {"subscription", "creator_membership"}:
            return
        terms = db.execute("SELECT * FROM aff_invoice_terms WHERE invoice_id=?", (invoice["id"],)).fetchone()
        # Older pending invoices receive a server-derived snapshot on settlement.
        if not terms:
            self.snapshot_invoice(db, invoice)
            terms = db.execute("SELECT * FROM aff_invoice_terms WHERE invoice_id=?", (invoice["id"],)).fetchone()
        if state == "paid":
            if not reference or not invoice.get("provider") or invoice["provider"] == "demo":
                raise ValueError("A verified provider payment reference is required.")
            if terms["amount_cents"] != invoice["total"] or terms["currency"] != invoice["currency"]:
                raise ValueError("Invoice terms changed after checkout.")
            kwargs = {}
            if invoice["kind"] == "subscription":
                sub = db.execute("SELECT * FROM subscriptions WHERE id=?", (terms["subscription_id"],)).fetchone()
                kwargs.update(period_start=sub["activated_at"], period_end=sub["expires_at"])
            core.record_verified_sale(db, provider=invoice["provider"], payment_id=str(reference),
                kind="membership" if invoice["kind"] == "creator_membership" else "subscription",
                user_id=invoice["buyer_id"], amount_cents=invoice["total"], currency=invoice["currency"],
                creator_id=terms["creator_id"], subscription_id=terms["subscription_id"],
                platform_fee_bps=terms["platform_fee_bps"], event_id=invoice["id"]+":paid", **kwargs)
            db.execute("UPDATE aff_invoice_terms SET sale_provider=?,sale_payment_id=? WHERE invoice_id=?",
                       (invoice["provider"],str(reference),invoice["id"]))
            if invoice["kind"] == "creator_membership":
                db.execute("UPDATE creators SET status='active' WHERE id=? AND status='membership_pending'",
                           (terms["creator_id"],))
        elif state == "review" and terms["sale_payment_id"]:
            core.hold_payment(db, terms["sale_provider"], terms["sale_payment_id"],
                              reason="Provider reported refund or dispute; reconcile exact amount.", actor_id="payment-provider")

    def assert_creator_membership(self, creator_id):
        if not self.creator_status_allowed(creator_id):
            abort(403, description="This creator is not currently available for publishing or sales.")
        if not self.require_membership:
            return
        entitlement = core.creator_entitlement(self.db(), creator_id, is_demo=self.demo)
        if not entitlement["active"]:
            raise MembershipRequired()

    def creator_is_active(self, creator_id):
        return self.creator_status_allowed(creator_id) and (not self.require_membership or
               core.creator_entitlement(self.db(), creator_id, is_demo=self.demo)["active"])

    def creator_status_allowed(self, creator_id):
        if self.platform is None:
            creator = self.creator(creator_id)
            return bool(creator and creator["status"] == "active")
        row = self.db().execute("""SELECT c.status, c.owner_user_id, u.status AS owner_status
            FROM creators c LEFT JOIN users u ON u.id=c.owner_user_id WHERE c.id=?""", (creator_id,)).fetchone()
        return bool(row and row["status"] in {"active", "membership_pending"} and
                    (not row["owner_user_id"] or row["owner_status"] == "active"))

    def validate_checkout_invoice(self, invoice):
        """Reject unsupported terms before collecting money, including old invoices."""
        if invoice["kind"] not in {"subscription", "creator_membership"}:
            return
        settings = core.get_settings(self.db())
        if invoice["currency"] != settings["currency"]:
            abort(409, description="This subscription's currency is not configured for settlement. No payment was started.")
        if invoice["kind"] == "creator_membership" and invoice["total"] != settings["annual_price_cents"]:
            abort(409, description="This membership invoice has outdated pricing. Contact the owner before paying.")

    def membership_checkout(self, creator, user):
        if self.demo or self.billing is None or not self.provider_ready():
            abort(503, description="Payment checkout is not connected. No charge has been made.")
        settings = core.get_settings(self.db())
        invoice = self.billing.create_invoice("creator_membership", creator["id"], user["id"],
                    "Annual creator membership — Smell Panties", Decimal(settings["annual_price_cents"])/100,
                    settings["currency"])
        return redirect("/billing/" + invoice["id"])

    def provider_ready(self):
        if self.demo or self.billing is None:
            return False
        return bool(getattr(self.platform, "BILLING_MODE", "") == "live" and
                    any(self.billing.payments.configured().values()))
