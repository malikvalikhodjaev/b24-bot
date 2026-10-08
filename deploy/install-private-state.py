"""Install explicitly authorized private bot files; refuse a running service."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path('/home/malik/datfo-crm-bot')
INCOMING = Path('/home/malik/.local/share/fom-bot-deploy/incoming')
FILES = {
    'settings': {'.env', 'registration.json', 'data/miniapp-publication.json'},
    'state': {'data/crm_bot.sqlite3', 'data/simulation.sqlite3',
              'data/communications_test.sqlite3', 'data/bot-migration-manifest.json'},
}


def install(archive_path, expected_hash, kind):
    archive_path = archive_path.resolve(strict=True)
    if archive_path.parent != INCOMING or not archive_path.is_file():
        raise RuntimeError('Expected a private archive in the dedicated incoming directory')
    if not ROOT.is_dir() or ROOT.is_symlink():
        raise RuntimeError('Expected the named bot checkout')
    service = subprocess.run(['systemctl','--user','show','fom-bitrix-bot.service',
                              '-p','ActiveState','-p','MainPID'], check=True,
                             capture_output=True, text=True).stdout.splitlines()
    values = dict(line.split('=', 1) for line in service)
    if values.get('MainPID') != '0' or values.get('ActiveState') not in {'inactive','failed'}:
        raise RuntimeError('Stop the server bot before installing private files')
    archive_path.chmod(0o600)
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != expected_hash:
        raise RuntimeError('Archive hash differs')
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive.getmembers()
        if len(members) != len(FILES[kind]) or {member.name for member in members} != FILES[kind]:
            raise RuntimeError('Unexpected archive members')
        payloads = {}
        for member in members:
            target = ROOT / member.name
            if (not member.isfile() or member.size > 128 * 1024 * 1024
                    or not target.resolve().is_relative_to(ROOT) or target.exists() or target.is_symlink()):
                raise RuntimeError('Unsafe or already existing private target: ' + member.name)
            with archive.extractfile(member) as content:
                payloads[target] = content.read()
    for target, content in payloads.items():
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write(content)
        target.chmod(0o600)
    print(json.dumps({'ok':True, 'kind':kind, 'files':len(payloads), 'permissions':'0600'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--kind', choices=FILES, required=True)
    args = parser.parse_args()
    install(args.archive, args.sha256, args.kind)
