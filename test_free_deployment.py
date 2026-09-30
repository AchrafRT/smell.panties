import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class FreeDeploymentTests(unittest.TestCase):
    def run_app(self,storage,code):
        env=os.environ.copy()
        env.update(RENDER='true',FREE_RENDER_PREVIEW='true',APP_STORAGE_DIR=storage,
                   ADMIN_PASSWORD='test-owner-password-123',SECRET_KEY='test-secret-only-123456789012345678901234567890',
                   SEED_DEMO_DATA='true',BILLING_MODE='live',COOKIE_SECURE='true',
                   STRIPE_SECRET_KEY='not-a-real-key',STRIPE_WEBHOOK_SECRET='not-real',
                   PAYPAL_CLIENT_ID='not-real',PAYPAL_CLIENT_SECRET='not-real',PAYPAL_WEBHOOK_ID='not-real',
                   HELEKET_MERCHANT_ID='not-real',HELEKET_API_KEY='not-real')
        result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_render_free_startup_owner_login_and_payment_block(self):
        with tempfile.TemporaryDirectory() as storage:
            self.run_app(storage,'''import app, payments
assert app.BILLING_MODE == 'launch_free'
assert not any(payments.configured().values())
client=app.app.test_client()
health=client.get('/health')
assert health.status_code==200 and health.json['temporary_preview'] is True
client.get('/age')
with client.session_transaction() as s: token=s['csrf_token']
client.post('/age-check',data={'csrf_token':token,'adult_confirm':'yes','next':'/'})
assert b'Temporary preview' in client.get('/').data
response=client.post('/login',data={'csrf_token':token,'email':'admin','password':'test-owner-password-123'})
assert response.status_code==302
assert client.get('/admin').status_code==200
for provider in ['stripe','paypal','heleket']:
    assert client.post('/webhooks/'+provider,json={}).status_code==404
with app.app.app_context():
    assert app.query_one('SELECT COUNT(*) AS n FROM creators')['n']==9
''')

    def test_fresh_storage_reseeds_without_old_accounts(self):
        with tempfile.TemporaryDirectory() as old,tempfile.TemporaryDirectory() as fresh:
            self.run_app(old,'''import app
with app.app.app_context():
    app.execute("INSERT INTO users(id,role,email,password_hash,display_name,created_at) VALUES('old','fan','old@example.test','test','Test',?)",(app.utcnow(),))
    assert app.query_one('SELECT COUNT(*) AS n FROM users')['n']==1
''')
            self.run_app(fresh,'''import app
with app.app.app_context():
    assert app.query_one('SELECT COUNT(*) AS n FROM users')['n']==0
    assert app.query_one('SELECT COUNT(*) AS n FROM creators')['n']==9
''')
