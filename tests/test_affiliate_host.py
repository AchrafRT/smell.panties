"""Host integration tests, isolated from provider credentials and the portal runtime.

The runtime double checks the agreed transaction contract. The affiliate package's
own tests cover attribution, annual periods, commission arithmetic and refunds.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from flask import abort, session


class RuntimeDouble:
    def __init__(self, host):
        self.host = host
        self.members = set()
        self.account_contexts = []
        self.capture_account = Mock(side_effect=self._capture_account)
        self.capture_subscription = Mock(side_effect=self._capture_subscription)
        self.snapshot_invoice = Mock(side_effect=self._snapshot_invoice)
        self.record_invoice_result = Mock(side_effect=self._record_invoice_result)
        self.assert_creator_membership = Mock(side_effect=self._assert_membership)
        self.validate_checkout_invoice = Mock()
        with host.app.app_context():
            host.get_db().execute("""CREATE TABLE host_test_events (
                phase TEXT NOT NULL, source_id TEXT NOT NULL, details TEXT NOT NULL
            )""")
            host.get_db().commit()

    def _event(self, db, phase, source, **details):
        db.execute('INSERT INTO host_test_events VALUES (?,?,?)',
                   (phase, source, json.dumps(details)))

    def _capture_account(self, user_id, role, db=None, creator_id=None):
        supplied_db = db is not None
        db = db if supplied_db else self.host.get_db()
        assert db.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
        if supplied_db:
            assert db.in_transaction, 'Creator attribution must share the signup transaction.'
        self.account_contexts.append({
            'role': role, 'user_id': user_id, 'creator_id': creator_id,
            'transactional': supplied_db and db.in_transaction,
            'referral_before_clear': session.get('host_test_referral'),
        })
        self._event(db, 'account', user_id, role=role, creator_id=creator_id)
        if not supplied_db:
            db.commit()

    def _capture_subscription(self, subscription_id, fan_id, creator):
        db = self.host.get_db()
        sub = db.execute('SELECT * FROM subscriptions WHERE id=?', (subscription_id,)).fetchone()
        assert sub and sub['fan_user_id'] == fan_id and sub['creator_id'] == creator['id']
        self._event(db, 'subscription', subscription_id, fan_id=fan_id, creator_id=creator['id'])
        db.commit()

    def _snapshot_invoice(self, db, invoice):
        assert db.in_transaction, 'Invoice terms must be snapshotted before invoice commit.'
        if invoice['kind'] == 'subscription':
            assert db.execute("SELECT 1 FROM host_test_events WHERE phase='subscription' AND source_id=?",
                              (invoice['target_id'],)).fetchone(), 'Capture attribution before creating an invoice.'
        self._event(db, 'snapshot', invoice['id'], total=invoice['total'], kind=invoice['kind'])

    def _record_invoice_result(self, db, invoice, state, reference):
        assert db.in_transaction, 'Entitlement and settlement must share the invoice transaction.'
        self._event(db, 'result', invoice['id'], state=state, reference=reference,
                    source_status=invoice['status'])
        if invoice['kind'] == 'creator_membership' and state == 'paid':
            self._event(db, 'membership', invoice['target_id'], invoice_id=invoice['id'])

    def _assert_membership(self, creator_id):
        if creator_id not in self.members:
            abort(403, description='An active creator membership is required.')

    def creator_is_active(self, creator_id):
        return creator_id in self.members


class AffiliateHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='sp-affiliate-host-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        source = Path(cls.temp.name) / 'source'
        source.mkdir()
        repo = Path(__file__).resolve().parents[1]
        # Copy only application code. Never load a developer's .env or production storage.
        for filename in ('app.py', 'billing.py', 'payments.py'):
            shutil.copyfile(repo / filename, source / filename)
        cls.env = patch.dict(os.environ, {
            'APP_STORAGE_DIR': str(Path(cls.temp.name) / 'data'),
            'SECRET_KEY': 'isolated-host-test-secret-with-at-least-32-characters',
            'ADMIN_PASSWORD': 'isolated-host-owner-password', 'DEMO_PASSWORD': '',
            'COOKIE_SECURE': 'false', 'RENDER': '', 'SEED_DEMO_DATA': 'false',
            'FREE_RENDER_PREVIEW': 'false', 'BILLING_MODE': 'live',
            'AFFILIATE_REQUIRE_MEMBERSHIP': 'true',
            'PUBLIC_BASE_URL': 'https://checkout.example.test',
            'STRIPE_SECRET_KEY': '', 'STRIPE_WEBHOOK_SECRET': '',
            'PAYPAL_CLIENT_ID': '', 'PAYPAL_CLIENT_SECRET': '', 'PAYPAL_WEBHOOK_ID': '',
            'HELEKET_MERCHANT_ID': '', 'HELEKET_API_KEY': '',
        })
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        package = types.ModuleType('affiliate_program')

        def install(app, host, billing_module):
            assert app is host.app and billing_module.platform is host
            return RuntimeDouble(host)

        package.install = install
        cls.modules = patch.dict(sys.modules, {'affiliate_program': package})
        cls.modules.start()
        cls.addClassCleanup(cls.modules.stop)

        def load(name, filename):
            spec = importlib.util.spec_from_file_location(name, source / filename)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module

        cls.payments = load('payments', 'payments.py')
        cls.billing = load('billing', 'billing.py')
        cls.host = load('_affiliate_host_contract_app', 'app.py')
        cls.host.app.config.update(TESTING=True)
        # UI rendering is tested separately; these tests exercise real route auth and SQLite writes.
        cls.host.render_template = lambda name, **context: name
        cls.billing.render_template = lambda name, **context: name

    def setUp(self):
        self.p = self.host
        self.runtime = self.p.affiliate_program
        self.runtime.account_contexts.clear()
        for name in ('capture_account', 'capture_subscription', 'snapshot_invoice',
                     'record_invoice_result', 'assert_creator_membership', 'validate_checkout_invoice'):
            getattr(self.runtime, name).reset_mock()
        self.p.BILLING_MODE = 'live'
        suffix = secrets.token_hex(6)
        self.creator_id = 'creator_' + suffix
        self.slug = 'creator-' + suffix
        self.owner_id = self.add_user('creator')
        self.fan_id = self.add_user('fan')
        with self.p.app.app_context():
            self.p.execute('''INSERT INTO creators
                (id,owner_user_id,slug,display_name,handle,business_email,token_name,token_symbol,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (self.creator_id, self.owner_id, self.slug, 'Host Test Creator', '@' + self.slug,
                 suffix + '@example.test', 'Token ' + suffix, 'T' + suffix, self.p.utcnow()))
        self.runtime.members = {self.creator_id}
        self.fan = self.client(self.fan_id)
        self.owner = self.client(self.owner_id)

    def add_user(self, role):
        uid = role + '_' + secrets.token_hex(6)
        with self.p.app.app_context():
            self.p.execute('''INSERT INTO users
                (id,role,email,password_hash,display_name,adult_confirmed,status,created_at)
                VALUES (?,?,?,'test-hash','Host Test',1,'active',?)''',
                (uid, role, uid + '@example.test', self.p.utcnow()))
        return uid

    def client(self, user_id=None):
        client = self.p.app.test_client()
        with client.session_transaction() as state:
            state.update(adult_gate=True, csrf_token='host-token', host_test_referral='clicked-link')
            if user_id:
                state.update(user_id=user_id, auth_stamp=hashlib.sha256(b'test-hash').hexdigest())
        return client

    def post(self, client, path, **data):
        return client.post(path, data={'csrf_token': 'host-token', **data})

    def product(self, status='available'):
        pid = 'product_' + secrets.token_hex(6)
        with self.p.app.app_context():
            self.p.execute('''INSERT INTO products
                (id,creator_id,title,type,price,currency,status,created_at)
                VALUES (?,?,'Test item','digital',12.50,'USD',?,?)''',
                (pid, self.creator_id, status, self.p.utcnow()))
        return pid

    def stream(self):
        sid = 'stream_' + secrets.token_hex(6)
        with self.p.app.app_context():
            self.p.execute('''INSERT INTO streams(id,creator_id,title,audience,status,created_at)
                VALUES (?,?,'Test stream','public','scheduled',?)''',
                (sid, self.creator_id, self.p.utcnow()))
        return sid

    def offer(self):
        oid = 'offer_' + secrets.token_hex(6)
        with self.p.app.app_context():
            self.p.execute('''INSERT INTO offers
                (id,creator_id,fan_user_id,title,price,currency,status,created_at,updated_at)
                VALUES (?,?,?,'Test offer',10,'USD','open',?,?)''',
                (oid, self.creator_id, self.fan_id, self.p.utcnow(), self.p.utcnow()))
        return oid

    def subscription_invoice(self):
        response = self.post(self.fan, f'/creator/{self.slug}/subscribe')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/billing/', response.location)
        with self.p.app.app_context():
            return dict(self.billing.get_invoice(response.location.rsplit('/', 1)[-1], False))

    def signup_data(self, creator=False):
        suffix = secrets.token_hex(6)
        data = dict(email=suffix + '@example.test', display_name='Signup ' + suffix,
                    password='host-test-password', password_confirm='host-test-password', adult_confirm='yes')
        if creator:
            data.update(token_name='Signup Token ' + suffix, email_handle='signup-' + suffix)
        return data

    def test_signups_capture_before_session_clear_and_creator_requires_membership(self):
        for role, path in (('fan', '/signup'), ('creator', '/creator-signup')):
            with self.subTest(role=role):
                client = self.client()
                data = self.signup_data(role == 'creator')
                response = self.post(client, path, **data)
                self.assertEqual(response.status_code, 302)
                captured = self.runtime.account_contexts[-1]
                self.assertEqual(captured['role'], role)
                self.assertEqual(captured['referral_before_clear'], 'clicked-link')
                self.assertEqual(captured['transactional'], role == 'creator')
                with client.session_transaction() as state:
                    self.assertEqual(state['user_id'], captured['user_id'])
                    self.assertNotIn('host_test_referral', state)
                if role == 'creator':
                    self.assertEqual(response.location, '/creator/membership')
                    with self.p.app.app_context():
                        creator = self.p.query_one('SELECT owner_user_id,status FROM creators WHERE id=?',
                                                  (captured['creator_id'],))
                        self.assertEqual(creator['owner_user_id'], captured['user_id'])
                        self.assertEqual(creator['status'], 'membership_pending')

    def test_creator_signup_rolls_back_when_attribution_cannot_be_saved(self):
        data = self.signup_data(True)

        def fail(*args, **kwargs):
            self.runtime._capture_account(*args, **kwargs)
            raise RuntimeError('attribution storage unavailable')

        with patch.object(self.runtime, 'capture_account', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'attribution storage'):
                self.post(self.client(), '/creator-signup', **data)
        captured = self.runtime.account_contexts[-1]
        with self.p.app.app_context():
            self.assertIsNone(self.p.query_one('SELECT id FROM users WHERE email=?', (data['email'],)))
            self.assertIsNone(self.p.query_one('SELECT id FROM creators WHERE id=?', (captured['creator_id'],)))
            self.assertIsNone(self.p.query_one('SELECT 1 FROM host_test_events WHERE source_id=?', (captured['user_id'],)))

    def test_unpaid_creator_cannot_publish_or_start_media_through_direct_routes(self):
        self.runtime.members.clear()
        product = self.product('archived')
        stream = self.stream()
        actions = [
            (f'/creator/{self.slug}/product/create', dict(title='Product', price='10')),
            (f'/creator/{self.slug}/product/{product}/status', dict(status='available')),
            (f'/creator/{self.slug}/post/create', dict(title='Post')),
            (f'/creator/{self.slug}/offer/{self.fan_id}', dict(title='Offer', price='10')),
            (f'/creator/{self.slug}/stream/create', dict(audience='public')),
            (f'/stream/{stream}/status', dict(status='live')),
        ]
        for path, data in actions:
            with self.subTest(path=path):
                self.assertEqual(self.post(self.owner, path, **data).status_code, 403)
        self.assertEqual(self.owner.get(f'/creator/{self.slug}/video/{self.fan_id}').status_code, 403)
        for path in (f'/api/stream/{stream}/signal', f'/api/call/{self.creator_id}:{self.fan_id}/signal'):
            with self.subTest(path=path):
                response = self.owner.post(path, json={'to': 'viewer', 'payload': {'type': 'offer'}},
                                           headers={'X-CSRF-Token': 'host-token'})
                self.assertEqual(response.status_code, 403)
        with self.p.app.app_context():
            self.assertEqual(self.p.query_one('SELECT status FROM products WHERE id=?', (product,))['status'], 'archived')
            self.assertEqual(self.p.query_one('SELECT status FROM streams WHERE id=?', (stream,))['status'], 'scheduled')
            for table in ('private_posts', 'offers'):
                self.assertEqual(self.p.query_one(f'SELECT COUNT(*) AS n FROM {table} WHERE creator_id=?',
                                                 (self.creator_id,))['n'], 0)

    def test_unpaid_creator_can_view_manage_and_stop_existing_content(self):
        self.runtime.members.clear()
        product, stream = self.product(), self.stream()
        self.assertEqual(self.owner.get(f'/creator/{self.slug}/dashboard').status_code, 200)
        self.assertEqual(self.owner.get(f'/creator/{self.slug}').status_code, 200)
        self.assertEqual(self.post(self.owner, f'/creator/{self.slug}/settings', bio='Updated profile').status_code, 302)
        self.assertEqual(self.post(self.owner, f'/creator/{self.slug}/product/{product}/status', status='archived').status_code, 302)
        self.assertEqual(self.post(self.owner, f'/stream/{stream}/status', status='ended').status_code, 302)
        self.runtime.assert_creator_membership.assert_not_called()

    def test_unpaid_creator_cannot_receive_new_fan_purchases(self):
        self.runtime.members.clear()
        product, offer = self.product(), self.offer()
        for path in (f'/creator/{self.slug}/subscribe', f'/checkout/{self.slug}/{product}', f'/offer/{offer}/accept'):
            with self.subTest(path=path):
                self.assertEqual(self.post(self.fan, path).status_code, 403)
        self.assertEqual(self.fan.get(f'/creator/{self.slug}/subscribe').status_code, 403)
        self.assertEqual(self.post(self.fan, f'/offer/{offer}/decline').status_code, 302)
        with self.p.app.app_context():
            self.assertIsNone(self.p.any_subscription(self.fan_id, self.creator_id))
            self.assertIsNone(self.p.query_one('SELECT id FROM orders WHERE creator_id=?', (self.creator_id,)))

    def test_existing_invoices_recheck_membership_before_contacting_provider(self):
        invoices = [self.subscription_invoice()]
        product, offer = self.product(), self.offer()
        self.assertEqual(self.post(self.fan, f'/checkout/{self.slug}/{product}').status_code, 302)
        with self.p.app.app_context():
            order = self.p.query_one('SELECT * FROM orders WHERE product_id=?', (product,))
            invoices.append(dict(self.billing.order_invoice(order)))
        response = self.post(self.fan, f'/offer/{offer}/accept')
        with self.p.app.app_context():
            invoices.append(dict(self.billing.get_invoice(response.location.rsplit('/', 1)[-1], False)))
        self.runtime.members.clear()
        with patch.object(self.payments, 'configured', return_value={'stripe': True}), \
                patch.object(self.payments, 'create') as create:
            for invoice in invoices:
                with self.subTest(kind=invoice['kind']):
                    self.assertEqual(self.post(self.fan, f"/billing/{invoice['id']}/pay/stripe").status_code, 403)
            create.assert_not_called()

    def test_membership_renewal_checkout_is_payable_when_creator_is_unpaid(self):
        self.runtime.members.clear()
        with self.p.app.app_context():
            invoice = dict(self.billing.create_invoice('creator_membership', self.creator_id,
                                                       self.owner_id, 'Creator annual membership', 54, 'USD'))
        with patch.object(self.payments, 'configured', return_value={'stripe': True}), \
                patch.object(self.payments, 'create', return_value=('cs_membership_test', 'https://checkout.example.test/pay')) as create:
            response = self.post(self.owner, f"/billing/{invoice['id']}/pay/stripe")
            self.assertEqual(response.status_code, 303)
            self.assertEqual(create.call_args.args[0]['total'], 5400)
        self.runtime.assert_creator_membership.assert_not_called()

    def test_attribution_is_captured_for_live_manual_and_free_subscriptions(self):
        for mode in ('live', 'manual', 'launch_free'):
            with self.subTest(mode=mode):
                self.p.BILLING_MODE = mode
                fan_id = self.add_user('fan')
                response = self.post(self.client(fan_id), f'/creator/{self.slug}/subscribe')
                self.assertEqual(response.status_code, 302)
                with self.p.app.app_context():
                    sub = self.p.any_subscription(fan_id, self.creator_id)
                    self.assertIsNotNone(self.p.query_one("SELECT 1 FROM host_test_events WHERE phase='subscription' AND source_id=?", (sub['id'],)))
                    invoice = self.p.query_one('SELECT * FROM billing_invoices WHERE target_id=?', (sub['id'],))
                    self.assertEqual(invoice is not None, mode == 'live')
        self.runtime.record_invoice_result.assert_not_called()

    def test_invoice_snapshot_is_immutable_and_rolls_back_with_invoice(self):
        with self.p.app.app_context():
            invoice = self.billing.create_invoice('creator_membership', self.creator_id, self.owner_id, 'Membership', 54, 'USD')
            repeated = self.billing.create_invoice('creator_membership', self.creator_id, self.owner_id, 'Changed', 99, 'USD')
            self.assertEqual(invoice['id'], repeated['id'])
            self.assertEqual(repeated['total'], 5400)
            self.runtime.snapshot_invoice.assert_called_once()

            def fail(db, row):
                self.runtime._snapshot_invoice(db, row)
                raise RuntimeError('snapshot unavailable')

            with patch.object(self.runtime, 'snapshot_invoice', side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, 'snapshot unavailable'):
                    self.billing.create_invoice('creator_membership', 'another-period', self.owner_id, 'Membership', 54, 'USD')
            self.assertIsNone(self.p.query_one("SELECT id FROM billing_invoices WHERE target_id='another-period'"))
            self.assertEqual(self.p.query_one("SELECT COUNT(*) AS n FROM host_test_events WHERE phase='snapshot' AND source_id=?", (invoice['id'],))['n'], 1)

    def test_failed_settlement_rolls_back_access_invoice_audit_and_runtime_writes(self):
        subscription_invoice = self.subscription_invoice()
        with self.p.app.app_context():
            membership_invoice = dict(self.billing.create_invoice('creator_membership', self.creator_id,
                                                                  self.owner_id, 'Membership', 54, 'USD'))
            for invoice in (subscription_invoice, membership_invoice):
                with self.subTest(kind=invoice['kind']):
                    def fail(db, row, state, reference):
                        self.runtime._record_invoice_result(db, row, state, reference)
                        raise RuntimeError('settlement unavailable')

                    with patch.object(self.runtime, 'record_invoice_result', side_effect=fail):
                        with self.assertRaisesRegex(RuntimeError, 'settlement unavailable'):
                            self.billing.apply_result(invoice['id'], 'paid', 'verified-test-reference')
                    self.assertEqual(self.billing.get_invoice(invoice['id'], False)['status'], 'pending')
                    self.assertIsNone(self.p.query_one("SELECT 1 FROM host_test_events WHERE phase='result' AND source_id=?", (invoice['id'],)))
                    self.assertIsNone(self.p.query_one("SELECT 1 FROM audit_log WHERE event='payment_paid' AND details=?", (invoice['id'],)))
            self.assertIsNone(self.p.active_subscription(self.fan_id, self.creator_id))
            self.assertIsNone(self.p.query_one("SELECT 1 FROM host_test_events WHERE phase='membership' AND source_id=?", (self.creator_id,)))

    def test_duplicate_and_out_of_order_results_do_not_repeat_settlement(self):
        invoice = self.subscription_invoice()
        with self.p.app.app_context():
            self.billing.apply_result(invoice['id'], 'paid', 'verified-reference')
            expiry = self.p.active_subscription(self.fan_id, self.creator_id)['expires_at']
            self.billing.apply_result(invoice['id'], 'paid', 'duplicate')
            self.billing.apply_result(invoice['id'], 'expired', 'stale')
            self.assertEqual(self.p.active_subscription(self.fan_id, self.creator_id)['expires_at'], expiry)
            self.runtime.record_invoice_result.assert_called_once()
            self.billing.apply_result(invoice['id'], 'review', 'provider-review')
            self.billing.apply_result(invoice['id'], 'review', 'duplicate-review')
            self.billing.apply_result(invoice['id'], 'paid', 'late-paid')
            self.assertEqual(self.runtime.record_invoice_result.call_count, 2)
            args = self.runtime.record_invoice_result.call_args.args
            self.assertEqual(args[1]['status'], 'paid')
            self.assertEqual(args[2:], ('review', 'provider-review'))
            self.assertIsNone(self.p.active_subscription(self.fan_id, self.creator_id))

    def test_membership_entitlement_is_delegated_once_with_verified_result(self):
        with self.p.app.app_context():
            invoice = self.billing.create_invoice('creator_membership', self.creator_id, self.owner_id, 'Membership', 54, 'USD')
            self.billing.apply_result(invoice['id'], 'paid', 'membership-reference')
            self.billing.apply_result(invoice['id'], 'paid', 'membership-reference')
            self.assertEqual(self.billing.get_invoice(invoice['id'], False)['status'], 'paid')
            self.assertEqual(self.p.query_one("SELECT COUNT(*) AS n FROM host_test_events WHERE phase='membership' AND source_id=?", (self.creator_id,))['n'], 1)
            self.runtime.record_invoice_result.assert_called_once()

    def test_root_login_alias_uses_existing_environment_password(self):
        client = self.client()
        self.assertEqual(self.post(client, '/login', email='root', password='admin').status_code, 401)
        response = self.post(client, '/login', email='root', password=self.p.ADMIN_PASSWORD)
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as state:
            self.assertTrue(state['admin'])


if __name__ == '__main__':
    unittest.main()
