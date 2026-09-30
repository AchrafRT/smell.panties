import os
import shutil
import tempfile
import unittest

TEST_STORAGE = tempfile.mkdtemp(prefix="sp-platform-test-")
os.environ["APP_STORAGE_DIR"] = TEST_STORAGE
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ADMIN_PASSWORD"] = "admin-test-password"
os.environ["COOKIE_SECURE"] = "false"
os.environ["BILLING_MODE"] = "launch_free"
os.environ["SEED_DEMO_DATA"] = "true"

import app as platform  # noqa: E402


class PlatformSmokeTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_STORAGE, ignore_errors=True)

    def setUp(self):
        platform.app.config.update(TESTING=True)

    def csrf(self, client):
        with client.session_transaction() as sess:
            return sess["csrf_token"]

    def enter_age(self, client, next_url="/"):
        client.get("/age")
        response = client.post(
            "/age-check",
            data={"csrf_token": self.csrf(client), "adult_confirm": "yes", "next": next_url},
        )
        self.assertEqual(response.status_code, 302)

    def create_fan(self, client, email="fan@example.com", name="Launch Fan", next_url="/"):
        self.enter_age(client, "/signup")
        client.get("/signup")
        response = client.post(
            "/signup",
            data={
                "csrf_token": self.csrf(client),
                "display_name": name,
                "email": email,
                "password": "password123",
                "password_confirm": "password123",
                "adult_confirm": "yes",
                "next": next_url,
            },
        )
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as sess:
            return sess["user_id"]

    def create_creator(self, client, email="creator@example.com", name="Launch Creator", token="Launch Key"):
        self.enter_age(client, "/creator-signup")
        client.get("/creator-signup")
        response = client.post(
            "/creator-signup",
            data={
                "csrf_token": self.csrf(client),
                "display_name": name,
                "email": email,
                "password": "password123",
                "password_confirm": "password123",
                "token_name": token,
                "email_handle": "launch.creator",
                "adult_confirm": "yes",
            },
        )
        self.assertEqual(response.status_code, 302)
        with platform.app.app_context():
            user_id = platform.query_one("SELECT id FROM users WHERE email=?", (email,))["id"]
            return platform.query_one("SELECT * FROM creators WHERE owner_user_id=?", (user_id,))

    def test_health_and_public_profile(self):
        client = platform.app.test_client()
        self.assertEqual(client.get("/health").status_code, 200)
        self.enter_age(client, "/creator/luna-noir")
        response = client.get("/creator/luna-noir")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Create subscriber account", response.data)

    def test_fan_can_signup_subscribe_enter_private_and_message(self):
        client = platform.app.test_client()
        fan_id = self.create_fan(client, "fan-one@example.com", next_url="/creator/luna-noir/subscribe")
        response = client.post("/creator/luna-noir/subscribe", data={"csrf_token": self.csrf(client)})
        self.assertEqual(response.status_code, 302)
        private = client.get("/creator/luna-noir/private")
        self.assertEqual(private.status_code, 200)
        message = client.post(
            f"/creator/luna-noir/messages/{fan_id}",
            data={"csrf_token": self.csrf(client), "body": "Hello creator"},
        )
        self.assertEqual(message.status_code, 302)
        with platform.app.app_context():
            row = platform.query_one("SELECT body FROM messages WHERE fan_user_id=? ORDER BY id DESC", (fan_id,))
            self.assertEqual(row["body"], "Hello creator")

    def test_creator_can_signup_edit_publish_and_create_live(self):
        client = platform.app.test_client()
        creator = self.create_creator(client)
        response = client.post(
            f"/creator/{creator['slug']}/product/create",
            data={
                "csrf_token": self.csrf(client),
                "title": "Launch Product",
                "description": "One of one test product",
                "type": "digital",
                "price": "99.00",
                "currency": "USD",
                "delivery_note": "Release after verified payment",
            },
        )
        self.assertEqual(response.status_code, 302)
        response = client.post(
            f"/creator/{creator['slug']}/post/create",
            data={"csrf_token": self.csrf(client), "title": "Launch post", "body": "Subscriber update"},
        )
        self.assertEqual(response.status_code, 302)
        response = client.post(
            f"/creator/{creator['slug']}/stream/create",
            data={
                "csrf_token": self.csrf(client),
                "title": "Launch live",
                "audience": "public",
                "price_per_minute": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        with platform.app.app_context():
            self.assertIsNotNone(platform.query_one("SELECT id FROM products WHERE creator_id=? AND title='Launch Product'", (creator["id"],)))
            self.assertIsNotNone(platform.query_one("SELECT id FROM streams WHERE creator_id=? AND title='Launch live'", (creator["id"],)))

    def test_one_of_one_order_is_reserved_and_admin_can_fulfill(self):
        fan = platform.app.test_client()
        fan_id = self.create_fan(fan, "buyer@example.com")
        with platform.app.app_context():
            product = platform.query_one("SELECT * FROM products WHERE creator_id='cr_001' AND status='available' LIMIT 1")
            self.assertIsNotNone(product)
            product_id = product["id"]
        response = fan.post(f"/checkout/luna-noir/{product_id}", data={"csrf_token": self.csrf(fan)})
        self.assertEqual(response.status_code, 302)
        with platform.app.app_context():
            order = platform.query_one("SELECT * FROM orders WHERE fan_user_id=? ORDER BY created_at DESC LIMIT 1", (fan_id,))
            product = platform.query_one("SELECT * FROM products WHERE id=?", (product_id,))
            self.assertEqual(order["status"], "pending_payment")
            self.assertEqual(product["status"], "reserved")
            order_id = order["order_id"]

        admin = platform.app.test_client()
        self.enter_age(admin, "/login")
        admin.get("/login")
        response = admin.post(
            "/login",
            data={"csrf_token": self.csrf(admin), "email": "admin", "password": "admin-test-password", "next": "/admin"},
        )
        self.assertEqual(response.status_code, 302)
        response = admin.post(f"/admin/order/{order_id}/mark-paid", data={"csrf_token": self.csrf(admin)})
        self.assertEqual(response.status_code, 302)
        with platform.app.app_context():
            order = platform.query_one("SELECT * FROM orders WHERE order_id=?", (order_id,))
            product = platform.query_one("SELECT * FROM products WHERE id=?", (product_id,))
            self.assertEqual(order["status"], "paid")
            self.assertEqual(product["status"], "sold")

    def test_fan_sp_nude_wallet_and_creator_token_redemption(self):
        client = platform.app.test_client()
        fan_id = self.create_fan(client, "token-fan@example.com", name="Token Fan")
        response = client.post(
            "/creator/luna-noir/token/spend",
            data={"csrf_token": self.csrf(client)},
        )
        self.assertEqual(response.status_code, 302)
        with platform.app.app_context():
            user = platform.query_one("SELECT * FROM users WHERE id=?", (fan_id,))
            creator = platform.query_one("SELECT * FROM creators WHERE slug='luna-noir'")
            balance = platform.query_one(
                "SELECT balance FROM creator_token_balances WHERE fan_user_id=? AND creator_id=?",
                (fan_id, creator["id"]),
            )
            self.assertEqual(user["sp_nudes_balance"], 8)
            self.assertEqual(balance["balance"], 1)
        response = client.post(
            "/creator/luna-noir/token/redeem",
            data={"csrf_token": self.csrf(client)},
        )
        self.assertEqual(response.status_code, 302)
        with platform.app.app_context():
            redemption = platform.query_one(
                "SELECT * FROM token_redemptions WHERE fan_user_id=? ORDER BY created_at DESC LIMIT 1",
                (fan_id,),
            )
            self.assertIsNotNone(redemption)
            self.assertEqual(redemption["status"], "requested")

    def test_safety_report_enters_admin_queue(self):
        client = platform.app.test_client()
        self.enter_age(client, "/report?creator=luna-noir")
        client.get("/report?creator=luna-noir")
        response = client.post(
            "/report",
            data={
                "csrf_token": self.csrf(client),
                "creator": "luna-noir",
                "reporter_email": "reporter@example.com",
                "reason": "other safety",
                "details": "Smoke-test report",
                "content_type": "profile",
            },
        )
        self.assertEqual(response.status_code, 302)
        with platform.app.app_context():
            report = platform.query_one(
                "SELECT * FROM reports WHERE reporter_email=? ORDER BY created_at DESC LIMIT 1",
                ("reporter@example.com",),
            )
            self.assertIsNotNone(report)
            self.assertEqual(report["status"], "open")

    def test_payment_webhook_is_explicit_unconnected_boundary(self):
        client = platform.app.test_client()
        response = client.post("/webhooks/payment", json={"event": "test"})
        self.assertEqual(response.status_code, 501)

    def test_seed_creator_can_be_assigned_a_login(self):
        admin = platform.app.test_client()
        self.enter_age(admin, "/login")
        admin.get("/login")
        admin.post(
            "/login",
            data={"csrf_token": self.csrf(admin), "email": "admin", "password": "admin-test-password", "next": "/admin"},
        )
        with platform.app.app_context():
            seed = platform.query_one("SELECT * FROM creators WHERE owner_user_id IS NULL LIMIT 1")
            self.assertIsNotNone(seed)
            seed_id = seed["id"]
        response = admin.post(
            f"/admin/creator/{seed_id}/assign",
            data={"csrf_token": self.csrf(admin), "email": "seed-owner@example.com", "password": "password123"},
        )
        self.assertEqual(response.status_code, 302)
        with platform.app.app_context():
            seed = platform.query_one("SELECT * FROM creators WHERE id=?", (seed_id,))
            self.assertTrue(seed["owner_user_id"])


if __name__ == "__main__":
    unittest.main()
