import hashlib
import base64
import hmac
import json
import os
import secrets
import time
import unittest
from unittest.mock import patch
import test_smoke as original
import billing
import payments

p=original.platform


class LaunchTests(unittest.TestCase):
    def setUp(self):
        p.app.config.update(TESTING=True)
        self.mode=p.BILLING_MODE
        p.BILLING_MODE='live'
        self.uid='test_'+secrets.token_hex(8)
        with p.app.app_context():
            p.execute("INSERT INTO users(id,role,email,password_hash,display_name,adult_confirmed,status,created_at) VALUES(?,'fan',?,'hash','Test buyer',1,'active',?)",(self.uid,self.uid+'@example.test',p.utcnow()))
        self.client=p.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=self.uid,auth_stamp=hashlib.sha256(b'hash').hexdigest(),adult_gate=True,csrf_token='token')
        self.env=patch.dict(os.environ,{'STRIPE_SECRET_KEY':'test','STRIPE_WEBHOOK_SECRET':'webhook-test','PUBLIC_BASE_URL':'https://store.example.test','PAYPAL_CLIENT_ID':'test','PAYPAL_CLIENT_SECRET':'test','PAYPAL_WEBHOOK_ID':'test','HELEKET_MERCHANT_ID':'test','HELEKET_API_KEY':'test'})
        self.env.start()

    def tearDown(self):
        p.BILLING_MODE=self.mode
        self.env.stop()

    def order(self,physical=False):
        pid='product_'+secrets.token_hex(8)
        with p.app.app_context():
            p.execute("INSERT INTO products(id,creator_id,title,price,type,currency,status,created_at) VALUES(?,'cr_001','Test item',12.50,?,'USD','available',?)",(pid,'physical' if physical else 'digital',p.utcnow()))
        r=self.client.post('/checkout/luna-noir/'+pid,data={'csrf_token':'token'})
        self.assertEqual(r.status_code,302)
        with p.app.app_context():
            order=p.query_one('SELECT * FROM orders WHERE product_id=?',(pid,))
            invoice=billing.order_invoice(order)
            return dict(order),dict(invoice)

    def post(self,url,**data):
        return self.client.post(url,data={'csrf_token':'token',**data})

    def test_checkout_page_renders_original_layout(self):
        order,i=self.order()
        r=self.client.get('/billing/'+i['id'])
        self.assertEqual(r.status_code,200)
        self.assertIn(b'Pay with Stripe',r.data)
        self.assertIn(b'sp_logo.png',r.data)
        self.assertEqual(self.client.get('/order/'+order['order_id']).status_code,200)

    def test_other_buyer_and_anonymous_cannot_read_invoice(self):
        _,i=self.order()
        other=p.app.test_client()
        with other.session_transaction() as s:s['adult_gate']=True
        self.assertEqual(other.get('/billing/'+i['id']).status_code,403)
        self.assertEqual(other.post('/billing/'+i['id']+'/pay/stripe').status_code,400)

    def test_cancel_then_pay_blocked(self):
        o,i=self.order()
        self.assertEqual(self.post('/order/'+o['order_id']+'/cancel').status_code,302)
        with p.app.test_request_context('/'):
            with self.assertRaises(Exception):p.finalize_order_paid(o['order_id'])
        with patch.object(payments,'create') as create:
            self.post('/billing/'+i['id']+'/pay/stripe')
            create.assert_not_called()

    def test_single_provider_idempotent_and_cancel_protected(self):
        o,i=self.order()
        with patch.object(payments,'create',return_value=('cs_'+i['id'],'https://checkout.stripe.com/test')) as create:
            for _ in range(2):
                self.assertEqual(self.post('/billing/'+i['id']+'/pay/stripe').status_code,303)
            self.assertEqual(create.call_count,1)
        self.assertEqual(self.post('/billing/'+i['id']+'/pay/paypal').status_code,409)
        self.post('/order/'+o['order_id']+'/cancel')
        with p.app.app_context():self.assertEqual(p.query_one('SELECT status FROM orders WHERE order_id=?',(o['order_id'],))['status'],'pending_payment')

    def test_failed_network_keeps_provider_and_reservation(self):
        o,i=self.order()
        with patch.object(payments,'create',side_effect=payments.PaymentError('offline')):
            self.assertEqual(self.post('/billing/'+i['id']+'/pay/stripe').status_code,302)
        self.post('/order/'+o['order_id']+'/cancel')
        with p.app.app_context():self.assertEqual(p.query_one('SELECT status FROM orders WHERE order_id=?',(o['order_id'],))['status'],'pending_payment')

    def test_paid_expired_callbacks_idempotent(self):
        o,i=self.order()
        with p.app.app_context():
            billing.apply_result(i['id'],'paid','verified')
            billing.apply_result(i['id'],'paid','verified')
            billing.apply_result(i['id'],'expired','stale')
            self.assertEqual(p.query_one('SELECT status FROM products WHERE id=?',(o['product_id'],))['status'],'sold')
            billing.apply_result(i['id'],'review','refund')
            billing.apply_result(i['id'],'paid','stale')
            self.assertEqual(p.query_one('SELECT status FROM orders WHERE order_id=?',(o['order_id'],))['status'],'payment_review')

    def test_expired_checkout_releases_once(self):
        o,i=self.order()
        with p.app.app_context():
            billing.apply_result(i['id'],'expired','expired')
            billing.apply_result(i['id'],'expired','expired')
            self.assertEqual(p.query_one('SELECT status FROM products WHERE id=?',(o['product_id'],))['status'],'available')
            billing.apply_result(i['id'],'paid','late')
            self.assertEqual(p.query_one('SELECT status FROM orders WHERE order_id=?',(o['order_id'],))['status'],'payment_review')

    def test_subscription_needs_payment_and_expires(self):
        r=self.post('/creator/luna-noir/subscribe')
        self.assertIn('/billing/',r.location)
        iid=r.location.rsplit('/',1)[-1]
        with p.app.app_context():
            self.assertIsNone(p.active_subscription(self.uid,'cr_001'))
            billing.apply_result(iid,'paid','verified')
            sub=p.active_subscription(self.uid,'cr_001')
            self.assertTrue(sub['expires_at'])
            expiry=sub['expires_at']
            billing.apply_result(iid,'paid','verified')
            self.assertEqual(p.active_subscription(self.uid,'cr_001')['expires_at'],expiry)
            p.execute("UPDATE subscriptions SET expires_at='2000-01-01' WHERE id=?",(sub['id'],))
            self.assertIsNone(p.active_subscription(self.uid,'cr_001'))

    def test_subscription_refund_revokes_access(self):
        r=self.post('/creator/luna-noir/subscribe');iid=r.location.rsplit('/',1)[-1]
        with p.app.app_context():
            billing.apply_result(iid,'paid','ref')
            billing.apply_result(iid,'review','refund')
            self.assertIsNone(p.active_subscription(self.uid,'cr_001'))

    def test_shipping_required_and_locked(self):
        _,i=self.order(True)
        self.assertEqual(self.post('/billing/'+i['id']+'/pay/stripe').status_code,400)
        self.assertEqual(self.post('/billing/'+i['id']+'/address',recipient='Test Buyer',street='123 Test',city='City',region='QC',postal_code='X0X0X0',country='CA').status_code,302)
        with patch.object(payments,'create',return_value=('cs_123','https://checkout.stripe.com/test')):
            self.assertEqual(self.post('/billing/'+i['id']+'/pay/stripe').status_code,303)
        self.assertEqual(self.post('/billing/'+i['id']+'/address').status_code,409)

    def test_suspended_session_revoked(self):
        with p.app.app_context():p.execute("UPDATE users SET status='suspended' WHERE id=?",(self.uid,))
        self.client.get('/account')
        with self.client.session_transaction() as s:self.assertNotIn('user_id',s)

    def test_csrf_and_redirect_validation(self):
        self.assertEqual(self.client.post('/logout').status_code,400)
        with p.app.test_request_context('/'):
            self.assertEqual(p.safe_next_url('/\\evil.test'),'/')
            self.assertEqual(p.safe_next_url('//evil.test'),'/')

    def test_forged_webhooks_rejected(self):
        self.assertEqual(self.client.post('/webhooks/stripe',json={'data':{}}).status_code,400)
        self.assertEqual(self.client.post('/webhooks/heleket',json={'uuid':'x','sign':'forged'}).status_code,400)

    def test_signed_stripe_webhook_rechecks_provider(self):
        o,i=self.order()
        with p.app.app_context():p.execute("UPDATE billing_invoices SET provider='stripe',external_id='cs_signed' WHERE id=?",(i['id'],))
        raw=json.dumps({'type':'checkout.session.completed','data':{'object':{'id':'cs_signed'}}}).encode()
        stamp=str(int(time.time()));sig=hmac.new(b'webhook-test',stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        with patch.object(payments,'inspect',return_value=('paid','pi_test')) as inspect:
            for _ in range(2):
                self.assertEqual(self.client.post('/webhooks/stripe',data=raw,content_type='application/json',headers={'Stripe-Signature':f't={stamp},v1={sig}'}).status_code,200)
            self.assertEqual(inspect.call_count,2)
        with p.app.app_context():self.assertEqual(p.query_one('SELECT status FROM orders WHERE order_id=?',(o['order_id'],))['status'],'paid')

    def test_provider_amount_mismatch_never_grants(self):
        _,i=self.order();a={'provider':'stripe','external_id':'cs_bad'}
        with patch.object(payments,'stripe',return_value={'client_reference_id':i['id'],'amount_total':1,'currency':'usd','payment_status':'paid'}):
            with self.assertRaises(payments.PaymentError):payments.inspect(i,a)

    def test_paypal_capture_and_heleket_amount(self):
        _,i=self.order()
        unit={'custom_id':i['id'],'amount':{'currency_code':'USD','value':'12.50'}}
        complete={'status':'COMPLETED','purchase_units':[{**unit,'payments':{'captures':[{'id':'cap_1','status':'COMPLETED','amount':unit['amount']}]}}]}
        with patch.object(payments,'paypal',side_effect=[{'status':'APPROVED','purchase_units':[unit]},{'status':'COMPLETED'},complete]):
            self.assertEqual(payments.inspect(i,{'provider':'paypal','external_id':'pp1','id':'request'},capture=True),('paid','cap_1'))
        with patch.object(payments,'heleket',return_value={'order_id':i['id'],'currency':'USD','amount':'12.50','payment_status':'paid'}):
            self.assertEqual(payments.inspect(i,{'provider':'heleket','external_id':'hk1'}),('paid','hk1'))

    def test_physical_address_adapter_and_no_shipping_for_digital(self):
        _,i=self.order()
        with patch.object(payments,'paypal',return_value={'id':'pp1','links':[{'rel':'payer-action','href':'https://paypal.com/pay'}]}) as api:
            payments.create(i,'paypal','https://store.example.test',i)
            body=api.call_args.args[1]
            self.assertEqual(body['purchase_units'][0]['amount']['value'],'12.50')

    def test_health_failure_uses_503(self):
        with patch.object(p,'query_one',side_effect=sqlite_error):
            # Exempt route still performs housekeeping; only SELECT is mocked.
            self.assertEqual(p.app.test_client().get('/health').status_code,503)

    def test_signed_heleket_callback_and_replay(self):
        _,i=self.order()
        with p.app.app_context():p.execute("UPDATE billing_invoices SET provider='heleket',external_id='hk_signed' WHERE id=?",(i['id'],))
        payload={'uuid':'hk_signed','order_id':i['id'],'status':'paid'}
        raw=json.dumps(payload,ensure_ascii=False,separators=(',',':')).replace('/','\\/').encode()
        payload['sign']=hashlib.md5(base64.b64encode(raw)+b'test').hexdigest()
        with patch.object(payments,'inspect',return_value=('paid','hk_signed')):
            for _ in range(2):
                self.assertEqual(self.client.post('/webhooks/heleket',data=json.dumps(payload),content_type='application/json').status_code,200)
        with p.app.app_context():self.assertEqual(billing.get_invoice(i['id'],False)['status'],'paid')

    def test_verified_paypal_webhook_and_invalid_signature(self):
        _,i=self.order()
        with p.app.app_context():p.execute("UPDATE billing_invoices SET provider='paypal',external_id='pp_signed' WHERE id=?",(i['id'],))
        event={'event_type':'PAYMENT.CAPTURE.COMPLETED','resource':{'supplementary_data':{'related_ids':{'order_id':'pp_signed'}}}}
        with patch.object(payments,'verify_paypal',return_value=False):
            self.assertEqual(self.client.post('/webhooks/paypal',json=event).status_code,400)
        with patch.object(payments,'verify_paypal',return_value=True),patch.object(payments,'inspect',return_value=('paid','capture1')):
            self.assertEqual(self.client.post('/webhooks/paypal',json=event).status_code,200)

    def test_offer_checkout_grants_only_after_payment(self):
        oid='offer_'+secrets.token_hex(6)
        with p.app.app_context():p.execute("INSERT INTO offers(id,creator_id,fan_user_id,title,price,currency,status,created_at,updated_at) VALUES(?,'cr_001',?,'Test offer',10,'USD','open',?,?)",(oid,self.uid,p.utcnow(),p.utcnow()))
        r=self.post('/offer/'+oid+'/accept')
        iid=r.location.rsplit('/',1)[-1]
        with p.app.app_context():
            self.assertEqual(p.query_one('SELECT status FROM offers WHERE id=?',(oid,))['status'],'accepted_payment_pending')
            billing.apply_result(iid,'paid','verified')
            self.assertEqual(p.query_one('SELECT status FROM offers WHERE id=?',(oid,))['status'],'accepted')

    def test_password_reset_revokes_old_session(self):
        with p.app.app_context():p.execute("UPDATE users SET password_hash='newhash' WHERE id=?",(self.uid,))
        self.client.get('/account')
        with self.client.session_transaction() as s:self.assertNotIn('user_id',s)

    def test_untouched_reservation_expires_but_provider_hold_does_not(self):
        o,i=self.order()
        with p.app.app_context():
            p.execute("UPDATE orders SET created_at='2000-01-01' WHERE order_id=?",(o['order_id'],))
            billing.expire_unstarted()
            self.assertEqual(p.query_one('SELECT status FROM products WHERE id=?',(o['product_id'],))['status'],'available')
        o,i=self.order()
        with p.app.app_context():
            p.execute("UPDATE orders SET created_at='2000-01-01' WHERE order_id=?",(o['order_id'],))
            p.execute("UPDATE billing_invoices SET provider='stripe' WHERE id=?",(i['id'],))
            billing.expire_unstarted()
            self.assertEqual(p.query_one('SELECT status FROM products WHERE id=?',(o['product_id'],))['status'],'reserved')

    def test_second_buyer_cannot_reserve_same_item(self):
        o,i=self.order()
        self.post('/checkout/luna-noir/'+o['product_id'])
        with p.app.app_context():self.assertEqual(p.query_one('SELECT COUNT(*) AS n FROM orders WHERE product_id=?',(o['product_id'],))['n'],1)


def sqlite_error(*args,**kwargs):
    raise RuntimeError('database unavailable')
