# Affiliate program implementation

## Status and source baseline

This is a local implementation and deployment handover, prepared on 7 November 2026. The affiliate extension, isolated demo and host integration tests are available for review. **The changes are not confirmed live on smellpanties.com.** SSH access to the known server was unreachable during this work, so no production deployment or real funds transfer was performed.

The starting repository was `main` at **`2ae9d01ba6792be4b4fc20328accfa295160b838`**, dated 30 September 2026. The known server deployment includes newer changes from 27 October. Treat this checkout as a source of changes to merge against the current server, not as a replacement deployment. The inherited README describes an older, temporary Render preview; its storage and deployment settings are not instructions for replacing the live service.

## Commercial rules implemented

| Item | Implemented rule |
|---|---|
| Creator membership | USD 54 per annual access period, required to publish and sell |
| Referred creator membership | 50%, or USD 27, credited for each eligible verified USD 54 payment |
| Referred fan subscription | 3% of each eligible verified subscription payment, deducted once from creator proceeds |
| Free platform signup | Attribution is recorded; the cash reward is USD 0 |
| Other purchases | Tips, products, individual content and paid messages do not earn affiliate commission |
| Affiliate recruitment | No downline, multi-level commission or reward for recruiting another affiliate |

A verified creator membership payment grants 12 months of access. Further eligible payments extend access and can earn another commission. **Automatic annual charging is not connected.** “Recurring commission” describes eligibility on later verified payments, not a working automatic debit instruction.

For a USD 20 fan subscription with the existing 9% platform fee:

| Allocation | Amount |
|---|---:|
| Affiliate, 3% | USD 0.60 |
| Platform, 9% | USD 1.80 |
| Creator remainder | USD 17.60 |
| Total | USD 20.00 |

The affiliate amount is deducted once. It is not also charged to the fan or deducted a second time from the creator. This example excludes additional processor fees and taxes; the host adapter snapshots the creator's actual platform fee when creating the invoice.

## Attribution and creator consent

Referral links use `/r/<code>` for the platform and `/r/<code>/<creator-slug>` for a creator. Signed cookies use a **30-day default attribution window**, configurable by the owner. First valid attribution wins. Later clicks do not replace a still-valid first referral or extend its original timestamp.

Platform signup attribution and creator-specific subscription attribution are separate. Referring a fan to the platform does not automatically claim all of that fan's unrelated creator subscriptions. New creators default to opted out; only participating creators appear in the affiliate directory.

The first eligible paid subscription locks its affiliate attribution and rate. If a creator opts out before that first payment, the pending referral becomes ineligible. Opting out after a successful referred payment stops new referrals while preserving commission eligibility on later payments for that existing subscription. Program pauses and affiliate suspension likewise preserve already-earned history and eligible paid relationships while stopping new attribution. Suspended affiliates' unpaid balances are held. Identity checks reject known self-referrals using the configured linked user identity; they do not infer identity from IP addresses. Attribution cannot be added retroactively to an already-paid purchase.

The inherited host reuses the same fan–creator subscription ID after cancellation or expiry. Attribution therefore survives a later reactivation of that ID. A different commercial rule would require separate enrollment generations. There is one settlement currency, initially USD; supported admin changes are limited to USD, CAD, EUR or GBP and are locked once checkout terms, pending invoices or financial activity exist. No currency conversion is performed.

## Components and routes

The extension is installed through `affiliate_program.install(...)`:

- [`affiliate_program/core.py`](affiliate_program/core.py) owns integer-cent accounting, consent, attribution, annual access periods, reversals, balances and audit records in additive `aff_*` tables.
- [`affiliate_program/runtime.py`](affiliate_program/runtime.py) connects host identities, signed referrals, invoice snapshots and verified settlement to that core.
- [`affiliate_program/web.py`](affiliate_program/web.py), its templates and assets provide the affiliate, owner and creator workspaces.
- [`app.py`](app.py) and [`billing.py`](billing.py) capture signup/subscription attribution, enforce membership and settle invoices and commission records within the same transaction.

`/affiliate` is the protected partner dashboard; `/admin` is the company workspace. The existing owner dashboard remains available at `/admin/legacy`. `/creator/membership` exposes membership status and promotion consent. The owner can manage affiliate accounts, review creators, inspect the ledger and audit log, and update program settings. Homepage and creator-dashboard links lead into these pages.

The default affiliate hostname is `affiliates.smellpanties.com`; its root redirects to `/affiliate` when requests reach the application. This does not create DNS or TLS configuration. DNS records, Caddy routing and certificate provisioning still require deployment access.

## Running the isolated demo

From the directory containing `demo_app.py` and `affiliate_program/`, use Python with Flask installed:

```bash
python -m pip install Flask
python demo_app.py
```

The default address is `http://localhost:5010`. `PORT` changes the port. `AFFILIATE_DEMO_DATA_DIR` selects the demo storage directory; its default is `./demo-data`. `AFFILIATE_DEMO_BASE_URL` controls the origin displayed in demo referral links.

| Demo access | Username | Password |
|---|---|---|
| Owner | `root` | `admin` |
| Affiliate 1 | `affiliate1` | `admin` |
| Affiliate 2 | `affiliate2` | `admin` |

These credentials apply only to the isolated demo. It seeds exactly two affiliates with zero balances and fictional creator records. Its SQLite database and session key are separate from production. It does not load the host payment adapter or collect payments. Owner simulation controls exercise payment accounting, renewal records, consent changes and partial/full refunds without moving money.

Static browser-QA snapshots, when supplied separately, are read-only renderings. They have disabled form submissions and are not the authenticated Flask demo.

## Production access and payment readiness

Production accepts the existing authenticated owner session. The additional `root` login uses `AFFILIATE_ADMIN_PASSWORD`, falling back to the host's existing `ADMIN_PASSWORD`. A configured password must contain at least 12 characters; the demo password is not a production fallback. Production affiliate creation also requires a password of at least 12 characters. Password hashing, CSRF protection, login throttling and account-scoped access are implemented.

Set a stable application `SECRET_KEY` and the canonical HTTPS `PUBLIC_BASE_URL` through deployment configuration. Membership enforcement defaults to enabled through `AFFILIATE_REQUIRE_MEMBERSHIP=true`. Existing creators without a verified membership will need an explicit rollout decision before this is enabled on their live accounts.

The payment ledger works, including duplicate-payment protection, immutable invoice terms, proportional refund reversals and payout reservation/confirmation bookkeeping. None of those records proves that a bank transfer occurred.

Membership checkout can use the existing configured live payment adapter. **Live automatic recurring billing, affiliate bank-transfer execution and an adapter reconciling exact provider refund amounts are not connected.** They are separate integrations. The owner status panel and protected `/affiliate/health` endpoint distinguish checkout from automatic renewals and transfers.

A provider refund/dispute review event places the affected sale and commission into review/hold and blocks unsafe payout confirmation. It does not invent a full or partial refund amount. Exact reversals require verified reconciliation through the core refund interface; releasing a hold requires explicit reconciliation as well.

## Verification and safe deployment

The final verification record is **80 passing tests plus 21 passing subtests**: 42 accounting tests, 13 portal tests and 25 host integration/contract cases. Coverage includes authentication and full host logout, attribution expiry/tampering, consent timing, USD 27 and USD 0.60 commissions, refunds, duplicate settlement, transaction rollback, membership expiry, ownership and direct-route publishing/purchase restrictions. Paid-but-suspended/deleted creators remain unable to publish or collect payments while retaining access to account billing controls. Tests use isolated storage and simulated provider results; they are not evidence of live checkout or bank transfers. Python compilation, JavaScript syntax and Git whitespace checks passed. Desktop and mobile portal layouts were visually reviewed from actual Flask renderings.

Run the suite in the assembled checkout:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_affiliate_core.py tests/test_affiliate_web.py tests/test_affiliate_host.py tests/test_affiliate_runtime.py -q
```

If the inherited flat checkout needs its original asset/template layout restored for the full host, run `python tools/prepare_local_layout.py`. It copies only missing known templates/assets and reports differing existing files without overwriting them. It is a local preparation aid, not a deployment script. The isolated `demo_app.py` does not need this helper.

For deployment:

1. Obtain the current server code and configuration, and back up its database, uploads and application files. Verify the backup can be restored.
2. Merge the extension and specific host/template changes against that current code. Preserve the October changes, existing secrets and persistent storage. Never overwrite the entire application with the September baseline.
3. Test the merged result against an isolated copy with external charging disabled. Confirm membership rollout behavior, invoice settlement and rollback before affecting current creators.
4. Deploy only reviewed source and required assets. Do not copy demo databases, demo session keys, test storage or static QA snapshots into production data directories. Create production affiliate accounts separately.
5. Configure DNS/Caddy and HTTPS, verify protected routes and health reporting, then enable only the payment capabilities actually connected. Retain the previous release and backup for rollback.

Until those steps and the outstanding provider integrations are completed, describe this work as an implemented extension with a working isolated accounting demo, not a live automatic payout service.
