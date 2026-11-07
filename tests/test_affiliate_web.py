"""HTTP acceptance tests for isolated demo, authentication and attribution."""
import hashlib
import time

import pytest

from demo_app import create_app
from affiliate_program import core
from affiliate_program.runtime import transaction


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "demo.sqlite3", testing=True)


@pytest.fixture
def client(app):
    return app.test_client()


def post(client, path, data=None, **kwargs):
    with client.session_transaction() as session:
        session.setdefault("csrf_token", "test-csrf")
        csrf = session["csrf_token"]
    return client.post(path, data={"csrf_token":csrf, **(data or {})}, **kwargs)


def login(client, username="root", password="admin", area="admin"):
    return post(client, f"/{area}/login", {"username":username,"password":password}, follow_redirects=True)


def payment(client, kind="creator_membership", affiliate_id=1, creator_id="demo_creator_1", amount="20"):
    return post(client, "/admin/demo-payment", {"kind":kind,"affiliate_id":str(affiliate_id),
                "creator_id":creator_id,"amount":amount}, follow_redirects=True)


def test_portals_require_login_and_seed_exactly_two_zero_balances(app, client):
    assert client.get("/affiliate").status_code == 302
    assert client.get("/admin").status_code == 302
    with app.app_context():
        rt = app.extensions["affiliate_program"]
        accounts = core.list_affiliates(rt.db())
        assert [a["username"] for a in accounts] == ["affiliate1","affiliate2"]
        assert all(core.get_balances(rt.db(),a["id"])["earned_cents"] == 0 for a in accounts)
        assert core.list_payouts(rt.db()) == []


def test_owner_login_is_server_side_and_csrf_protected(client):
    assert client.post("/admin/login", data={"username":"root","password":"admin"}).status_code == 400
    assert login(client, password="wrong").status_code == 401
    response = login(client)
    assert response.status_code == 200
    assert b"Development demo" in response.data
    assert b"password_hash" not in response.data
    assert "no-store" in response.headers["Cache-Control"]


def test_membership_renewal_and_fan_payment_accounting(app, client):
    assert login(client).status_code == 200
    assert payment(client).status_code == 200
    assert payment(client).status_code == 200
    assert payment(client, "fan_subscription").status_code == 200
    with app.app_context():
        rt=app.extensions["affiliate_program"]
        sales=core.list_sales(rt.db())
        assert len(sales)==3
        fan=next(s for s in sales if s["kind"]=="subscription")
        assert fan["amount_cents"]==2000
        assert fan["commission_cents"]==60
        assert fan["platform_fee_cents"]==180
        assert fan["creator_net_cents"]==1760
        assert core.get_balances(rt.db(),1)["earned_cents"]==5460
        assert core.creator_entitlement(rt.db(),"demo_creator_1",is_demo=True)["active"]
        assert not core.creator_entitlement(rt.db(),"demo_creator_1",is_demo=False)["active"]


def test_partial_demo_refund_updates_ledger_without_money_transfer(app,client):
    login(client)
    payment(client)
    with app.app_context():
        rt=app.extensions["affiliate_program"]
        pid=core.list_sales(rt.db())[0]["payment_id"]
    r=post(client,"/admin/demo-refund",{"payment_id":pid,"amount":"27.00"},follow_redirects=True)
    assert r.status_code==200
    with app.app_context():
        rt=app.extensions["affiliate_program"]
        b=core.get_balances(rt.db(),1)
        assert b["earned_cents"]==2700
        assert b["reversed_cents"]==1350
        assert b["paid_cents"]==0


def test_affiliate_cannot_access_owner_or_another_account(app,client):
    owner=app.test_client();login(owner);payment(owner)
    assert login(client,"affiliate2",area="affiliate").status_code==200
    assert client.get("/admin").status_code==302
    assert post(client,"/admin/settings",{"currency":"CAD"}).status_code==302
    response=client.get("/affiliate?affiliate_id=1")
    assert response.status_code==200
    assert b"$27.00 USD" not in response.data
    with app.app_context():
        assert core.get_settings(app.extensions["affiliate_program"].db())["currency"]=="USD"


def test_opt_out_blocks_new_referrals_and_directory(app,client):
    login(client)
    post(client,"/admin/demo-consent",{"creator_id":"demo_creator_1"})
    with app.app_context():
        code=core.get_affiliate(app.extensions["affiliate_program"].db(),1)["code"]
    assert client.get(f"/r/{code}/creator-1").status_code==404
    affiliate=app.test_client();login(affiliate,"affiliate1",area="affiliate")
    text=affiliate.get("/affiliate").data
    assert b"Creator One" not in text
    assert b"Creator Two" in text


def test_signed_attribution_survives_login_and_rejects_tampering(app,client):
    with app.app_context():
        code=core.get_affiliate(app.extensions["affiliate_program"].db(),1)["code"]
    response=client.get(f"/r/{code}/creator-1")
    assert response.status_code==302
    cookie=client.get_cookie("sp_aff_demo_ref")
    assert cookie and cookie.http_only
    login(client,"affiliate1",area="affiliate")
    with app.test_request_context("/",headers={"Cookie":"sp_aff_demo_ref="+cookie.value}):
        rt=app.extensions["affiliate_program"]
        assert rt.referral()==code
        assert rt.referral("creator","demo_creator_1")==code
    with app.test_request_context("/",headers={"Cookie":"sp_aff_demo_ref="+cookie.value+"x"}):
        assert app.extensions["affiliate_program"].referral() is None


def test_old_click_cannot_be_extended_by_another_click(app,client):
    with app.app_context():
        rt=app.extensions["affiliate_program"]
        first,second=core.list_affiliates(rt.db())
        expired=rt.signer.dumps({"platform":{"code":first["code"],"at":int(time.time())-31*86400},"creators":{}})
    client.set_cookie("sp_aff_demo_ref",expired)
    client.get(f"/r/{second['code']}")
    value=client.get_cookie("sp_aff_demo_ref").value
    with app.test_request_context("/",headers={"Cookie":"sp_aff_demo_ref="+value}):
        assert app.extensions["affiliate_program"].referral()==second["code"]


def test_password_change_and_suspension_revoke_session(app,client):
    login(client,"affiliate1",area="affiliate")
    with app.app_context():
        rt=app.extensions["affiliate_program"]
        with transaction(rt.db()):
            core.update_affiliate(rt.db(),1,password="replacement-demo-password",actor_id="test-owner")
    assert client.get("/affiliate").status_code==302
    assert login(client,"affiliate1","replacement-demo-password",area="affiliate").status_code==200
    with app.app_context():
        rt=app.extensions["affiliate_program"]
        with transaction(rt.db()):
            core.update_affiliate(rt.db(),1,status="suspended",actor_id="test-owner")
    assert client.get("/affiliate").status_code==302


def test_login_throttling_persists_between_requests(client):
    for _ in range(10):
        assert login(client,password="wrong").status_code==401
    assert login(client,password="admin").status_code==429


def test_demo_checkout_does_not_contact_payment_service(app,client):
    login(client)
    response=post(client,"/creator/membership/checkout",{"creator_id":"demo_creator_1"})
    assert response.status_code==503
    health=client.get("/affiliate/health").get_json()
    assert health["automatic_payouts_connected"] is False
    assert health["recurring_billing_connected"] is False
    with app.app_context():
        assert core.list_sales(app.extensions["affiliate_program"].db())==[]


def test_invalid_demo_amount_does_not_create_sale(app,client):
    login(client)
    for amount in ("nan","-2","2.001","Infinity"):
        assert payment(client,"fan_subscription",amount=amount).status_code==200
    with app.app_context():
        assert core.list_sales(app.extensions["affiliate_program"].db())==[]


def test_owner_session_works_across_workers_and_demo_cookies_are_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("AFFILIATE_DEMO_DATA_DIR",str(tmp_path))
    first=create_app(tmp_path/"shared.sqlite3")
    second=create_app(tmp_path/"shared.sqlite3")
    one,two=first.test_client(),second.test_client()
    assert login(one).status_code==200
    cookie=one.get_cookie("sp_aff_demo_session")
    assert cookie is not None
    assert one.get_cookie("session") is None
    two.set_cookie("sp_aff_demo_session",cookie.value)
    assert two.get("/admin").status_code==200
