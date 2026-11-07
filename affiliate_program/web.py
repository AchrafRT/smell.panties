"""Password-protected owner and affiliate interfaces; no live payout controls."""
from functools import wraps
import hashlib
import json
import secrets
import sqlite3
import time
from urllib.parse import quote

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, session)

from . import core
from .runtime import money, parse_cents, transaction


def create_blueprint(rt):
    bp = Blueprint("affiliates", __name__, template_folder="templates",
                   static_folder="static", static_url_path="/affiliate/assets")

    def token():
        if not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    def owner_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not rt.is_owner():
                return redirect("/admin/login")
            return view(*args, **kwargs)
        return wrapped

    def affiliate_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not rt.affiliate():
                return redirect("/affiliate/login")
            return view(*args, **kwargs)
        return wrapped

    @bp.before_app_request
    def subdomain():
        expected = rt.app.config.get("AFFILIATE_HOST", "affiliates.smellpanties.com")
        if request.host.split(":", 1)[0].lower() == expected and request.path == "/":
            return redirect("/affiliate")

    @bp.before_request
    def protect_forms():
        token()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
            if not supplied or not secrets.compare_digest(supplied, token()):
                abort(400, description="The form expired. Reload the page and try again.")

    @bp.after_request
    def protect_pages(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        return response

    def render(name, *, role=None, **context):
        affiliate = rt.affiliate()
        return render_template("affiliate_program/" + name, role=role, demo=rt.demo,
                               csrf_token=token(), current_name="Owner" if role == "admin" else
                               affiliate["display_name"] if affiliate else "Creator", **context)

    def login(area):
        if request.method == "POST":
            username = request.form.get("username", "").strip()[:80]
            password = request.form.get("password", "")[:256]
            # Persist throttling across workers. Store a keyed digest, never the address.
            subject = hashlib.sha256((str(rt.app.secret_key) + "|" + str(request.remote_addr) + "|" + area).encode()).hexdigest()
            db, now = rt.db(), int(time.time())
            with transaction(db):
                row = db.execute("SELECT * FROM aff_login_limits WHERE subject=?", (subject,)).fetchone()
                if row and row["started"] > now - 300 and row["failures"] >= 10:
                    abort(429, description="Too many login attempts. Try again in five minutes.")
                db.execute("DELETE FROM aff_login_limits WHERE started<?", (now-300,))
                found = rt.owner_login(username, password) if area == "admin" else core.authenticate_affiliate(db, username, password)
                if area == "affiliate" and found and bool(found.get("is_demo")) != rt.demo:
                    found = None
                if not found:
                    db.execute("""INSERT INTO aff_login_limits(subject,started,failures) VALUES(?,?,1)
                        ON CONFLICT(subject) DO UPDATE SET failures=failures+1""", (subject, now))
                else:
                    db.execute("DELETE FROM aff_login_limits WHERE subject=?", (subject,))
            if not found:
                flash("Username or password is incorrect.", "error")
                return render("login.html", area=area, next_url="/"+area), 401
            adult = session.get("adult_gate", False)
            session.clear()
            if adult:
                session["adult_gate"] = True
            token()
            if area == "admin":
                session["aff_owner_stamp"] = rt.owner_stamp
            else:
                session["aff_id"] = found["id"]
                session["aff_stamp"] = rt.affiliate_stamp(found["id"])
            return redirect("/admin" if area == "admin" else "/affiliate")
        return render("login.html", area=area, next_url="/"+area)

    @bp.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        return login("admin")

    @bp.route("/affiliate/login", methods=["GET", "POST"])
    def affiliate_login():
        return login("affiliate")

    @bp.route("/affiliate/logout", methods=["POST"])
    def logout():
        adult = session.get("adult_gate", False)
        session.clear()
        if adult:
            session["adult_gate"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect("/affiliate/login")

    def creator_context(include_all=False):
        preferences = {r["creator_id"]: r for r in core.list_creator_preferences(rt.db())}
        rows = []
        for c in rt.creator_rows():
            pref = preferences.get(c["id"], {})
            allowed = bool(pref.get("allowed", pref.get("allow_promotion", False)))
            entitlement = core.creator_entitlement(rt.db(), c["id"], is_demo=rt.demo)
            if not include_all and (c["status"] != "active" or not allowed or (not rt.demo and not entitlement["active"])):
                continue
            c.update(allow_promotion=allowed, membership_status=entitlement["status"],
                     subscription_price_display=money(round(c["subscription_price"]*100), c["subscription_currency"]),
                     period_end=entitlement.get("period_end"))
            rows.append(c)
        return rows

    def display_ledger(affiliate_id=None):
        entries = core.list_commissions(rt.db(), affiliate_id=affiliate_id)
        accounts = {a["id"]: a for a in core.list_affiliates(rt.db())}
        rows = []
        for e in entries:
            value = e.get("amount_cents", e.get("commission_cents", 0))
            rows.append({**e, "amount_display": money(value, e.get("currency", "USD")),
                         "reference": e.get("payment_id", e.get("sale_id", "")),
                         "status": e.get("status", e.get("state", "recorded")),
                         "affiliate_name": accounts.get(e.get("affiliate_id"), {}).get("display_name", "Affiliate")})
        return rows[:100]

    def metrics(affiliate_id=None):
        raw = core.get_dashboard(rt.db(), affiliate_id=affiliate_id, is_demo=rt.demo)
        currency = raw.get("currency", "USD")
        where = " WHERE kind='membership'" + (" AND affiliate_id=?" if affiliate_id else "")
        membership_count = rt.db().execute("SELECT COUNT(DISTINCT creator_id) FROM aff_sales"+where, (affiliate_id,) if affiliate_id else ()).fetchone()[0]
        return {**raw, "memberships": membership_count,
                "subscriptions": raw.get("subscription_referrals", 0),
                "earned": money(raw.get("commission_cents", 0), currency),
                "available": money(raw.get("available_cents", 0), currency),
                "paid": money(raw.get("paid_cents", 0), currency),
                "held": money(raw.get("held_cents", 0), currency),
                "gross": money(raw.get("gross_cents", 0), currency)}

    @bp.route("/affiliate")
    @bp.route("/affiliates")
    @affiliate_required
    def dashboard():
        account = rt.affiliate()
        base = rt.public_base()
        creators = creator_context()
        for c in creators:
            c["referral_link"] = base + "/r/" + quote(account["code"]) + "/" + quote(c["slug"])
        return render("dashboard.html", role="affiliate", affiliate=account,
                      metrics=metrics(account["id"]), creators=creators,
                      platform_link=base + "/r/" + quote(account["code"]),
                      ledger=display_ledger(account["id"]), provider_ready=rt.provider_ready())

    @bp.route("/admin")
    @owner_required
    def admin_page():
        settings = core.get_settings(rt.db())
        accounts = core.list_affiliates(rt.db())
        for a in accounts:
            bal = core.get_balances(rt.db(), a["id"])
            a["balance_display"] = money(bal.get("available_cents", 0), settings["currency"])
        creators = creator_context(include_all=True)
        sales = core.list_sales(rt.db())
        payments = [{**s, "amount_display": money(s["amount_cents"],s["currency"]),
                     "remaining_display": money(s["amount_cents"]-s.get("refunded_cents",0),s["currency"])} for s in sales]
        return render("admin.html", role="admin", metrics=metrics(), affiliates=accounts,
                      creators=creators, ledger=display_ledger(), payments=payments,
                      memberships=[{"creator_name":c["display_name"], "status":c["membership_status"],
                                    "period_end":c["period_end"]} for c in creators],
                      settings={**settings, "attribution_days":settings["attribution_window_days"],
                                "enabled":settings["program_enabled"], "creator_annual_price":settings["annual_price_cents"]/100,
                                "creator_commission_percent":settings["creator_rate_bps"]/100,
                                "fan_commission_percent":settings["fan_rate_bps"]/100},
                      audit=[{**a, "actor": a["actor_id"], "detail": json.dumps(a["details"], ensure_ascii=False)} for a in core.list_audit(rt.db())], integrated=rt.platform is not None,
                      provider_ready=rt.provider_ready())

    @bp.route("/admin/affiliates", methods=["POST"])
    @owner_required
    def manage_affiliate():
        try:
            with transaction(rt.db()) as db:
                if request.form.get("action") == "create":
                    password = request.form.get("password", "")
                    if len(password) < (5 if rt.demo else 12):
                        raise ValueError("Use a password of at least 12 characters for a live affiliate.")
                    linked_id = request.form.get("linked_user_id", "").strip() or None
                    if linked_id and rt.platform is not None and not db.execute("SELECT 1 FROM users WHERE id=?", (linked_id,)).fetchone():
                        raise ValueError("The linked user does not exist.")
                    core.create_affiliate(db, username=request.form.get("username", ""), password=password,
                                          display_name=request.form.get("display_name", ""), linked_user_id=linked_id,
                                          is_demo=rt.demo, actor_id="owner")
                    flash("Affiliate account created.", "success")
                elif request.form.get("action") == "update":
                    core.update_affiliate(db, request.form.get("id", ""),
                                          display_name=request.form.get("display_name", ""),
                                          status=request.form.get("status", ""), actor_id="owner")
                    flash("Affiliate account updated.", "success")
                else:
                    raise ValueError("Unknown action.")
        except (ValueError, sqlite3.IntegrityError) as exc:
            flash(str(exc) if isinstance(exc, ValueError) else "That account name already exists.", "error")
        return redirect("/admin#affiliates")

    @bp.route("/admin/settings", methods=["POST"])
    @owner_required
    def settings():
        try:
            with transaction(rt.db()) as db:
                previous = core.get_settings(db)
                currency = request.form.get("currency", "USD").strip().upper()
                if currency not in {"USD", "CAD", "EUR", "GBP"}:
                    raise ValueError("Checkout supports USD, CAD, EUR and GBP.")
                if currency != previous["currency"]:
                    open_terms = db.execute("SELECT 1 FROM aff_invoice_terms LIMIT 1").fetchone()
                    pending = rt.platform is not None and db.execute("SELECT 1 FROM billing_invoices WHERE kind IN ('subscription','creator_membership') AND status='pending' LIMIT 1").fetchone()
                    if open_terms or pending:
                        raise ValueError("Currency is locked while existing checkout terms or pending invoices exist.")
                core.update_settings(db, actor_id="owner", currency=currency,
                                     attribution_window_days=int(request.form.get("attribution_days", "30")),
                                     program_enabled=request.form.get("enabled") == "on")
            flash("Program settings saved. Existing referral terms are preserved.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect("/admin#settings")

    @bp.route("/r/<code>")
    @bp.route("/r/<code>/<slug>")
    def referral(code, slug=None):
        creator = rt.creator(slug=slug) if slug else None
        if slug and not creator:
            abort(404)
        if creator and not rt.demo:
            rt.assert_creator_membership(creator["id"])
        try:
            with transaction(rt.db()) as db:
                core.record_click(db, code, creator_id=creator["id"] if creator else None)
        except ValueError:
            abort(404, description="This referral link is unavailable.")
        target = "/creator/" + quote(slug) if slug else "/"
        if rt.demo:
            target = "/demo/referral" + ("?creator_id="+quote(creator["id"]) if creator else "")
        return rt.set_referral(redirect(target), code, creator["id"] if creator else None)

    @bp.route("/creator/membership")
    def membership():
        creator = rt.current_creator()
        if not creator:
            return redirect("/admin/login" if rt.demo else "/login?next=%2Fcreator%2Fmembership")
        c = next((c for c in creator_context(include_all=True) if c["id"] == creator["id"]), None)
        if c is None:
            # Suspended profiles stay out of promotion, but their owner can
            # still inspect billing and turn off affiliate consent.
            preference = core.get_creator_preference(rt.db(), creator["id"])
            c = {**creator, "allow_promotion": bool(preference["allowed"])}
        settings = core.get_settings(rt.db())
        return render("membership.html", role="creator", creator=c,
                      membership=core.creator_entitlement(rt.db(), creator["id"], is_demo=rt.demo),
                      price_display=money(settings["annual_price_cents"], settings["currency"])+" / year",
                      provider_ready=rt.provider_ready())

    @bp.route("/creator/affiliates", methods=["POST"])
    def creator_consent():
        creator = rt.current_creator()
        if not creator:
            abort(403)
        with transaction(rt.db()) as db:
            core.set_creator_opt_in(db, creator["id"], request.form.get("allow_promotion") == "on",
                                    owner_user_id=creator["owner_user_id"], actor_id=creator["owner_user_id"])
        flash("Affiliate promotion preference saved.", "success")
        return redirect("/creator/membership" + ("?creator_id="+creator["id"] if rt.demo else ""))

    @bp.route("/creator/membership/checkout", methods=["POST"])
    def checkout():
        creator = rt.current_creator()
        if not creator:
            abort(403)
        user = rt.platform.current_user() if rt.platform is not None else None
        return rt.membership_checkout(creator, user)

    @bp.route("/admin/demo-consent", methods=["POST"])
    @owner_required
    def demo_consent():
        if not rt.demo:
            abort(404)
        creator = rt.creator(request.form.get("creator_id"))
        if not creator:
            abort(404)
        with transaction(rt.db()) as db:
            core.set_creator_opt_in(db, creator["id"], request.form.get("allow_promotion") == "on",
                                    owner_user_id=creator["owner_user_id"], actor_id="demo-owner")
        flash("Demo creator preference updated.", "success")
        return redirect("/admin#creators")

    @bp.route("/admin/demo-payment", methods=["POST"])
    @owner_required
    def demo_payment():
        if not rt.demo:
            abort(404)
        try:
            account = core.get_affiliate(rt.db(), request.form.get("affiliate_id", ""))
        except ValueError:
            abort(400)
        creator = rt.creator(request.form.get("creator_id"))
        kind = request.form.get("kind")
        if not account or not creator or kind not in {"creator_membership", "fan_subscription"}:
            abort(400)
        try:
            settings = core.get_settings(rt.db())
            amount = settings["annual_price_cents"] if kind == "creator_membership" else parse_cents(request.form.get("amount", "20"))
            with transaction(rt.db()) as db:
                if kind == "creator_membership":
                    uid, sid = creator["owner_user_id"], None
                    core.attribute_account(db, uid, account["code"], actor_id="demo-owner")
                else:
                    uid = "demo_fan_" + str(account["id"])
                    sid = uid + "_" + creator["id"]
                    core.attribute_subscription(db, sid, uid, creator["id"], account["code"], actor_id="demo-owner")
                core.record_verified_sale(db, provider="demo", payment_id="demo_"+secrets.token_hex(10),
                    kind="membership" if kind == "creator_membership" else "subscription", user_id=uid, amount_cents=amount, currency=settings["currency"],
                    creator_id=creator["id"], subscription_id=sid, is_demo=True,
                    platform_fee_bps=900 if kind == "fan_subscription" else 0,
                    actor_id="demo-owner")
            flash("Simulated payment recorded. No money was charged or transferred.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect("/admin#simulation")

    @bp.route("/admin/demo-refund", methods=["POST"])
    @owner_required
    def demo_refund():
        if not rt.demo:
            abort(404)
        try:
            pid = request.form.get("payment_id", "")
            sale = next((s for s in core.list_sales(rt.db()) if s["payment_id"] == pid and s["provider"] == "demo"), None)
            if not sale:
                raise ValueError("Choose a demo payment.")
            amount = parse_cents(request.form["amount"]) if request.form.get("amount", "").strip() else sale["amount_cents"]-sale.get("refunded_cents",0)
            with transaction(rt.db()) as db:
                core.refund_payment(db, provider="demo", payment_id=pid, refund_id="demo_refund_"+secrets.token_hex(8),
                                    amount_cents=amount, actor_id="demo-owner")
            flash("Simulated refund and commission reversal recorded.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect("/admin#simulation")

    @bp.route("/affiliate/health")
    @owner_required
    def health():
        return jsonify(status="ok", demo=rt.demo, payment_checkout_connected=rt.provider_ready(),
                       recurring_billing_connected=False, automatic_payouts_connected=False)

    return bp
