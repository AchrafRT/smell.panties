import os
from pathlib import Path
from getpass import getpass
import app

if not app.ADMIN_PASSWORD:
    value=getpass('Choose a local owner password (at least 12 characters): ')
    if len(value)<12:raise SystemExit('Password must contain at least 12 characters.')
    app.ADMIN_PASSWORD=value
    print('For the next run, save ADMIN_PASSWORD in your local .env (never GitHub).')
print('Open http://127.0.0.1:5000 — owner login: admin')
app.app.run(host='127.0.0.1',port=5000,debug=False)
