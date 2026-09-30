# Final launch checklist

- Upload extracted source with app.py at the GitHub repository root.
- Deploy render.yaml as a Blueprint; confirm the disk is attached at /var/data.
- Configure ADMIN_PASSWORD, SECRET_KEY, APP_STORAGE_DIR and PUBLIC_BASE_URL.
- Confirm /health reports ok and owner login works.
- Keep BILLING_MODE=live and SEED_DEMO_DATA=false.
- Confirm your chosen payment provider permits the actual business; configure its credentials and signed webhook.
- Complete your own provider test purchase, failed-payment test, repeat callback test and refund test before live sales.
- Create real creator/buyer accounts and publish real listings. Sample creators are not real accounts.
- Configure TURN and test a call between separate networks if using live rooms.
- Publish your actual business policies and establish creator verification/moderation/support processes.
- Back up the database and uploads; keep the backup outside GitHub and the service.

Read START_HERE.md for exact settings, commands, limitations and migration instructions.
