"""Launch one GUI-selected Z-A or Scarlet/Violet trade on a prepared Pi over SSH.

Each run first syncs this checkout's Pi-side code to the Pi (only when it changed), so
the Pi always runs exactly what this computer has. `--sync` does just that step.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import shlex
import subprocess
import sys
import tarfile
import threading
import uuid

REPOSITORY = Path(__file__).resolve().parents[1]
# What the Pi imports: the package, upstream joiners, the Pi scripts, and LDN for setup-pi.sh.
SYNC_PATHS = ('pokeldn', 'bin', 'examples/raspberry_pi', 'vendor/LDN')
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def profile(path):
    data = json.loads(Path(path).expanduser().read_text(encoding='utf-8-sig'))
    for key in ('host', 'user', 'identity', 'known_hosts', 'root', 'sudo_password_file'):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f'Pi connection profile needs {key}')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', data['host']) or not re.fullmatch(r'[A-Za-z0-9_-]+', data['user']):
        raise ValueError('Invalid Pi hostname or username')
    if not data['root'].startswith('/') or '\n' in data['root']:
        raise ValueError('Pi root must be an absolute Linux path')
    if data.get('repo') and (not data['repo'].startswith('/') or '\n' in data['repo']):
        raise ValueError('Pi repo must be an absolute Linux path')
    return data


def connection(cfg):
    """-> an ssh(command, **run_kwargs) helper and the option list scp shares with it."""
    target = f"{cfg['user']}@{cfg['host']}"
    # ssh splits UserKnownHostsFile on spaces, so quote it for paths like "Pokemon Pi Tool".
    known_hosts = Path(cfg['known_hosts']).expanduser()
    opts = ['-i', str(Path(cfg['identity']).expanduser()), '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile="{known_hosts}"',
            '-o', 'ConnectTimeout=10']
    if cfg.get('host_key_alias'):
        opts += ['-o', 'HostKeyAlias='+cfg['host_key_alias']]
    def ssh(command, **kwargs):
        if 'input' not in kwargs:
            kwargs['stdin'] = subprocess.DEVNULL
        return subprocess.run(['ssh', *opts, target, command], creationflags=FLAGS,
                              check=True, **kwargs)
    return ssh, opts, target


def checkout_archive():
    """-> (content hash, tar.gz bytes) of this checkout's SYNC_PATHS, edits included."""
    # Never inherit stdin: under the GUI a thread blocks reading it, and on Windows a
    # child started with that pipe hangs (git did, before printing anything).
    listed = subprocess.run(['git', '-C', str(REPOSITORY), 'ls-files', '-z', '--cached', '--others',
                             '--exclude-standard', '--', *SYNC_PATHS], stdin=subprocess.DEVNULL,
                            capture_output=True, check=True, timeout=30, creationflags=FLAGS).stdout
    digest = hashlib.sha256()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for name in sorted({n.decode() for n in listed.split(b'\0') if n}):
            path = REPOSITORY / name
            if '__pycache__' in name or not path.is_file():
                continue
            data = path.read_bytes()
            if b'\0' not in data:
                # A Windows checkout may hold CRLF text; bash on the Pi rejects it.
                data = data.replace(b'\r\n', b'\n')
            digest.update(name.encode() + b'\0' + len(data).to_bytes(8, 'big') + data)
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith('.sh') else 0o644
            tar.addfile(info, io.BytesIO(data))
    return digest.hexdigest(), buffer.getvalue()


def sync_checkout(cfg, ssh):
    """Replace the Pi's checkout with this one when they differ; -> the checkout path."""
    q = shlex.quote
    repo = cfg.get('repo') or cfg['root'].rstrip('/') + '/repo'
    digest, archive = checkout_archive()
    current = ssh(f'cat {q(repo)}/.sync-hash 2>/dev/null || true', capture_output=True).stdout
    if current.decode(errors='replace').strip() == digest:
        print(f'[pi] code on the Pi is current ({digest[:12]})', flush=True)
        return repo
    # Swap a fully unpacked copy in, and never under a running session's imports.
    ssh('set -e; lock=/run/pokepi-session.lock; '
        'if [ -e "$lock" ] && ! flock -n "$lock" true; then '
        'echo "A Pi radio session is running; not replacing its code." >&2; exit 3; fi; '
        f'new={q(repo + ".new")}; old={q(repo + ".old")}; rm -rf "$new" "$old"; mkdir -p "$new"; '
        f'tar -xzf - -C "$new"; echo {digest} > "$new/.sync-hash"; '
        f'if [ -e {q(repo)} ]; then mv {q(repo)} "$old"; fi; mv "$new" {q(repo)}; rm -rf "$old" || true',
        input=archive)
    print(f'[pi] synced this checkout to {repo} ({digest[:12]}, {len(archive) // 1024} KB)', flush=True)
    return repo


def main():
    if '--sync' in sys.argv[1:]:
        cfg = profile(os.environ['POKEPI_CONNECTION'])
        sync_checkout(cfg, connection(cfg)[0])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('--tool', required=True)
    parser.add_argument('--trade-offer', action='append', required=True)
    parser.add_argument('--offer-out', required=True)
    parser.add_argument('--trainer-name', default='PI-TEST')
    parser.add_argument('--code', default=None)
    parser.add_argument('--fresh-pid', action='store_true')
    parser.add_argument('--for-receiver', action='store_true')
    args, _ = parser.parse_known_args()
    if args.tool not in ('za-join', 'sv-join', 'sv-gift-host') or len(args.trade_offer) != 1:
        raise ValueError('Pi supports one Pokémon per Z-A/SV join or SV simulated-gift run.')
    if args.for_receiver:
        raise ValueError('Pi does not yet remake gifts for the receiving trainer. Turn off that option.')
    game = args.tool.split('-')[0]
    from pokeldn import pokemon
    data = pokemon.prepare_offer(game, Path(args.trade_offer[0]).read_bytes(), fresh=args.fresh_pid)
    cfg = profile(os.environ['POKEPI_CONNECTION'])
    token = uuid.uuid4().hex
    root = cfg['root'].rstrip('/')
    remote_offer = f'{root}/private/gui-{token}.offer.bin'
    extension = 'pa9' if game == 'za' else 'pk9'
    remote_received = f'{root}/private/received/gui-{token}.{extension}'
    pidfile = f'{root}/private/gui-{token}.pid'
    ssh, opts, target = connection(cfg)
    flags = FLAGS
    password = Path(cfg['sudo_password_file']).expanduser().read_text().strip()
    if '\n' in password:
        raise ValueError('Invalid sudo password file')
    q = shlex.quote
    repo = sync_checkout(cfg, ssh)
    ssh(f'umask 077; cat > {q(remote_offer)}', input=data)
    env = {'POKEPI_ROOT': root, 'POKEPI_KEYS': cfg.get('keys', root+'/private/prod.keys'),
           'POKEPI_RECEIVED': remote_received, 'POKEPI_TRAINER': args.trainer_name,
           'POKEPI_CODE': args.code if args.code is not None else ('00000000' if game == 'za' else ''),
           'POKEPI_GAME': game}
    print('[pi] Start local Link Trade on the Switch with the same link code; leave it empty for no code.', flush=True)
    command = ('echo $$ > ' + q(pidfile) + '; exec env ' +
               ' '.join(k+'='+q(v) for k,v in env.items()) + ' bash ' +
               q(repo+'/examples/raspberry_pi/run_trade.sh') + ' ' + q(remote_offer))
    proc = subprocess.Popen(['ssh', *opts, target,
        'sudo -S -p "" setsid bash -c ' + q(command)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
        errors='replace', creationflags=flags)
    proc.stdin.write(password+'\n')
    proc.stdin.flush()
    lines = queue.Queue()
    def read():
        for line in proc.stdout:
            lines.put(line.rstrip())
        lines.put(None)
    threading.Thread(target=read, daemon=True).start()
    try:
        while True:
            try:
                line = lines.get(timeout=.2)
            except queue.Empty:
                continue
            if line is None:
                break
            print(line, flush=True)
        code = proc.wait()
        if code:
            raise RuntimeError(f'Pi trade exited with status {code}')
        destination = Path(args.offer_out).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['scp', *opts, target+':'+remote_received, str(destination)],
                       stdin=subprocess.DEVNULL, check=True, creationflags=flags)
        print(f'[pi] Received Pokémon saved to {destination}', flush=True)
    finally:
        if proc.poll() is None:
            stop = 'test ! -f '+q(pidfile)+' || kill -TERM -- -"$(cat '+q(pidfile)+')"'
            try:
                ssh('sudo -S -p "" bash -c '+q(stop), input=(password+'\n').encode(), timeout=15)
            except (OSError, subprocess.SubprocessError):
                print('[pi] Stop could not be confirmed; the Pi timeout will restore its radio.', flush=True)
            proc.terminate()
        ssh('rm -f -- '+q(remote_offer)+' '+q(pidfile), timeout=15)


if __name__ == '__main__':
    main()
