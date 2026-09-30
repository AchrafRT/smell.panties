"""Owner maintenance tools. Run on the service owning the persistent disk."""
import argparse
import getpass
import json
from pathlib import Path
import sqlite3
import zipfile
import tempfile
from datetime import datetime,timezone
from werkzeug.security import generate_password_hash
import app
import billing
import payments


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    backup=sub.add_parser('backup');backup.add_argument('destination')
    reset=sub.add_parser('reset-password');reset.add_argument('email')
    sub.add_parser('check')
    sub.add_parser('reconcile-payments')
    recover=sub.add_parser('recover-checkout');recover.add_argument('invoice_id');recover.add_argument('external_id')
    args=parser.parse_args()
    with app.app.app_context():
        if args.command=='check':
            db=app.get_db()
            print(json.dumps({'database':db.execute('PRAGMA quick_check').fetchone()[0],
                              'admin_configured':len(app.ADMIN_PASSWORD)>=12,
                              'billing_mode':app.BILLING_MODE,
                              'providers_configured':payments.configured(),
                              'storage':str(app.STORAGE_ROOT)},indent=2))
        elif args.command=='reset-password':
            user=app.query_one('SELECT id FROM users WHERE email=? COLLATE NOCASE',(args.email,))
            if not user:raise SystemExit('No matching user. Owner password is changed in ADMIN_PASSWORD, not this command.')
            password=getpass.getpass('New password (at least 12 characters): ')
            if len(password)<12 or password!=getpass.getpass('Repeat new password: '):raise SystemExit('Passwords must match and contain at least 12 characters.')
            app.execute('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash(password),user['id']))
            print('Password reset. Existing sessions are revoked.')
        elif args.command=='backup':
            target=Path(args.destination).resolve()
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():raise SystemExit('Choose a new backup filename; existing backups are never overwritten.')
            with tempfile.TemporaryDirectory() as tmp:
                dbfile=Path(tmp)/'platform.sqlite3'
                dest=sqlite3.connect(dbfile)
                try:app.get_db().backup(dest)
                finally:dest.close()
                with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
                    z.write(dbfile,'platform.sqlite3')
                    for f in app.UPLOAD_ROOT.rglob('*'):
                        if f.is_file():z.write(f,'uploads/'+f.relative_to(app.UPLOAD_ROOT).as_posix())
                    z.writestr('BACKUP-INFO.txt','Private database/upload backup. Keep outside GitHub.\nCreated '+datetime.now(timezone.utc).isoformat())
            print('Private backup saved:',target)
        elif args.command=='reconcile-payments':
            rows=app.query_all("SELECT * FROM billing_invoices WHERE external_id IS NOT NULL AND status IN ('pending','paid')")
            for row in rows:
                try:
                    state,reference=payments.inspect(dict(row),dict(row),capture=False)
                    billing.apply_result(row['id'],state,reference)
                    print(row['id'],state)
                except payments.PaymentError:
                    print(row['id'],'provider unavailable; unchanged')
        elif args.command=='recover-checkout':
            row=app.query_one('SELECT * FROM billing_invoices WHERE id=?',(args.invoice_id,))
            if not row or not row['provider'] or row['external_id']:
                raise SystemExit('Use this only for an invoice with a selected provider and an unknown creation outcome.')
            candidate=dict(row);candidate['external_id']=args.external_id
            state,reference=payments.inspect(candidate,candidate,capture=False)
            app.execute('UPDATE billing_invoices SET external_id=? WHERE id=?',(args.external_id,row['id']))
            billing.apply_result(row['id'],state,reference)
            print('Verified provider invoice linked:',state)

if __name__=='__main__':main()
