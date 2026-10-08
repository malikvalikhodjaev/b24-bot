"""Copy and verify frozen SQLite state without printing participant data."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile

ROOT = Path(__file__).resolve().parents[1]
DATABASES = ('crm_bot.sqlite3', 'simulation.sqlite3', 'communications_test.sqlite3')


def digest(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        check = db.execute('PRAGMA quick_check').fetchone()[0]
        if check != 'ok':
            raise RuntimeError('Database integrity failed: ' + path.name)
        schema = list(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
        tables = {}
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [row[1] for row in db.execute('PRAGMA table_info(' + quoted + ')')]
            rows = sorted(json.dumps(list(row), ensure_ascii=False, separators=(',', ':'),
                         default=lambda value: {'bytes':base64.b64encode(value).decode('ascii')})
                          for row in db.execute('SELECT * FROM ' + quoted))
            tables[name] = {'columns':columns, 'rows':len(rows),
                            'sha256':hashlib.sha256('\n'.join(rows).encode('utf-8')).hexdigest()}
        return {'quick_check':check, 'schema_sha256':hashlib.sha256(
            json.dumps(schema, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest(),
            'tables':tables}


def create(output):
    output = output.resolve()
    if not output.is_relative_to(ROOT / 'data'):
        raise RuntimeError('Snapshot destination must be inside this bot data directory')
    with sqlite3.connect((ROOT/'data/crm_bot.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
        for table, column in (('operations','status'), ('fom_request_crm_jobs','state'), ('crm_reminder_actions','status')):
            if db.execute('SELECT count(*) FROM ' + table + ' WHERE ' + column + "='sending'").fetchone()[0]:
                raise RuntimeError('A write is in progress; snapshot postponed')
    output.mkdir(mode=0o700)
    copied = output / 'data'
    copied.mkdir(mode=0o700)
    manifest = {'created_utc':datetime.now(timezone.utc).isoformat(), 'databases':{}}
    for name in DATABASES:
        source, target = ROOT/'data'/name, copied/name
        with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as original, sqlite3.connect(target) as backup:
            original.backup(backup)
        target.chmod(0o600)
        before, after = digest(source), digest(target)
        if before != after:
            raise RuntimeError('Source changed while copying: ' + name)
        manifest['databases'][name] = after
    manifest_path = copied/'bot-migration-manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    manifest_path.chmod(0o600)
    archive_path = output/'state.tar.gz'
    with tarfile.open(archive_path, 'w:gz') as archive:
        for name in (*DATABASES, 'bot-migration-manifest.json'):
            archive.add(copied/name, arcname='data/'+name)
    archive_path.chmod(0o600)
    print(json.dumps({'ok':True, 'databases':len(DATABASES), 'archive':str(archive_path),
                      'sha256':hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                      'all_tables_preserved':True}))


def verify():
    manifest = json.loads((ROOT/'data/bot-migration-manifest.json').read_text(encoding='utf-8'))
    if set(manifest['databases']) != set(DATABASES):
        raise RuntimeError('Unexpected database manifest')
    for name in DATABASES:
        if digest(ROOT/'data'/name) != manifest['databases'][name]:
            raise RuntimeError('Database state differs: ' + name)
    print(json.dumps({'ok':True, 'databases':len(DATABASES), 'all_tables_preserved':True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('create').add_argument('--output', type=Path, required=True)
    commands.add_parser('verify')
    options = parser.parse_args()
    create(options.output) if options.command == 'create' else verify()
