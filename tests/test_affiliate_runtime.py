"""Real host/runtime acceptance tests with isolated storage and no provider network.

Settlement enters at billing.apply_result, the existing verified-provider boundary.
Every simulated provider payment has a unique reference; no checkout is charged.
"""
from datetime import datetime
import importlib
import importlib.util
import os
from pathlib import Path
import secrets
import shutil
import sys
from unittest.mock import Mock, patch

import pytest
from werkzeug.security import generate_password_hash


REPO = Path(__file__).resolve().parents[1]
BASE = 'https://host.example.test'


def test_homepage_affiliate_section_renders_once_outside_title(host):
    response = host.get(host.client(), '/')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<title>Smell Panties — More Than a Scent</title>' in html
    assert html.count('id="affiliates"') == 1
    assert 'Open affiliate portal' in html


@pytest.mark.parametrize('role', ['owner', 'creator'])
def test_portal_logout_ends_existing_host_identity(host, role):
    if role == 'creator':
        client, _creator = host.creator(referred=False)
    else:
        client = host.client()
        response = host.post(client, '/login', email='admin',
                             password=host.p.ADMIN_PASSWORD)
        assert response.status_code == 302
    assert host.post(client, '/affiliate/logout').status_code == 302
    with client.session_transaction(base_url=BASE) as state:
        assert not any(key in state for key in ('admin', 'user_id', 'aff_owner_stamp', 'aff_id'))
    assert host.get(client, '/admin').status_code == 302
    assert host.get(client, '/creator/membership').status_code == 302


class HostScenario:
    def __init__(self, app_module, billing, payments, core, affiliate):
        self.p, self.billing, self.payments, self.core = app_module, billing, payments, core
        self.app = app_module.app
        self.runtime = app_module.affiliate_program
        self.affiliate = affiliate

    def client(self):
        client = self.app.test_client()
        with client.session_transaction(base_url=BASE) as state:
            state.update(adult_gate=True, csrf_token='runtime-test-token')
        return client

    def get(self, client, path):
        return client.get(path, base_url=BASE)

    def post(self, client, path, **data):
        with client.session_transaction(base_url=BASE) as state:
            csrf = state.setdefault('csrf_token', 'runtime-test-token')
        return client.post(path, data={'csrf_token': csrf, **data}, base_url=BASE)

    def follow_referral(self, client, creator=None):
        path = '/r/' + self.affiliate['code']
        if creator:
            path += '/' + creator['slug']
        response = self.get(client, path)
        assert response.status_code == 302
        cookie = client.get_cookie('sp_aff_ref', domain='host.example.test')
        assert cookie and cookie.http_only and cookie.secure
        return cookie.value

    def creator(self, referred=True):
        owner = self.client()
        cookie = self.follow_referral(owner) if referred else None
        suffix = secrets.token_hex(5)
        email = 'creator-' + suffix + '@example.test'
        response = self.post(owner, '/creator-signup', email=email,
                             display_name='Creator ' + suffix, token_name='Token ' + suffix,
                             email_handle='creator-' + suffix, adult_confirm='yes',
                             password='runtime-test-password', password_confirm='runtime-test-password')
        assert response.status_code == 302 and response.location == '/creator/membership'
        if cookie:
            assert owner.get_cookie('sp_aff_ref', domain='host.example.test').value == cookie
        with self.app.app_context():
            creator = dict(self.p.query_one('''SELECT c.* FROM creators c JOIN users u
                ON u.id=c.owner_user_id WHERE u.email=?''', (email,)))
        return owner, creator

    def fan(self, creator=None, platform_referral=False):
        client = self.client()
        if creator or platform_referral:
            self.follow_referral(client, creator)
        suffix = secrets.token_hex(5)
        email = 'fan-' + suffix + '@example.test'
        response = self.post(client, '/signup', email=email, display_name='Fan ' + suffix,
                             adult_confirm='yes', password='runtime-test-password',
                             password_confirm='runtime-test-password')
        assert response.status_code == 302
        with self.app.app_context():
            user = dict(self.p.query_one('SELECT * FROM users WHERE email=?', (email,)))
        return client, user

    def invoice(self, invoice_id):
        with self.app.app_context():
            return dict(self.billing.get_invoice(invoice_id, False))

    def membership_invoice(self, owner):
        response = self.post(owner, '/creator/membership/checkout')
        assert response.status_code == 302 and '/billing/' in response.location
        return self.invoice(response.location.rsplit('/', 1)[-1])

    def subscription_invoice(self, fan, creator):
        response = self.post(fan, '/creator/' + creator['slug'] + '/subscribe')
        assert response.status_code == 302 and '/billing/' in response.location
        return self.invoice(response.location.rsplit('/', 1)[-1])

    def settle(self, invoice, reference=None):
        reference = reference or 'verified-host-' + secrets.token_hex(10)
        with self.app.app_context():
            self.p.execute("UPDATE billing_invoices SET provider='test-provider' WHERE id=?", (invoice['id'],))
            self.billing.apply_result(invoice['id'], 'paid', reference)
            return self.core.get_sale(self.runtime.db(), 'test-provider', reference)

    def paid_creator(self):
        owner, creator = self.creator()
        invoice = self.membership_invoice(owner)
        sale = self.settle(invoice)
        return owner, creator, invoice, sale

    def opt_in(self, owner, allowed=True):
        data = {'allow_promotion': 'on'} if allowed else {}
        assert self.post(owner, '/creator/affiliates', **data).status_code == 302

    def price_twenty(self, owner, creator):
        assert self.post(owner, '/creator/' + creator['slug'] + '/settings',
                         subscription_price='20.00', subscription_currency='USD').status_code == 302


@pytest.fixture
def host(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    for name in ('app.py', 'billing.py', 'payments.py'):
        shutil.copyfile(REPO / name, source / name)
    # Normalize the original flat checkout without loading its .env or any real data.
    templates = source / 'templates'
    templates.mkdir()
    for path in REPO.glob('*.html'):
        shutil.copyfile(path, templates / path.name)
    if (REPO / 'templates').is_dir():
        shutil.copytree(REPO / 'templates', templates, dirs_exist_ok=True)
    seed = source / 'seed_data'
    seed.mkdir()
    if (REPO / 'platform.json').is_file():
        shutil.copyfile(REPO / 'platform.json', seed / 'platform.json')
    for key, value in {
        'APP_STORAGE_DIR': str(tmp_path / 'data'),
        'SECRET_KEY': 'real-runtime-test-secret-with-at-least-32-characters',
        'ADMIN_PASSWORD': 'real-runtime-owner-test-password', 'AFFILIATE_ADMIN_PASSWORD': '',
        'DEMO_PASSWORD': '', 'COOKIE_SECURE': 'true', 'RENDER': '',
        'SEED_DEMO_DATA': 'false', 'FREE_RENDER_PREVIEW': 'false', 'BILLING_MODE': 'live',
        'AFFILIATE_REQUIRE_MEMBERSHIP': 'true', 'PUBLIC_BASE_URL': BASE,
        'STRIPE_SECRET_KEY': '', 'STRIPE_WEBHOOK_SECRET': '',
        'PAYPAL_CLIENT_ID': '', 'PAYPAL_CLIENT_SECRET': '', 'PAYPAL_WEBHOOK_ID': '',
        'HELEKET_MERCHANT_ID': '', 'HELEKET_API_KEY': '',
    }.items():
        monkeypatch.setenv(key, value)
    package_root = REPO if (REPO / 'affiliate_program').is_dir() else REPO.parent / 'affiliate-work'
    assert (package_root / 'affiliate_program').is_dir(), 'Install the real affiliate package before running these tests.'
    monkeypatch.syspath_prepend(str(package_root))

    def load(name, filename):
        spec = importlib.util.spec_from_file_location(name, source / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    # Fresh billing globals and affiliate modules for each test, restored afterwards.
    with patch.dict(sys.modules):
        for name in list(sys.modules):
            if name in {'billing', 'payments', 'affiliate_program'} or name.startswith('affiliate_program.'):
                del sys.modules[name]
        core = importlib.import_module('affiliate_program.core')
        payments = load('payments', 'payments.py')
        billing = load('billing', 'billing.py')
        app_module = load('_affiliate_runtime_host', 'app.py')
        app_module.app.config.update(TESTING=True)

        def no_network(*args, **kwargs):
            raise AssertionError('Real payment-provider network is forbidden in host integration tests.')

        monkeypatch.setattr(payments, 'call', no_network)
        monkeypatch.setattr(payments, 'configured', lambda: {'stripe': True, 'paypal': False, 'heleket': False})
        with app_module.app.app_context():
            linked_user = 'linked-affiliate-user'
            app_module.execute('''INSERT INTO users
                (id,role,email,password_hash,display_name,adult_confirmed,status,created_at)
                VALUES (?,'fan','affiliate@example.test','fixture-hash','Affiliate owner',1,'active',?)''',
                (linked_user, app_module.utcnow()))
            affiliate = core.create_affiliate(app_module.get_db(), username='host-affiliate',
                password_hash=generate_password_hash('affiliate-test-password', method='pbkdf2:sha256:1000'),
                linked_user_id=linked_user, actor_id='test-owner')
            app_module.get_db().commit()
        yield HostScenario(app_module, billing, payments, core, affiliate)


def test_signed_creator_signup_membership_activation_and_annual_renewal(host):
    owner, creator = host.creator()
    visitor = host.client()
    with host.app.app_context():
        db = host.runtime.db()
        referral = host.core.get_account_referral(db, creator['owner_user_id'])
        assert referral['affiliate_id'] == host.affiliate['id']
        assert host.core.get_balances(db, host.affiliate['id'])['earned_cents'] == 0
        assert not host.core.creator_entitlement(db, creator['id'])['active']
        assert host.p.query_one('SELECT status FROM creators WHERE id=?', (creator['id'],))['status'] == 'membership_pending'
        assert host.p.creator_rankings() == []
    assert host.get(visitor, '/creator/' + creator['slug']).status_code == 402
    assert host.get(visitor, '/creator/' + creator['slug'] + '/subscribe').status_code == 402
    assert host.get(owner, '/creator/' + creator['slug']).status_code == 200
    assert host.get(owner, '/creator/membership').status_code == 200
    assert host.post(owner, '/creator/' + creator['slug'] + '/product/create', title='Unpaid product', price='10').status_code == 402
    assert creator['display_name'].encode() not in host.get(visitor, '/').data

    invoice = host.membership_invoice(owner)
    assert invoice['kind'] == 'creator_membership' and invoice['total'] == 5400 and invoice['currency'] == 'USD'
    first = host.settle(invoice)
    assert first['commission_cents'] == first['platform_net_cents'] == 2700
    assert first['creator_deduction_cents'] == 0
    duration = datetime.fromisoformat(first['period_end']) - datetime.fromisoformat(first['period_start'])
    assert 365 <= duration.total_seconds() / 86400 <= 366
    with host.app.app_context():
        db = host.runtime.db()
        host.billing.apply_result(invoice['id'], 'paid', first['payment_id'])
        assert host.core.get_balances(db, host.affiliate['id'])['earned_cents'] == 2700
        assert len(host.core.list_sales(db)) == 1
        assert host.core.creator_entitlement(db, creator['id'])['active']
        assert host.p.query_one('SELECT status FROM creators WHERE id=?', (creator['id'],))['status'] == 'active'
    assert host.get(visitor, '/creator/' + creator['slug']).status_code == 200
    assert creator['display_name'].encode() in host.get(visitor, '/').data
    assert host.post(owner, '/creator/' + creator['slug'] + '/product/create', title='Paid product', price='10').status_code == 302

    renewed_invoice = host.membership_invoice(owner)
    assert renewed_invoice['id'] != invoice['id']
    renewal = host.settle(renewed_invoice)
    assert renewal['period_start'] == first['period_end']
    assert renewal['commission_cents'] == 2700
    with host.app.app_context():
        db = host.runtime.db()
        assert host.core.get_balances(db, host.affiliate['id'])['earned_cents'] == 5400
        assert host.p.query_one('SELECT period_end FROM aff_memberships WHERE creator_id=?', (creator['id'],))['period_end'] == renewal['period_end']


def test_referred_fan_subscription_snapshots_fees_and_keeps_three_percent_on_renewal(host):
    owner, creator, _, _ = host.paid_creator()
    host.price_twenty(owner, creator)
    host.opt_in(owner)
    fan, fan_user = host.fan(creator)
    invoice = host.subscription_invoice(fan, creator)
    with host.app.app_context():
        db = host.runtime.db()
        captured = host.core.get_subscription_referral(db, invoice['target_id'])
        assert captured['fan_user_id'] == fan_user['id']
        assert captured['affiliate_id'] == host.affiliate['id'] and captured['rate_bps'] == 300
        assert captured['status'] == 'pending'
        terms = host.p.query_one('SELECT * FROM aff_invoice_terms WHERE invoice_id=?', (invoice['id'],))
        assert terms['amount_cents'] == 2000 and terms['platform_fee_bps'] == 900
        host.core.update_settings(db, actor_id='test-owner', fan_rate_bps=500)
        db.execute('UPDATE creators SET platform_fee_percent=12 WHERE id=?', (creator['id'],))
        db.commit()
    first = host.settle(invoice)
    assert first['commission_cents'] == first['creator_deduction_cents'] == 60
    assert first['platform_fee_cents'] == 180 and first['creator_net_cents'] == 1760
    assert first['commission_cents'] + first['platform_net_cents'] + first['creator_net_cents'] == 2000
    with host.app.app_context():
        assert host.p.active_subscription(fan_user['id'], creator['id'])
        host.billing.apply_result(invoice['id'], 'paid', first['payment_id'])
        assert host.core.get_balances(host.runtime.db(), host.affiliate['id'])['earned_cents'] == 2760

    # Opt-out stops new attribution. The already-paid subscription keeps its original rate.
    host.opt_in(owner, False)
    assert host.post(fan, '/subscription/' + invoice['target_id'] + '/cancel').status_code == 302
    renewal_invoice = host.subscription_invoice(fan, creator)
    renewal = host.settle(renewal_invoice)
    assert renewal_invoice['id'] != invoice['id']
    assert renewal['commission_rate_bps'] == 300 and renewal['commission_cents'] == 60
    assert renewal['platform_fee_cents'] == 240 and renewal['creator_net_cents'] == 1700
    with host.app.app_context():
        balances = host.core.get_balances(host.runtime.db(), host.affiliate['id'])
        assert balances['earned_cents'] == 2820 and balances['paid_cents'] == 0


def test_platform_referral_and_revoked_pending_creator_referral_earn_no_fan_commission(host):
    owner, creator, _, _ = host.paid_creator()
    host.price_twenty(owner, creator)
    host.opt_in(owner)
    platform_fan, platform_user = host.fan(platform_referral=True)
    platform_invoice = host.subscription_invoice(platform_fan, creator)
    referred_fan, _ = host.fan(creator)
    pending_invoice = host.subscription_invoice(referred_fan, creator)
    host.opt_in(owner, False)
    generic = host.settle(platform_invoice)
    revoked = host.settle(pending_invoice)
    assert generic['commission_cents'] == revoked['commission_cents'] == 0
    assert generic['creator_net_cents'] == revoked['creator_net_cents'] == 1820
    with host.app.app_context():
        db = host.runtime.db()
        assert host.core.get_account_referral(db, platform_user['id'])
        assert host.core.get_subscription_referral(db, platform_invoice['target_id']) is None
        captured = host.core.get_subscription_referral(db, pending_invoice['target_id'])
        assert captured['status'] == 'ineligible' and captured['ineligible_reason'] == 'creator_opted_out'
        assert host.core.get_balances(db, host.affiliate['id'])['earned_cents'] == 2700


def test_snapshot_mismatch_and_nested_core_failure_roll_back_paid_access(host, monkeypatch):
    owner, creator, _, _ = host.paid_creator()
    host.price_twenty(owner, creator)
    host.opt_in(owner)
    fan, user = host.fan(creator)
    invoice = host.subscription_invoice(fan, creator)
    reference = 'verified-rollback-' + secrets.token_hex(8)
    with host.app.app_context():
        db = host.runtime.db()
        host.p.execute("UPDATE billing_invoices SET total=2100,provider='test-provider' WHERE id=?", (invoice['id'],))
        with pytest.raises(ValueError, match='terms changed'):
            host.billing.apply_result(invoice['id'], 'paid', reference)
        assert host.billing.get_invoice(invoice['id'], False)['status'] == 'pending'
        assert host.p.active_subscription(user['id'], creator['id']) is None
        host.p.execute('UPDATE billing_invoices SET total=2000 WHERE id=?', (invoice['id'],))
        original = host.core.record_verified_sale

        def fail_after_writes(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('ledger unavailable after nested writes')

        with monkeypatch.context() as scoped:
            scoped.setattr(host.core, 'record_verified_sale', fail_after_writes)
            with pytest.raises(RuntimeError, match='ledger unavailable'):
                host.billing.apply_result(invoice['id'], 'paid', reference)
        assert host.billing.get_invoice(invoice['id'], False)['status'] == 'pending'
        assert host.core.get_sale(db, 'test-provider', reference) is None
        assert host.core.get_subscription_referral(db, invoice['target_id'])['status'] == 'pending'
        assert host.p.active_subscription(user['id'], creator['id']) is None
        assert host.core.get_balances(db, host.affiliate['id'])['earned_cents'] == 2700
        host.billing.apply_result(invoice['id'], 'paid', reference)
        assert host.core.get_sale(db, 'test-provider', reference)['commission_cents'] == 60
        assert host.p.active_subscription(user['id'], creator['id'])


def test_provider_review_holds_commission_without_inventing_a_refund(host):
    owner, creator, invoice, sale = host.paid_creator()
    with host.app.app_context():
        db = host.runtime.db()
        host.billing.apply_result(invoice['id'], 'review', 'provider-review-with-unknown-refund-amount')
        host.billing.apply_result(invoice['id'], 'review', 'repeated-review')
        balances = host.core.get_balances(db, host.affiliate['id'])
        assert balances['earned_cents'] == balances['held_cents'] == 2700
        assert balances['reversed_cents'] == balances['available_cents'] == 0
        assert db.execute('SELECT COUNT(*) FROM aff_refunds').fetchone()[0] == 0
        assert host.core.get_sale(db, 'test-provider', sale['payment_id'])['commission_cents'] == 2700
        assert host.core.creator_entitlement(db, creator['id'])['status'] == 'payment_review'
        assert host.p.creator_rankings() == []
    assert host.post(owner, '/creator/' + creator['slug'] + '/product/create', title='Held product', price='10').status_code == 402
    assert host.get(owner, '/creator/' + creator['slug'] + '/dashboard').status_code == 200
    assert host.get(host.client(), '/creator/' + creator['slug']).status_code == 402


def test_invalid_currency_and_old_pending_invoice_are_blocked_before_provider(host, monkeypatch):
    owner, creator, _, _ = host.paid_creator()
    host.opt_in(owner)
    fan, _ = host.fan(creator)
    assert host.post(owner, '/creator/' + creator['slug'] + '/settings',
                     subscription_price='20', subscription_currency='CAD').status_code == 302
    response = host.post(fan, '/creator/' + creator['slug'] + '/subscribe')
    assert response.status_code == 409
    with host.app.app_context():
        assert host.p.query_one("SELECT id FROM billing_invoices WHERE kind='subscription'") is None
    host.price_twenty(owner, creator)
    invoice = host.subscription_invoice(fan, creator)
    with host.app.app_context():
        host.p.execute('DELETE FROM aff_invoice_terms WHERE invoice_id=?', (invoice['id'],))
        host.p.execute("UPDATE billing_invoices SET currency='CAD' WHERE id=?", (invoice['id'],))
    create = Mock(return_value=('should-not-be-created', BASE + '/provider'))
    monkeypatch.setattr(host.payments, 'create', create)
    response = host.post(fan, '/billing/' + invoice['id'] + '/pay/stripe')
    assert response.status_code == 409
    create.assert_not_called()
    assert host.invoice(invoice['id'])['provider'] is None


def test_membership_checkout_ownership_and_status_are_preserved(host):
    owner, creator = host.creator()
    fan, user = host.fan()
    assert host.post(fan, '/creator/membership/checkout', creator_id=creator['id']).status_code == 403
    with host.app.app_context():
        with pytest.raises(ValueError, match='ownership mismatch'):
            host.billing.create_invoice('creator_membership', creator['id'], user['id'], 'Wrong owner', 54, 'USD')
        assert host.p.query_one("SELECT id FROM billing_invoices WHERE kind='creator_membership'") is None
    invoice = host.membership_invoice(owner)
    with host.app.app_context():
        host.p.execute("UPDATE creators SET status='suspended' WHERE id=?", (creator['id'],))
    host.settle(invoice)
    with host.app.app_context():
        assert host.p.query_one('SELECT status FROM creators WHERE id=?', (creator['id'],))['status'] == 'suspended'


@pytest.mark.parametrize('creator_status', ['suspended', 'deleted'])
def test_paid_inactive_creator_cannot_publish_or_sell_but_keeps_billing_access(host, monkeypatch, creator_status):
    owner, creator, membership_invoice, _ = host.paid_creator()
    host.opt_in(owner)
    fan, user = host.fan(creator)
    pending_invoice = host.subscription_invoice(fan, creator)
    assert host.post(owner, '/creator/' + creator['slug'] + '/product/create',
                     title='Existing product', price='10').status_code == 302
    assert host.post(owner, '/creator/' + creator['slug'] + '/offer/' + user['id'],
                     title='Existing offer', price='10').status_code == 302
    with host.app.app_context():
        product = host.p.query_one('SELECT id FROM products WHERE creator_id=?', (creator['id'],))['id']
        offer = host.p.query_one('SELECT id FROM offers WHERE creator_id=?', (creator['id'],))['id']
        host.p.execute('UPDATE creators SET status=? WHERE id=?', (creator_status, creator['id']))
        assert host.core.creator_entitlement(host.runtime.db(), creator['id'])['active']
        assert not host.runtime.creator_is_active(creator['id'])
        assert host.p.creator_rankings() == []

    for action, data in (
        ('product/create', {'title': 'Blocked product', 'price': '10'}),
        ('post/create', {'title': 'Blocked post'}),
        ('offer/' + user['id'], {'title': 'Blocked offer', 'price': '10'}),
        ('stream/create', {'title': 'Blocked stream', 'audience': 'public'}),
    ):
        assert host.post(owner, '/creator/' + creator['slug'] + '/' + action, **data).status_code in {403, 404}
    for path in (
        '/creator/' + creator['slug'] + '/subscribe',
        '/checkout/' + creator['slug'] + '/' + product,
        '/offer/' + offer + '/accept',
    ):
        assert host.post(fan, path).status_code in {403, 404}
    assert host.get(fan, '/creator/' + creator['slug']).status_code in {403, 404}

    create = Mock(return_value=('should-not-be-created', BASE + '/provider'))
    monkeypatch.setattr(host.payments, 'create', create)
    assert host.post(fan, '/billing/' + pending_invoice['id'] + '/pay/stripe').status_code == 403
    create.assert_not_called()
    with host.app.app_context():
        assert host.p.query_one('SELECT COUNT(*) AS n FROM products WHERE creator_id=?', (creator['id'],))['n'] == 1
        assert host.p.query_one('SELECT COUNT(*) AS n FROM offers WHERE creator_id=?', (creator['id'],))['n'] == 1

    # Status restrictions do not lock a signed-in owner out of their paid invoice or account controls.
    assert host.get(owner, '/billing/' + membership_invoice['id']).status_code == 200
    assert host.post(owner, '/creator/affiliates').status_code == 302
    assert host.get(owner, '/creator/membership').status_code == 200
