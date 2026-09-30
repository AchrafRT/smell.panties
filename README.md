# Launch your original-design Smell Panties version

This release uses your uploaded application as its base. It retains your dark rose-gold design, logo, storefront, creator studios, buyer accounts, owner dashboard, messaging and live-room interface.

The source is packaged for GitHub and Render. It has been tested locally. It has **not** been deployed to your Render account, connected to your payment credentials, or verified with real transactions.

## 1. Put the code on GitHub

1. Extract the ZIP on your computer.
2. Create a private GitHub repository, or replace the code in your existing repository after saving its current version.
3. Upload the **contents** of the extracted folder. `app.py`, `billing.py`, `payments.py`, `requirements.txt`, `render.yaml`, `templates`, `static` and `seed_data` must be at the repository root. Upload extracted files, not the ZIP itself.
4. Do not upload `.env`, `.venv`, `platform.sqlite3`, `.session-key`, backups or user uploads. These are runtime/private data, not application source.

Your supplied database had 9 sample creators and no users, orders, subscriptions or messages. It is intentionally absent from the public-source package. The samples remain available through `SEED_DEMO_DATA=true` on a separate preview service. Production defaults to an empty directory of creators; new creators register themselves. The original files on your Desktop have not been changed.

## 2. Deploy on Render

Choose **New → Blueprint**, connect the GitHub repository, and use `render.yaml`. Set `ADMIN_PASSWORD` to a unique password with at least 12 characters. Render generates the session secret. The blueprint creates a paid web service and a persistent disk.

If configuring a Web Service manually, use:

| Setting | Value |
|---|---|
| Language | Python 3 |
| Root directory | Empty when app.py is at the repository root |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 app:app` |
| Health check | `/health` |
| Disk mount path | `/var/data` |
| Disk size | 1 GB to start; increase as uploads grow |
| `APP_STORAGE_DIR` | `/var/data` |
| `PYTHON_VERSION` | `3.12.14` |
| `SECRET_KEY` | Generate a random secret of at least 32 characters |
| `ADMIN_PASSWORD` | Your private owner password, at least 12 characters |
| `COOKIE_SECURE` | `true` |
| `BILLING_MODE` | `live` |
| `SEED_DEMO_DATA` | `false` |

**Selecting a paid plan does not attach a disk automatically when using the manual form.** Add the disk in the service's Disk settings or Advanced section. Without it, `/var/data` can cause the permission error from your earlier deployment. Render's ordinary filesystem is temporary; the persistent disk stores both the SQLite database and uploads. Keep one service instance and the supplied one-worker command. [Render disk documentation](https://render.com/docs/disks)

After deployment, open `https://YOUR-SERVICE.onrender.com/health`. It should report `status: ok`, `database: ok`, and `admin_configured: true`. Then log in at `/login` with email **admin** and your `ADMIN_PASSWORD`.

## 3. Configure eligible payment methods

Set `PUBLIC_BASE_URL` to the exact public HTTPS origin, with no path, for example `https://YOUR-SERVICE.onrender.com`. `RENDER_EXTERNAL_URL` is used as a fallback. When you add your domain, update this setting and provider webhook URLs.

Secrets belong in **Render → Environment**, never in GitHub or browser code. A payment method appears only when its required credentials are present. Presence of credentials is not proof of account approval.

| Method | Required environment values | Webhook URL |
|---|---|---|
| Stripe | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | `https://YOUR-DOMAIN/webhooks/stripe` |
| PayPal | `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID`, `PAYPAL_MODE` | `https://YOUR-DOMAIN/webhooks/paypal` |
| Heleket | `HELEKET_MERCHANT_ID`, `HELEKET_API_KEY` | `https://YOUR-DOMAIN/webhooks/heleket` |

**Provider eligibility matters for this particular business:** Stripe's published prohibited-business list includes adult content, fetish services and adult live-chat features. Do not enable Stripe for those transactions. PayPal also has restrictions on sexually oriented goods/services; confirm the exact permitted use with PayPal. Heleket merchant eligibility has not been verified for your account. The included adapters do not bypass provider rules or establish approval. Use only a provider that supports the actual catalog and business model. [Stripe restrictions](https://stripe.com/legal/restricted-businesses), [PayPal acceptable use](https://www.paypal.com/us/legalhub/acceptableuse-full).

### Stripe, for a permitted catalog

Use matching test or live API keys and webhook secrets. Subscribe to `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.expired`, `charge.refunded`, and `charge.dispute.created`. The app verifies signatures and retrieves the payment from Stripe before granting access.

### PayPal, for a permitted catalog

Start with `PAYPAL_MODE=sandbox` and sandbox application credentials. Register a webhook for `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.REFUNDED`, `PAYMENT.CAPTURE.REVERSED` and `CUSTOMER.DISPUTE.CREATED`. Save the webhook's ID in `PAYPAL_WEBHOOK_ID`. After approval, the buyer returns to checkout and presses **Complete approved PayPal payment**; a verified approval webhook can also trigger capture. To go live, switch to live credentials, a live webhook ID, and `PAYPAL_MODE=live` together.

### Heleket

Use your merchant ID and payment API key. The app supplies the callback URL when creating each invoice, validates callback signatures, and retrieves payment status independently. Partial or unconfirmed payments do not grant access. Select a merchant-supported currency; the storefront supports USD, CAD, EUR and GBP.

Provider references: [Stripe checkout](https://docs.stripe.com/api/checkout/sessions), [PayPal Orders API](https://developer.paypal.com/docs/api/orders/v2/), [Heleket invoice API](https://doc.heleket.com/methods/payments/creating-invoice).

## 4. Verify before inviting customers

Create one creator and one buyer account. Publish a test item and test one purchase with your provider's supported test environment. Verify the total and currency, callback delivery, paid status, download or shipping details, and access after returning to the site. Repeat a callback and confirm there is no duplicate fulfillment. Test a failed payment and a refund. Remove test listings before opening publicly.

Subscriptions are **one-time purchases of 30 days of access**. They expire automatically and require a new payment to renew. There are no automatic recurring charges. Paid offers and boutique orders also use hosted checkout. For physical items, buyers save a shipping address before paying; creators can see it on the paid order. Prices must include applicable shipping and taxes; this build does not calculate regional tax or shipping rates.

The owner can review payments at `/admin/payments`. A refund or dispute places a matched purchase into review and stops access/fulfillment; refunds themselves are performed in the provider dashboard. Creator payouts are also performed externally. This is not a Stripe Connect or automatic split-payment implementation.

## 5. Keep the service working

- Back up the database and uploads with `python manage.py backup /var/data/backups/backup-YYYY-MM-DD.zip`. Use a new name each time. Download backups securely and store them away from the service. Do not commit them. For a consistent media snapshot, pause uploads while making the backup. The database backup uses SQLite's backup API, which includes committed WAL data.
- Use `python manage.py check` in the service Shell to inspect configuration without printing secrets.
- Use `python manage.py reconcile-payments` to re-fetch pending/paid payments after missed callbacks. It does not create new charges or capture approvals. Unknown creation outcomes remain reserved until you reconcile them with the provider; never release a reservation merely because the browser closed.
- Change the owner's password through `ADMIN_PASSWORD` and redeploy. For a buyer or creator, `python manage.py reset-password user@example.com` prompts privately for a new password and revokes their existing sessions. Email-based password recovery is not integrated.
- Monitor disk usage. Media uploads can fill a 1 GB disk quickly. There is no object-storage integration in this build.
- For reliable calls across restrictive networks, configure a coturn-compatible service with `TURN_URL` and `TURN_SECRET`. The server generates temporary TURN credentials. Without TURN, some browser-to-browser calls will fail. No real camera call or external TURN service was tested in this release.
- Keep `BILLING_MODE=live`. `launch_free` is an explicit preview mode that grants free access; `manual` is for independently verified payments. The old `/webhooks/payment` placeholder is not used—configure the provider-specific URLs above.

The age gate is self-attestation, not identity verification. Creator verification, content consent/moderation operations, your actual terms/privacy/returns policies, transactional email, and merchant approval remain business setup tasks. This package does not certify those processes.

## Existing installations and local preview

For a real existing installation, back up first, replace source files only, and keep the existing `APP_STORAGE_DIR` and `SECRET_KEY`. The schema upgrade adds billing records and subscription expiry without deleting users or uploads. Old login sessions will be revoked by stronger session checks. Review old free/manual subscriptions before switching to live mode: legacy active rows with no expiry remain active until the owner cancels or updates them.

For a local preview, install Python 3.12, extract the ZIP, and run `launch.bat` on Windows or `bash launch.sh` on macOS/Linux. The launcher asks for a local owner password if one is not configured. Copy `.env.example` to `.env` to save local settings. To view sample creators, set `SEED_DEMO_DATA=true` **before the first run on a separate preview database**. Samples have no passwords until assigned by the owner. Never enable samples as real customer records.

Run local checks with `python -m unittest discover -s tests -v`. Tests use temporary data and mock payment APIs; they do not move money.
