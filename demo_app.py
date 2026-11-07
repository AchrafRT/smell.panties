"""Run the isolated affiliate demo; never import the production payment modules."""
import os
from pathlib import Path
import secrets

from flask import Flask, redirect, render_template_string, request
from affiliate_program import install


def create_app(db_path=None, *, testing=False):
    app = Flask(__name__)
    directory = Path(os.getenv("AFFILIATE_DEMO_DATA_DIR", "./demo-data"))
    if testing:
        session_key = secrets.token_hex(32)
    else:
        directory.mkdir(parents=True, exist_ok=True)
        secret_path = directory / ".session-key"
        if not secret_path.exists():
            secret_path.write_text(secrets.token_hex(32), encoding="utf-8")
            secret_path.chmod(0o600)
        session_key = secret_path.read_text().strip()
    app.config.update(SECRET_KEY=session_key, TESTING=testing,
                      SESSION_COOKIE_NAME="sp_aff_demo_session",
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower()=="true",
                      MAX_CONTENT_LENGTH=64*1024,
                      AFFILIATE_PUBLIC_BASE_URL=os.getenv("AFFILIATE_DEMO_BASE_URL", "http://localhost:5010"))
    install(app, demo=True, db_path=db_path or str(directory / "affiliates-demo.sqlite3"))

    @app.route("/")
    def home():
        return render_template_string('''<!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1"><title>Affiliate program — Smell Panties</title>
        <link rel="stylesheet" href="/affiliate/assets/program.css"></head>
        <body><main class="demo-home" style="max-width:1040px;margin:8vh auto;padding:32px">
        <p class="eyebrow">SMELL PANTIES · PARTNER PROGRAM</p><h1>Make an introduction.<br>Build recurring earnings.</h1>
        <p>Invite creators to a $54 annual membership and earn 50% of each eligible paid renewal.
        Promote participating creators and earn 3% of their referred subscription payments.</p>
        <p><a class="button btn" href="/affiliate">Affiliate portal →</a> <a class="button btn" href="/admin">Owner dashboard →</a></p>
        <p>Development demo. Fictional accounts, no real charges or payouts. All displayed amounts are USD.</p>
        </main></body></html>''')

    @app.route("/demo/referral")
    def referral_preview():
        rt = app.extensions["affiliate_program"]
        code = rt.referral()
        creator = rt.creator(request.args.get("creator_id"))
        return render_template_string('''<!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1"><title>Referral captured</title>
        <link rel="stylesheet" href="/affiliate/assets/program.css"></head><body>
        <main style="max-width:720px;margin:10vh auto;padding:30px"><h1>Referral link recorded.</h1>
        <p>This signed referral will be remembered for the configured attribution window.</p>
        <p>Destination: {{ creator.display_name if creator else 'Smell Panties' }}.</p>
        <p>No signup or payment was fabricated. Use the owner simulation to test a paid conversion.</p>
        <p><a class="btn" href="/admin#simulation">Open owner simulation</a></p></main></body></html>''', creator=creator)

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.getenv("PORT", "5010")), debug=False)
