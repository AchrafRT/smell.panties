"""Verified hosted payments, with idempotent access grants and order fulfillment."""
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
import payments

bp = Blueprint('billing', __name__)
platform = None


def expire_unstarted():
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    db=platform.get_db()
    db.execute('BEGIN IMMEDIATE')
    try:
        rows=db.execute("""SELECT * FROM orders o WHERE o.status='pending_payment' AND o.created_at<?
            AND NOT EXISTS(SELECT 1 FROM billing_invoices i WHERE i.kind='order' AND i.target_id=o.order_id AND i.provider IS NOT NULL)""",(cutoff,)).fetchall()
        for row in rows:
            db.execute("UPDATE orders SET status='canceled' WHERE order_id=?",(row['order_id'],))
            db.execute("UPDATE products SET status='available' WHERE id=? AND status='reserved'",(row['product_id'],))
            db.execute("UPDATE billing_invoices SET status='expired' WHERE target_id=? AND kind='order' AND status='pending'",(row['order_id'],))
        db.commit()
    except BaseException:
        db.rollback()
        raise


def install(app, module):
    global platform
    platform = module
    with app.app_context():
        db = platform.get_db()
        columns = {r[1] for r in db.execute('PRAGMA table_info(subscriptions)')}
        if 'expires_at' not in columns:
            db.execute('ALTER TABLE subscriptions ADD COLUMN expires_at TEXT')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS billing_invoices (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_id TEXT NOT NULL,
          buyer_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
          total INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          provider TEXT, external_id TEXT, checkout_url TEXT, reference TEXT,
          created INTEGER NOT NULL, granted INTEGER NOT NULL DEFAULT 0,
          address TEXT NOT NULL DEFAULT '{}', paid_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS billing_open_target
        ON billing_invoices(kind,target_id) WHERE status='pending';
        CREATE UNIQUE INDEX IF NOT EXISTS billing_external
        ON billing_invoices(provider,external_id) WHERE external_id IS NOT NULL;
        ''')
        db.commit()
    app.register_blueprint(bp)


def minor(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > 100000:
            raise ValueError()
        return int((amount * 100).quantize(Decimal('1')))
    except (InvalidOperation, ValueError):
        abort(400, description='A valid positive price is required.')


def create_invoice(kind, target, buyer, title, amount, currency):
    currency = currency.upper()
    if currency not in {'USD', 'CAD', 'EUR', 'GBP'}:
        abort(400, description='Checkout supports USD, CAD, EUR and GBP.')
    db = platform.get_db()
    db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute("SELECT * FROM billing_invoices WHERE kind=? AND target_id=? AND status='pending'", (kind, target)).fetchone()
        if not row:
            iid = 'pay_' + secrets.token_hex(12)
            db.execute('INSERT INTO billing_invoices(id,kind,target_id,buyer_id,title,total,currency,created) VALUES(?,?,?,?,?,?,?,?)',
                       (iid, kind, target, buyer, title, minor(amount), currency, int(time.time())))
            row = db.execute('SELECT * FROM billing_invoices WHERE id=?', (iid,)).fetchone()
        db.commit()
        return row
    except BaseException:
        db.rollback()
        raise


def order_invoice(order):
    return create_invoice('order', order['order_id'], order['fan_user_id'], order['product_title'], order['amount'], order['currency'])


def order_shipping(order):
    user=platform.current_user()
    if order['status']!='paid' and not platform.is_admin() and not (user and user['id']==order['fan_user_id']):
        return None
    row=platform.query_one("SELECT address FROM billing_invoices WHERE kind='order' AND target_id=? ORDER BY created DESC LIMIT 1",(order['order_id'],))
    return json.loads(row['address']) if row else None


def subscription_checkout(creator, user):
    if platform.active_subscription(user['id'], creator['id']):
        return redirect(url_for('creator_private', slug=creator['slug']))
    sub = platform.any_subscription(user['id'], creator['id'])
    if not sub:
        sid = 'sub_' + secrets.token_hex(8)
        try:
            platform.execute('''INSERT INTO subscriptions(id,fan_user_id,creator_id,tier,price,currency,status,billing_state,created_at)
                                VALUES(?,?,?,'Subscriber',?,?,'pending','awaiting_payment',?)''',
                             (sid, user['id'], creator['id'], creator['subscription_price'], creator['subscription_currency'], platform.utcnow()))
        except sqlite3.IntegrityError:
            pass
        sub = platform.any_subscription(user['id'], creator['id'])
    invoice = create_invoice('subscription', sub['id'], user['id'], '30-day access: ' + creator['display_name'], creator['subscription_price'], creator['subscription_currency'])
    return redirect(url_for('billing.invoice_page', iid=invoice['id']))


def offer_checkout(offer):
    if offer['status'] not in {'open', 'accepted_payment_pending'}:
        abort(409)
    invoice = create_invoice('offer', offer['id'], offer['fan_user_id'], offer['title'], offer['price'], offer['currency'])
    platform.execute("UPDATE offers SET status='accepted_payment_pending',updated_at=? WHERE id=?", (platform.utcnow(), offer['id']))
    return redirect(url_for('billing.invoice_page', iid=invoice['id']))


def get_invoice(iid, authorized=True):
    row = platform.query_one('SELECT * FROM billing_invoices WHERE id=?', (iid,))
    if not row:
        abort(404)
    if authorized:
        user = platform.current_user()
        if not platform.is_admin() and not (user and user['id'] == row['buyer_id']):
            abort(403)
    return row


def attempt(row):
    return dict(row)


def apply_result(iid, state, reference):
    db = platform.get_db()
    db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute('SELECT * FROM billing_invoices WHERE id=?', (iid,)).fetchone()
        if not row:
            abort(404)
        if state == 'pending':
            db.commit()
            return
        # Review is sticky; duplicates and stale events can never grant access again.
        if row['status'] == 'review':
            db.commit()
            return
        if state == 'paid' and row['status'] == 'paid':
            db.commit()
            return
        if state == 'paid' and row['status'] != 'pending':
            state = 'review'
        if state == 'expired' and row['status'] != 'pending':
            db.commit()
            return
        if row['kind'] == 'order':
            order = db.execute('SELECT * FROM orders WHERE order_id=?', (row['target_id'],)).fetchone()
            product = db.execute('SELECT * FROM products WHERE id=?', (order['product_id'],)).fetchone()
            if state == 'paid':
                if order['status'] != 'pending_payment' or product['status'] != 'reserved':
                    state = 'review'
                else:
                    db.execute("UPDATE orders SET status='paid',payment_reference=?,paid_at=? WHERE order_id=?", (reference, platform.utcnow(), order['order_id']))
                    db.execute("UPDATE products SET status='sold',buyer_user_id=?,sold_at=? WHERE id=?", (row['buyer_id'], platform.utcnow(), product['id']))
            if state == 'expired' and order['status'] == 'pending_payment':
                db.execute("UPDATE orders SET status='canceled' WHERE order_id=?", (order['order_id'],))
                db.execute("UPDATE products SET status='available' WHERE id=? AND status='reserved'", (product['id'],))
            if state == 'review':
                db.execute("UPDATE orders SET status='payment_review' WHERE order_id=?", (order['order_id'],))
        elif row['kind'] == 'subscription':
            if state == 'paid':
                now = datetime.now(timezone.utc)
                db.execute("UPDATE subscriptions SET status='active',billing_state='paid_30_days',activated_at=?,expires_at=?,canceled_at=NULL,price=?,currency=? WHERE id=?",
                           (now.isoformat(), (now + timedelta(days=30)).isoformat(), row['total']/100, row['currency'], row['target_id']))
            elif state == 'review':
                # Revoke only the grant linked to this invoice, not a newer renewal.
                db.execute("UPDATE subscriptions SET status='pending',billing_state='payment_review' WHERE id=? AND activated_at=?", (row['target_id'], row['paid_at']))
        elif row['kind'] == 'offer':
            db.execute('UPDATE offers SET status=?,updated_at=? WHERE id=?', ('accepted' if state == 'paid' else 'payment_review' if state == 'review' else 'open', platform.utcnow(), row['target_id']))
        paid_at = None
        if state == 'paid' and row['kind'] == 'subscription':
            paid_at = db.execute('SELECT activated_at FROM subscriptions WHERE id=?', (row['target_id'],)).fetchone()[0]
        elif state == 'paid':
            paid_at = platform.utcnow()
        db.execute('UPDATE billing_invoices SET status=?,reference=?,granted=?,paid_at=COALESCE(paid_at,?) WHERE id=?', (state, reference, int(state=='paid'), paid_at, iid))
        db.execute('INSERT INTO audit_log(actor,event,details,created_at) VALUES(?,?,?,?)', ('payment-provider','payment_'+state,iid,platform.utcnow()))
        db.commit()
    except BaseException:
        db.rollback()
        raise


@bp.route('/billing/<iid>')
def invoice_page(iid):
    invoice = get_invoice(iid)
    return render_template('billing.html', invoice=invoice, providers=payments.configured(), needs_address=needs_address(invoice), address=json.loads(invoice['address']))


def needs_address(row):
    if row['kind'] != 'order':
        return False
    product=platform.query_one('SELECT p.type FROM products p JOIN orders o ON o.product_id=p.id WHERE o.order_id=?',(row['target_id'],))
    return bool(product and product['type']=='physical')


@bp.route('/billing/<iid>/address', methods=['POST'])
def shipping_address(iid):
    row=get_invoice(iid)
    if not needs_address(row) or row['provider'] or row['status']!='pending':
        abort(409)
    address={k:request.form.get(k,'').strip()[:200] for k in ['recipient','street','city','region','postal_code','country']}
    address['country']=address['country'].upper()
    if not all(address.values()) or len(address['country'])!=2 or not address['country'].isalpha():
        abort(400,description='Complete every shipping field; use a two-letter country code.')
    platform.execute('UPDATE billing_invoices SET address=? WHERE id=? AND provider IS NULL',(json.dumps(address),iid))
    return redirect(url_for('billing.invoice_page',iid=iid))


@bp.route('/billing/<iid>/pay/<provider>', methods=['POST'])
def pay(iid, provider):
    row = get_invoice(iid)
    if not payments.configured().get(provider):
        abort(400, description='This payment method is not configured.')
    base = (os.getenv('PUBLIC_BASE_URL') or os.getenv('RENDER_EXTERNAL_URL') or '').rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        abort(503, description='The owner must configure PUBLIC_BASE_URL with the public HTTPS website address.')
    if row['status'] != 'pending':
        return redirect(url_for('billing.invoice_page', iid=iid))
    if needs_address(row) and not json.loads(row['address']):
        abort(400,description='Save your shipping address before paying.')
    db = platform.get_db()
    db.execute('BEGIN IMMEDIATE')
    row = db.execute('SELECT * FROM billing_invoices WHERE id=?', (iid,)).fetchone()
    if row['kind'] == 'order':
        order = db.execute('SELECT status FROM orders WHERE order_id=?', (row['target_id'],)).fetchone()
        if not order or order['status'] != 'pending_payment':
            db.rollback()
            abort(409, description='This order is no longer awaiting payment.')
    if row['provider'] and row['provider'] != provider:
        db.rollback()
        abort(409, description='Continue with the original provider to avoid paying twice.')
    if row['status'] != 'pending':
        db.rollback()
        return redirect(url_for('billing.invoice_page',iid=iid))
    db.execute('UPDATE billing_invoices SET provider=?,created=CASE WHEN provider IS NULL THEN ? ELSE created END WHERE id=?', (provider,int(time.time()),iid))
    db.commit()
    row = get_invoice(iid)
    if row['checkout_url']:
        return redirect(row['checkout_url'], code=303)
    if time.time()-row['created'] > 1800:
        abort(409, description='Checkout creation timed out. Contact support to reconcile this invoice before retrying.')
    try:
        external, target = payments.create(dict(row), provider, base, attempt(row))
        parsed = urlparse(target)
        if parsed.scheme != 'https' or not parsed.netloc:
            raise payments.PaymentError('The provider returned an invalid checkout address.')
        platform.execute('UPDATE billing_invoices SET external_id=?,checkout_url=? WHERE id=?', (external, target, iid))
        return redirect(target, code=303)
    except payments.PaymentError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('billing.invoice_page', iid=iid))


@bp.route('/billing/<iid>/check', methods=['POST'])
def check(iid):
    row = get_invoice(iid)
    try:
        state, reference = payments.inspect(dict(row), attempt(row), capture=True)
        apply_result(iid, state, reference)
        flash('Payment status checked.', 'success')
    except payments.PaymentError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('billing.invoice_page', iid=iid))


@bp.route('/payments/paypal/return')
def paypal_return():
    # Return URL never grants access or trusts a browser-supplied payment status.
    row = get_invoice(request.args.get('order',''))
    if request.args.get('token') != row['external_id'] or row['provider'] != 'paypal':
        abort(400)
    return redirect(url_for('billing.invoice_page', iid=row['id']))


@bp.route('/webhooks/<provider>', methods=['POST'])
def webhook(provider):
    if not payments.configured().get(provider):
        abort(404)
    if request.content_length and request.content_length > 1024*1024:
        abort(413)
    event = request.get_json(silent=True)
    if not isinstance(event, dict):
        abort(400)
    try:
        verified = (payments.verify_stripe(request.get_data(),request.headers.get('Stripe-Signature','')) if provider=='stripe'
                    else payments.verify_heleket(event) if provider=='heleket'
                    else payments.verify_paypal(event,request.headers))
        if not verified:
            abort(400)
        external = None
        row = None
        review = False
        if provider == 'heleket':
            external = event.get('uuid')
        elif provider == 'stripe':
            obj = event.get('data',{}).get('object',{})
            typ = event.get('type','')
            if typ.startswith('checkout.session.'):
                external = obj.get('id')
            elif typ in {'charge.refunded','charge.dispute.created'}:
                pi = obj.get('payment_intent')
                if pi:
                    intent = payments.stripe('payment_intents/'+pi)
                    iid = intent.get('metadata',{}).get('order_id')
                    row = platform.query_one("SELECT * FROM billing_invoices WHERE id=? AND provider='stripe'", (iid,))
                    review = True
            else:
                return {'ok':True}
        else:
            obj=event.get('resource',{})
            typ=event.get('event_type','')
            if typ.startswith('CHECKOUT.ORDER.'):
                external=obj.get('id')
            else:
                related=obj.get('supplementary_data',{}).get('related_ids',{})
                external=related.get('order_id')
                capture=related.get('capture_id')
                if not capture:
                    capture=next((x['href'].rstrip('/').split('/')[-1] for x in obj.get('links',[]) if x.get('rel')=='up' and '/captures/' in x.get('href','')),None)
                review=typ in {'PAYMENT.CAPTURE.REFUNDED','PAYMENT.CAPTURE.REVERSED','CUSTOMER.DISPUTE.CREATED'}
                if typ=='CUSTOMER.DISPUTE.CREATED':
                    capture=next((x.get('seller_transaction_id') for x in obj.get('disputed_transactions',[]) if x.get('seller_transaction_id')),capture)
                if capture:
                    row=platform.query_one("SELECT * FROM billing_invoices WHERE reference=? AND provider='paypal'",(capture,))
                if not external and not row and not review:
                    return {'ok':True}
        if not row and external:
            row=platform.query_one('SELECT * FROM billing_invoices WHERE provider=? AND external_id=?',(provider,external))
        if not row:
            return {'error':'Invoice not matched yet; retry notification.'},503
        state,reference=payments.inspect(dict(row),attempt(row),capture=True)
        apply_result(row['id'],'review' if review else state,reference)
        return {'ok':True}
    except payments.PaymentError:
        return {'error':'Verification temporarily unavailable; retry notification.'},503


@bp.route('/api/rtc-config')
def rtc_config():
    if not platform.current_user() and not platform.is_admin():
        abort(403)
    servers=[{'urls':'stun:stun.l.google.com:19302'}]
    turn=os.getenv('TURN_URL','')
    if turn and os.getenv('TURN_SECRET'):
        import hmac, hashlib, base64
        user=str(int(time.time())+3600)+':'+secrets.token_hex(4)
        credential=base64.b64encode(hmac.new(os.environ['TURN_SECRET'].encode(),user.encode(),hashlib.sha1).digest()).decode()
        servers.append({'urls':turn,'username':user,'credential':credential})
    return {'iceServers':servers}


@bp.route('/admin/payments')
def owner_payments():
    if not platform.is_admin():
        abort(403)
    rows=platform.query_all('SELECT * FROM billing_invoices ORDER BY created DESC LIMIT 500')
    return render_template('payments_admin.html',invoices=rows,providers=payments.configured())
