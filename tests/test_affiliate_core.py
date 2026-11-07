"""Financial and authorization invariants for the standalone affiliate core."""

import sqlite3
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from affiliate_program import core


NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
PASSWORD_HASH = generate_password_hash("test-password-for-affiliates", method="pbkdf2:sha256:1000")


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(core, "_now", lambda: NOW)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    core.init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def affiliate(db):
    return core.create_affiliate(db, username="partner-a", password_hash=PASSWORD_HASH,
        linked_user_id="affiliate-owner-a", actor_id="owner")


@pytest.fixture
def affiliate_b(db):
    return core.create_affiliate(db, username="partner-b", password_hash=PASSWORD_HASH,
        linked_user_id="affiliate-owner-b", actor_id="owner")


def opted_in(db, creator="creator-a", owner="creator-owner-a"):
    return core.set_creator_opt_in(db, creator, True, actor_id=owner, owner_user_id=owner)


def account_referral(db, affiliate, user="creator-owner-a"):
    return core.attribute_account(db, user, affiliate["code"])


def subscription_referral(db, affiliate, subscription="sub-a", fan="fan-a", creator="creator-a"):
    opted_in(db, creator)
    return core.attribute_subscription(db, subscription, fan, creator, affiliate["code"])


def membership_sale(db, payment="membership-1", **overrides):
    args = {"provider":"test-provider", "payment_id":payment, "kind":"membership",
        "user_id":"creator-owner-a", "creator_id":"creator-a", "subscription_id":"membership-a",
        "amount_cents":5400, "currency":"USD", "actor_id":"verified-provider"}
    args.update(overrides)
    return core.record_verified_sale(db, **args)


def subscription_sale(db, payment="payment-1", **overrides):
    args = {"provider":"test-provider", "payment_id":payment, "kind":"subscription",
        "user_id":"fan-a", "creator_id":"creator-a", "subscription_id":"sub-a",
        "amount_cents":2000, "currency":"USD", "platform_fee_bps":900,
        "actor_id":"verified-provider"}
    args.update(overrides)
    return core.record_verified_sale(db, **args)


def refund(db, payment, refund_id, amount):
    return core.refund_payment(db, provider="test-provider", payment_id=payment,
        refund_id=refund_id, amount_cents=amount, actor_id="verified-provider")


def assert_error(code, call, *args, **kwargs):
    with pytest.raises(core.AffiliateError) as exc:
        call(*args, **kwargs)
    assert exc.value.code == code


def test_settings_defaults_and_validation(db):
    settings = core.get_settings(db)
    assert settings["annual_price_cents"] == 5400
    assert settings["creator_rate_bps"] == 5000
    assert settings["fan_rate_bps"] == 300
    assert settings["currency"] == "USD"
    assert settings["program_enabled"] is True
    core.update_settings(db, actor_id="owner", currency="cad")
    assert core.get_settings(db)["currency"] == "CAD"
    assert_error("invalid_input", core.update_settings, db, actor_id="owner", fan_rate_bps=10001)
    assert_error("invalid_input", core.update_settings, db, actor_id="owner", missing_setting=10)


def test_account_auth_crud_and_secret_exclusion(db, affiliate):
    assert core.authenticate_affiliate(db, "PARTNER-A", "test-password-for-affiliates")["id"] == affiliate["id"]
    assert core.authenticate_affiliate(db, "partner-a", "wrong-password") is None
    assert "password_hash" not in affiliate
    assert "password_hash" not in core.get_affiliate(db, affiliate["id"])
    assert "password_hash" not in core.list_affiliates(db)[0]
    renamed = core.update_affiliate(db, affiliate["id"], actor_id="owner", display_name="Partner A")
    assert renamed["display_name"] == "Partner A"
    core.disable_affiliate(db, affiliate["id"], actor_id="owner")
    assert core.authenticate_affiliate(db, "partner-a", "test-password-for-affiliates") is None
    assert len(core.list_affiliates(db)) == 1
    assert PASSWORD_HASH not in str(core.list_audit(db))


def test_mutations_never_commit_and_failed_operation_is_atomic(db):
    created = core.create_affiliate(db, username="temporary", password_hash=PASSWORD_HASH)
    assert db.in_transaction
    db.rollback()
    assert core.list_affiliates(db) == []
    assert core.list_audit(db) == []
    affiliate = core.create_affiliate(db, username="a", password_hash=PASSWORD_HASH)
    subscription_referral(db, affiliate)
    db.commit()
    assert_error("fees_exceed_payment", subscription_sale, db, platform_fee_bps=9900)
    assert core.get_subscription_referral(db, "sub-a")["status"] == "pending"
    assert core.list_sales(db) == []
    db.rollback()
    assert core.get_affiliate(db, affiliate["id"])


def test_schema_setup_refuses_to_commit_host_transaction(db):
    db.execute("CREATE TABLE host_example(value INTEGER)")
    db.execute("INSERT INTO host_example VALUES (7)")
    assert_error("transaction_active", core.init_schema, db)
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM host_example").fetchone()[0] == 0


def test_exact_two_demo_accounts_idempotent_and_initially_zero(db):
    seeded = core.seed_demo_affiliates(db, password_hash=PASSWORD_HASH)
    again = core.seed_demo_affiliates(db, password_hash=PASSWORD_HASH)
    assert [a["id"] for a in seeded] == [a["id"] for a in again]
    assert {a["username"] for a in seeded} == {"affiliate1", "affiliate2"}
    assert len(core.list_affiliates(db)) == 2
    assert all(core.get_balances(db,a["id"])["earned_cents"] == 0 for a in seeded)
    assert_error("demo_payout_disabled", core.request_payout, db, seeded[0]["id"], 1,
        currency="USD", actor_id="owner")


def test_first_account_referral_wins_and_free_signup_has_no_money(db, affiliate, affiliate_b):
    first = account_referral(db, affiliate)
    second = account_referral(db, affiliate_b)
    assert first["created"] is True
    assert second["created"] is False
    assert second["affiliate_id"] == affiliate["id"]
    dashboard = core.get_dashboard(db, affiliate_id=affiliate["id"])
    assert dashboard["signups"] == 1
    assert dashboard["commission_cents"] == 0
    assert core.list_sales(db) == []


def test_platform_referral_does_not_claim_unrelated_creator_subscriptions(db, affiliate):
    core.attribute_account(db, "fan-a", affiliate["code"])
    opted_in(db)
    sale = subscription_sale(db)
    assert sale["affiliate_id"] is None
    assert sale["commission_cents"] == 0
    assert sale["creator_net_cents"] == 1820


def test_self_referral_guards_use_real_creator_owner(db, affiliate):
    assert_error("self_referral", core.attribute_account, db, "affiliate-owner-a", affiliate["code"])
    opted_in(db, creator="different-profile-id", owner="affiliate-owner-a")
    assert_error("self_referral", core.attribute_subscription, db, "sub-self", "fan-a",
        "different-profile-id", affiliate["code"])
    opted_in(db)
    assert_error("self_referral", core.attribute_subscription, db, "sub-self-fan", "affiliate-owner-a",
        "creator-a", affiliate["code"])
    assert_error("self_referral", subscription_sale, db, user_id="creator-owner-a")


def test_creator_default_optout_cannot_be_bypassed_and_consent_is_audited(db, affiliate):
    assert core.get_creator_preference(db,"new-creator")["allowed"] is False
    assert_error("creator_opted_out", core.attribute_subscription, db, "sub-a", "fan-a", "new-creator", affiliate["code"])
    assert_error("creator_opted_out", core.record_click, db, affiliate["code"], creator_id="new-creator")
    opted_in(db)
    core.set_creator_opt_in(db,"creator-a",False,actor_id="creator-owner-a")
    consents = [r for r in core.list_audit(db) if r["action"] == "creator.consent_changed"]
    assert len(consents) == 2
    assert consents[0]["details"]["previous_allowed"] is True


def test_membership_recurring_27_dollars_and_idempotent_different_events(db, affiliate):
    account_referral(db, affiliate)
    sale = membership_sale(db,event_id="event-one")
    repeated = membership_sale(db,event_id="event-retry-different-id")
    assert sale["commission_cents"] == 2700
    assert sale["platform_net_cents"] == 2700
    assert sale["creator_deduction_cents"] == 0
    assert repeated["created"] is False
    assert repeated["period_end"] == sale["period_end"]
    assert len(core.list_commissions(db)) == 1
    renewal = membership_sale(db,payment="membership-2")
    assert renewal["period_start"] == sale["period_end"]
    assert renewal["period_end"].startswith("2028-")
    assert core.get_balances(db,affiliate["id"])["earned_cents"] == 5400
    assert_error("currency_locked",core.update_settings,db,actor_id="owner",currency="CAD")


def test_duplicate_payment_conflicting_amount_identity_or_period_rejected(db, affiliate):
    account_referral(db,affiliate)
    membership_sale(db)
    assert_error("idempotency_conflict",membership_sale,db,amount_cents=5500)
    assert_error("idempotency_conflict",membership_sale,db,user_id="another-user")
    assert_error("idempotency_conflict",membership_sale,db,period_end="2030-11-07T12:00:00Z")
    assert len(core.list_sales(db)) == 1


def test_payment_replay_after_settings_change_uses_original_record(db, affiliate):
    account_referral(db,affiliate)
    sale = membership_sale(db)
    core.update_settings(db,actor_id="owner",annual_price_cents=6000,creator_rate_bps=2500)
    assert membership_sale(db)["created"] is False
    renewed = membership_sale(db,payment="membership-2",amount_cents=6000)
    assert renewed["commission_rate_bps"] == sale["commission_rate_bps"] == 5000
    assert renewed["commission_cents"] == 3000


def test_subscription_split_first_win_and_rate_snapshot(db,affiliate,affiliate_b):
    captured = subscription_referral(db,affiliate)
    assert captured["status"] == "pending"
    again = core.attribute_subscription(db,"sub-a","fan-a","creator-a",affiliate_b["code"])
    assert again["affiliate_id"] == affiliate["id"]
    sale = subscription_sale(db)
    assert sale["gross_cents"] == 2000
    assert sale["affiliate_cents"] == sale["creator_deduction_cents"] == 60
    assert sale["platform_fee_cents"] == sale["platform_net_cents"] == 180
    assert sale["creator_net_cents"] == 1760
    assert sale["affiliate_cents"]+sale["platform_net_cents"]+sale["creator_net_cents"] == 2000
    assert core.get_subscription_referral(db,"sub-a")["status"] == "locked"
    core.update_settings(db,actor_id="owner",fan_rate_bps=500)
    renewal = subscription_sale(db,payment="renewal-1",platform_fee_bps=1200)
    assert renewal["commission_cents"] == 60
    assert renewal["platform_fee_cents"] == 240
    assert renewal["creator_net_cents"] == 1700


def test_optout_before_first_payment_prevents_entitlement_forever_on_that_subscription(db,affiliate):
    subscription_referral(db,affiliate)
    core.set_creator_opt_in(db,"creator-a",False,actor_id="creator-owner-a")
    sale = subscription_sale(db)
    assert sale["commission_cents"] == 0
    assert sale["creator_net_cents"] == 1820
    assert core.get_subscription_referral(db,"sub-a")["status"] == "ineligible"
    opted_in(db)
    assert subscription_sale(db,payment="renewal-1")["commission_cents"] == 0


def test_optout_after_success_keeps_recurring_commission_and_blocks_new_refs(db,affiliate):
    subscription_referral(db,affiliate)
    subscription_sale(db)
    core.set_creator_opt_in(db,"creator-a",False,actor_id="creator-owner-a")
    assert subscription_sale(db,payment="renewal-1")["commission_cents"] == 60
    assert_error("creator_opted_out",core.attribute_subscription,db,"sub-b","fan-b","creator-a",affiliate["code"])


def test_attribution_cannot_be_retroactively_added_to_paid_subscription_or_membership(db,affiliate):
    opted_in(db)
    subscription_sale(db)
    assert_error("already_paid",core.attribute_subscription,db,"sub-a","fan-a","creator-a",affiliate["code"])
    membership_sale(db)
    assert_error("already_paid",account_referral,db,affiliate)


def test_subscription_attribution_identity_mismatch_rejected(db,affiliate):
    subscription_referral(db,affiliate)
    assert_error("identity_mismatch",core.attribute_subscription,db,"sub-a","fan-b","creator-a",affiliate["code"])
    assert_error("identity_mismatch",subscription_sale,db,user_id="fan-b")
    assert core.list_sales(db) == []


@pytest.mark.parametrize("amount",[True,False,0,-1,54.00,"5400",None,10**13])
def test_money_requires_positive_bounded_integer_cents(db,amount):
    assert_error("invalid_input",membership_sale,db,amount_cents=amount)


def test_provider_amount_currency_and_kind_validation(db):
    assert_error("amount_mismatch",membership_sale,db,amount_cents=5399)
    assert_error("currency_mismatch",membership_sale,db,currency="CAD")
    assert_error("invalid_input",membership_sale,db,currency="USDT")
    assert_error("invalid_kind",membership_sale,db,kind="tip")
    assert_error("invalid_period",membership_sale,db,period_start="2026-01-01",period_end="2028-01-01")
    assert core.list_sales(db) == []


def test_demo_live_financial_modes_cannot_mix(db):
    affiliate = core.seed_demo_affiliates(db,password_hash=PASSWORD_HASH)[0]
    account_referral(db,affiliate)
    assert_error("mode_mismatch",membership_sale,db)
    assert core.list_sales(db) == []
    sale = membership_sale(db,is_demo=True)
    assert sale["commission_cents"] == 2700
    assert core.creator_entitlement(db,"creator-a")["active"] is False
    assert core.creator_entitlement(db,"creator-a",is_demo=True)["active"] is True
    assert_error("demo_payout_disabled",core.request_payout,db,affiliate["id"],2700,currency="USD",actor_id="owner")


def test_suspension_blocks_new_referrals_and_payouts_preserves_prior_earnings(db,affiliate):
    subscription_referral(db,affiliate)
    subscription_sale(db)
    core.update_affiliate(db,affiliate["id"],actor_id="owner",status="suspended")
    assert_error("affiliate_inactive",account_referral,db,affiliate)
    assert_error("affiliate_inactive",core.request_payout,db,affiliate["id"],1,currency="USD",actor_id="owner")
    assert subscription_sale(db,payment="renewal-1")["commission_cents"] == 60
    balance = core.get_balances(db,affiliate["id"])
    assert balance["earned_cents"] == balance["held_cents"] == 120
    assert balance["available_cents"] == 0
    core.update_affiliate(db,affiliate["id"],actor_id="owner",status="active")
    assert core.get_balances(db,affiliate["id"])["available_cents"] == 120


def test_suspension_before_first_payment_makes_pending_referral_ineligible(db,affiliate):
    subscription_referral(db,affiliate)
    core.update_affiliate(db,affiliate["id"],actor_id="owner",status="suspended")
    assert subscription_sale(db)["commission_cents"] == 0
    assert core.get_subscription_referral(db,"sub-a")["ineligible_reason"] == "affiliate_inactive"


def test_partial_refunds_use_cumulative_rounding_and_full_refund_reverses_exactly(db,affiliate):
    subscription_referral(db,affiliate)
    sale = subscription_sale(db,amount_cents=101,platform_fee_bps=900)
    assert sale["commission_cents"] == 3
    amounts = [1,17,17,17,17,17,15]
    reversals = [refund(db,"payment-1",f"refund-{i}",amount) for i,amount in enumerate(amounts)]
    assert sum(r["commission_reversed_cents"] for r in reversals) == 3
    assert sum(r["creator_deduction_reversed_cents"] for r in reversals) == 3
    assert sum(r["platform_fee_reversed_cents"] for r in reversals) == sale["platform_fee_cents"]
    assert core.get_balances(db,affiliate["id"])["net_commission_cents"] == 0
    assert_error("refund_exceeds_payment",refund,db,"payment-1","extra",1)
    assert refund(db,"payment-1","refund-0",1)["created"] is False
    assert_error("idempotency_conflict",refund,db,"payment-1","refund-0",2)


def test_membership_cancellation_expiry_leap_year_and_full_refund(db,affiliate):
    account_referral(db,affiliate)
    sale = membership_sale(db,period_start="2024-02-29T10:00:00Z")
    assert sale["period_end"] == "2025-02-28T10:00:00Z"
    assert core.creator_entitlement(db,"creator-a",at="2025-02-28T09:59:59Z")["active"]
    assert not core.creator_entitlement(db,"creator-a",at="2025-02-28T10:00:00Z")["active"]
    sale = membership_sale(db,payment="current",period_start=NOW)
    core.cancel_membership(db,"creator-a",actor_id="provider")
    state = core.creator_entitlement(db,"creator-a")
    assert state["active"] is True
    assert state["cancel_at_period_end"] is True
    assert state["status"] == "canceling"
    refund(db,"current","partial",2700)
    assert core.creator_entitlement(db,"creator-a")["active"]
    refund(db,"current","remaining",2700)
    assert not core.creator_entitlement(db,"creator-a")["active"]


def test_membership_payment_hold_blocks_current_not_newer_paid_term(db,affiliate):
    account_referral(db,affiliate)
    membership_sale(db,period_start="2025-11-07T12:00:00Z",period_end="2026-11-07T12:00:00Z")
    membership_sale(db,payment="new-term",period_start="2026-11-07T12:00:00Z",period_end="2027-11-07T12:00:00Z")
    core.hold_payment(db,"test-provider","membership-1",reason="Older invoice refund review",actor_id="provider")
    assert core.creator_entitlement(db,"creator-a")["active"] is True
    core.hold_payment(db,"test-provider","new-term",reason="Current invoice dispute",actor_id="provider")
    entitlement = core.creator_entitlement(db,"creator-a")
    assert entitlement["status"] == "payment_review"
    assert entitlement["active"] is False
    core.release_payment_hold(db,"test-provider","new-term",reason="Provider settled dispute",actor_id="owner")
    assert core.creator_entitlement(db,"creator-a")["active"] is True


def test_new_membership_after_full_refund_starts_now_not_after_refunded_term(db,affiliate):
    account_referral(db,affiliate)
    original = membership_sale(db)
    refund(db,"membership-1","full",5400)
    fresh = membership_sale(db,payment="repurchase")
    assert fresh["period_start"] == original["period_start"]
    assert core.creator_entitlement(db,"creator-a")["active"] is True


def test_payout_reservation_confirmed_reference_and_double_spend_guards(db,affiliate):
    account_referral(db,affiliate)
    membership_sale(db)
    payout = core.request_payout(db,affiliate["id"],2700,currency="USD",actor_id="owner")
    assert payout["status"] == "pending"
    assert payout["provider_reference"] is None
    assert core.get_balances(db,affiliate["id"])["available_cents"] == 0
    assert_error("insufficient_balance",core.request_payout,db,affiliate["id"],1,currency="USD",actor_id="owner")
    assert_error("invalid_input",core.confirm_payout,db,payout["id"],provider_reference="",actor_id="provider")
    assert core.get_payout(db,payout["id"])["status"] == "pending"
    core.confirm_payout(db,payout["id"],provider_reference="test-provider:transfer-1",actor_id="verified-provider")
    repeated = core.confirm_payout(db,payout["id"],provider_reference="test-provider:transfer-1",actor_id="verified-provider")
    assert repeated["created"] is False
    assert core.get_balances(db,affiliate["id"])["paid_cents"] == 2700
    assert_error("idempotency_conflict",core.confirm_payout,db,payout["id"],provider_reference="different",actor_id="provider")
    assert_error("payout_already_paid",core.fail_payout,db,payout["id"],reason="Retry",actor_id="owner")


def test_provider_transfer_reference_cannot_pay_two_requests(db,affiliate):
    account_referral(db,affiliate)
    membership_sale(db)
    first = core.request_payout(db,affiliate["id"],1000,currency="USD",actor_id="owner")
    second = core.request_payout(db,affiliate["id"],1000,currency="USD",actor_id="owner")
    core.confirm_payout(db,first["id"],provider_reference="transfer-1",actor_id="provider")
    assert_error("idempotency_conflict",core.confirm_payout,db,second["id"],provider_reference="transfer-1",actor_id="provider")
    assert core.get_payout(db,second["id"])["status"] == "pending"
    assert core.get_balances(db,affiliate["id"])["paid_cents"] == 1000


def test_refund_after_completed_payout_records_negative_balance_and_preserves_history(db,affiliate):
    account_referral(db,affiliate)
    membership_sale(db)
    payout = core.request_payout(db,affiliate["id"],2700,currency="USD",actor_id="owner")
    core.confirm_payout(db,payout["id"],provider_reference="transfer-1",actor_id="provider")
    refund(db,"membership-1","chargeback-1",5400)
    balance = core.get_balances(db,affiliate["id"])
    assert balance["paid_cents"] == 2700
    assert balance["negative_cents"] == 2700
    assert balance["balance_cents"] == -2700
    assert balance["available_cents"] == 0
    assert core.get_payout(db,payout["id"])["status"] == "paid"
    assert len(core.list_commissions(db)) == 2


def test_review_holds_commission_and_pending_payout_until_explicit_reconciliation(db,affiliate):
    account_referral(db,affiliate)
    membership_sale(db)
    payout = core.request_payout(db,affiliate["id"],2700,currency="USD",actor_id="owner")
    core.hold_payment(db,"test-provider","membership-1",reason="Unknown partial refund",actor_id="provider")
    balance = core.get_balances(db,affiliate["id"])
    assert balance["held_cents"] == 2700
    assert balance["available_cents"] == 0
    assert core.get_payout(db,payout["id"])["status"] == "held"
    assert core.list_payouts(db,status="pending") == []
    assert_error("payout_not_pending",core.confirm_payout,db,payout["id"],provider_reference="transfer-1",actor_id="provider")
    refund(db,"membership-1","partial",2700)
    assert core.get_balances(db,affiliate["id"])["held_cents"] == 1350
    core.release_payment_hold(db,"test-provider","membership-1",reason="Refund reconciled",actor_id="owner")
    core.fail_payout(db,payout["id"],reason="Replace after reconciliation; no transfer sent",actor_id="owner")
    assert core.get_balances(db,affiliate["id"])["available_cents"] == 1350


def test_refund_after_payout_request_prevents_oversized_confirmation(db,affiliate):
    account_referral(db,affiliate)
    membership_sale(db)
    payout = core.request_payout(db,affiliate["id"],2700,currency="USD",actor_id="owner")
    refund(db,"membership-1","partial",2700)
    assert_error("insufficient_balance",core.confirm_payout,db,payout["id"],provider_reference="transfer-1",actor_id="provider")
    core.fail_payout(db,payout["id"],reason="Cancel unsent oversized payout",actor_id="owner")
    assert core.get_balances(db,affiliate["id"])["available_cents"] == 1350


def test_audit_and_dashboard_are_scoped_and_clicks_retain_no_identifiers(db,affiliate,affiliate_b):
    core.record_click(db,affiliate["code"])
    core.record_click(db,affiliate["code"])
    core.record_click(db,affiliate_b["code"])
    account_referral(db,affiliate)
    membership_sale(db)
    refund(db,"membership-1","partial",1000)
    own = core.get_dashboard(db,affiliate_id=affiliate["id"])
    other = core.get_dashboard(db,affiliate_id=affiliate_b["id"])
    assert own["clicks"] == 2
    assert own["sales"] == own["signups"] == 1
    assert own["commission_cents"] == 2200
    assert own["refunded_cents"] == 1000
    assert other["clicks"] == 1
    assert other["sales"] == other["signups"] == other["commission_cents"] == 0
    assert other["recent_sales"] == other["recent_commissions"] == []
    assert core.get_dashboard(db)["affiliates"] == 2
    columns = {r["name"] for r in db.execute("PRAGMA table_info(aff_clicks)")}
    assert columns == {"id","affiliate_id","creator_id","created_at"}
    assert core.list_commissions(db)[0]["state"] == "reversed"


def test_ledger_sales_and_refunds_are_append_only(db,affiliate):
    account_referral(db,affiliate)
    membership_sale(db)
    refund(db,"membership-1","partial",100)
    for table in ("aff_commission_ledger","aff_sales","aff_refunds"):
        with pytest.raises(sqlite3.IntegrityError,match="append-only"):
            db.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError,match="append-only"):
            db.execute(f"UPDATE {table} SET id=id")
    assert len(core.list_sales(db)) == 1


def test_tiny_payments_cannot_create_negative_creator_balance(db,affiliate):
    subscription_referral(db,affiliate)
    sale = subscription_sale(db,amount_cents=1)
    assert sale["commission_cents"] == 0
    assert sale["platform_fee_cents"] == 0
    assert sale["creator_net_cents"] == 1


def test_program_pause_stops_new_attribution_and_preserves_paid_contracts(db,affiliate):
    subscription_referral(db,affiliate)
    subscription_sale(db)
    core.update_settings(db,actor_id="owner",program_enabled=False)
    assert_error("program_paused",core.record_click,db,affiliate["code"])
    assert_error("program_paused",account_referral,db,affiliate)
    assert subscription_sale(db,payment="renewal")["commission_cents"] == 60
