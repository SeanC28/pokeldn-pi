"""Launch one GUI-selected Z-A or Scarlet/Violet trade on a prepared Pi over SSH."""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import shlex
import subprocess
import threading
import uuid


def profile(path):
    data = json.loads(Path(path).expanduser().read_text(encoding='utf-8-sig'))
    for key in ('host', 'user', 'identity', 'known_hosts', 'root', 'sudo_password_file'):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f'Pi connection profile needs {key}')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', data['host']) or not re.fullmatch(r'[A-Za-z0-9_-]+', data['user']):
        raise ValueError('Invalid Pi hostname or username')
    if not data['root'].startswith('/') or '\n' in data['root']:
        raise ValueError('Pi root must be an absolute Linux path')
    return data


def main():
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
    target = f"{cfg['user']}@{cfg['host']}"
    # ssh splits UserKnownHostsFile on spaces, so quote it for paths like "Pokemon Pi Tool".
    known_hosts = Path(cfg['known_hosts']).expanduser()
    opts = ['-i', str(Path(cfg['identity']).expanduser()), '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile="{known_hosts}"',
            '-o', 'ConnectTimeout=10']
    if cfg.get('host_key_alias'):
        opts += ['-o', 'HostKeyAlias='+cfg['host_key_alias']]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    def ssh(command, **kwargs):
        return subprocess.run(['ssh', *opts, target, command], creationflags=flags,
                              check=True, **kwargs)
    password = Path(cfg['sudo_password_file']).expanduser().read_text().strip()
    if '\n' in password:
        raise ValueError('Invalid sudo password file')
    q = shlex.quote
    ssh(f'umask 077; cat > {q(remote_offer)}', input=data)
    env = {'POKEPI_ROOT': root, 'POKEPI_KEYS': cfg.get('keys', root+'/private/prod.keys'),
           'POKEPI_RECEIVED': remote_received, 'POKEPI_TRAINER': args.trainer_name,
           'POKEPI_CODE': args.code if args.code is not None else ('00000000' if game == 'za' else ''),
           'POKEPI_GAME': game}
    print('[pi] Start local Link Trade on the Switch with the same link code; leave it empty for no code.', flush=True)
    command = ('echo $$ > ' + q(pidfile) + '; exec env ' +
               ' '.join(k+'='+q(v) for k,v in env.items()) + ' bash ' +
               q(root+'/run_trade.sh') + ' ' + q(remote_offer))
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
                       check=True, creationflags=flags)
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
