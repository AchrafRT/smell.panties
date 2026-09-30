# Smell Panties — Render Free, no disk

This ZIP preserves your original design and runs as a free Render Web Service without attaching a disk. It is a temporary preview, not a persistent customer store. Accounts, orders, messages and uploads are stored locally and are lost when Render restarts, redeploys or spins down the service. Real payments are disabled even if provider keys are accidentally supplied.

## Deploy with a Blueprint

1. Extract the ZIP and upload its contents to a GitHub repository. Keep `app.py`, `requirements.txt`, `render.yaml`, `templates`, `static` and `seed_data` at the repository root.
2. In Render, select **New → Blueprint**, connect the repository, and use `render.yaml`.
3. Set `ADMIN_PASSWORD` to your own password of at least 12 characters. Render generates `SECRET_KEY` automatically.
4. Deploy. **Do not add a disk.** The blueprint specifies the Free plan and temporary storage.
5. Visit `/health`, then `/login`. Owner email: **admin**. Password: the value you entered in Render.

## If creating a Web Service manually

| Setting | Value |
|---|---|
| Language | Python 3 |
| Plan | Free |
| Root directory | Leave empty |
| Build | `pip install -r requirements.txt` |
| Start | `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 app:app` |
| Health check | `/health` |
| `PYTHON_VERSION` | `3.12.14` |
| `APP_STORAGE_DIR` | `/tmp/smell-panties-preview` |
| `SECRET_KEY` | Generate at least 32 random characters |
| `ADMIN_PASSWORD` | Your password, at least 12 characters |
| `COOKIE_SECURE` | `true` |
| `FREE_RENDER_PREVIEW` | `true` |
| `BILLING_MODE` | `launch_free` |
| `SEED_DEMO_DATA` | `true` |
| `MAX_UPLOAD_MB` | `10` |

If reusing an existing service, remove any disk and change the old `/var/data` storage value to `/tmp/smell-panties-preview`. Do not reuse the paid edition's blueprint. Save any existing customer data before changing that existing service. Creating a separate free service leaves your existing one untouched.

## What works in this preview

The original storefront and logo, nine sample creators, buyer and creator signup, owner dashboard, creator publishing, messaging, free subscription access and the existing live-room interface are retained. Sample creators do not have passwords; the owner can assign them an account. New accounts and uploads are temporary. Provider checkout is unavailable. Product orders can be previewed and the owner can simulate fulfillment; no funds are collected. Do not upload private customer data or accept real orders in this build.

The visible preview banner explains the reset behavior. After a reset, the owner login still uses the Render environment password, sample creators are seeded again, and newly registered accounts must be recreated. Free subscriptions are for testing only.

Render free services sleep after 15 minutes without inbound traffic and may take about a minute to wake. Local data is lost on sleep/restart/redeploy. Free services have no persistent disk or interactive Shell. Render's free Postgres offering expires after 30 days, so it is not silently included as permanent storage. See [Render's free-tier documentation](https://render.com/docs/free).

To operate a persistent marketplace while keeping the Render web service free, the application needs an external database plus external file/object storage, with their own accounts and limits. This ZIP does not include that migration. The paid persistent edition remains separate.

## Local use and validation

Run `launch.bat` on Windows or `bash launch.sh` on macOS/Linux with Python 3.12 installed. Copy `.env.example` to `.env` for local settings. Never commit `.env` or database files.

The inherited 32 tests use mocked payment APIs outside preview mode. Separate free-mode checks verify startup, owner login, the preview banner, provider blocking and reset behavior. No live Render deployment or payment transaction was performed.
