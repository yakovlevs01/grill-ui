#!/usr/bin/env python3
"""Install, open, wait for and finish the Grill tab of one grill on the agent's Herdr server."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from grill_client import Client
from grill_ui import load_state, offline, read, require, summary, write

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'grill-ui'
PYTHON = DATA / 'venv/bin/python'
CONTEXT_KEYS = ('HERDR_SOCKET_PATH', 'HERDR_SESSION', 'HERDR_CONFIG_PATH',
                'HERDR_BIN_PATH', 'HERDR_WORKSPACE_ID', 'HERDR_TAB_ID', 'HERDR_PANE_ID')
SUBMIT_NOTICE = 'Готово: ответы Grill отправлены'


def herdr_binary(env):
    # A rebuilt Herdr leaves the server with a stale "/path/herdr (deleted)".
    binary = env.get('HERDR_BIN_PATH')
    if binary and os.access(binary, os.X_OK):
        return binary
    return shutil.which('herdr', path=env.get('PATH'))


def herdr(args, env=None):
    env = dict(os.environ if env is None else env)
    binary = herdr_binary(env)
    require(binary, 'herdr is not installed')
    result = subprocess.run([binary, *args], env=env, capture_output=True, text=True, timeout=15)
    require(result.returncode == 0, result.stderr.strip() or result.stdout.strip() or 'Herdr command failed')
    try:
        return json.loads(result.stdout)['result']
    except json.JSONDecodeError:
        return {'text': result.stdout}


def context():
    require(os.environ.get('HERDR_ENV') == '1', 'Run inside the agent’s Herdr pane')
    require(os.environ.get('HERDR_SOCKET_PATH'), 'Missing Herdr server socket')
    pane = herdr(['pane', 'current', '--current'])['pane']
    endpoint = Path(os.environ['HERDR_SOCKET_PATH']).resolve()
    stat = endpoint.stat()
    return {'host': socket.gethostname(), 'socket': str(endpoint),
            'socket_identity': [stat.st_dev, stat.st_ino],
            'env': {k: os.environ[k] for k in CONTEXT_KEYS if k in os.environ},
            'workspace': pane['workspace_id'], 'parent_tab': pane['tab_id'],
            'parent_pane': pane['pane_id'], 'parent_terminal': pane['terminal_id']}


def owner_env(owner):
    require(owner['host'] == socket.gethostname(), 'This round belongs to another host')
    stat = Path(owner['socket']).stat()
    require(owner['socket_identity'] == [stat.st_dev, stat.st_ino],
            'Herdr server endpoint changed; refusing to reuse old tab IDs')
    env = {k: v for k, v in os.environ.items() if not k.startswith('HERDR_')}
    env.update(owner['env'])
    env.update(HERDR_ENV='1', HERDR_SOCKET_PATH=owner['socket'])
    return env


@contextmanager
def locked(session):
    with (session / 'lifecycle.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def ensure_server(session):
    client = Client(session)
    if client.healthy():
        return client
    with (session / 'server.log').open('ab') as log:
        proc = subprocess.Popen([str(PYTHON), str(ROOT / 'scripts/grill_ui.py'),
            'serve', '--session', str(session)], stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True)
    for _ in range(60):
        if client.healthy():
            return client
        if proc.poll() is not None:
            raise ValueError(f'Server failed; inspect {session / "server.log"}')
        time.sleep(.1)
    raise ValueError(f'Server startup timed out; inspect {session / "server.log"}')


def registry_key(owner):
    return hashlib.sha256((owner['socket'] + ':' + owner['workspace']).encode()).hexdigest()[:24]


def tab_present(owner, env):
    return any(t['tab_id'] == owner.get('tab') for t in
               herdr(['tab', 'list', '--workspace', owner['workspace']], env)['tabs'])


def owned_pane(owner, env):
    panes = herdr(['pane', 'list', '--workspace', owner['workspace']], env)['panes']
    panes = [p for p in panes if p['tab_id'] == owner['tab']]
    require(len(panes) == 1 and panes[0]['pane_id'] == owner['pane']
            and panes[0]['terminal_id'] == owner['terminal'],
            'Grill tab topology changed; refusing to close or replace user panes')
    return panes[0]


def open_tab(args):
    session = Path(args.session).resolve()
    require((session / 'state.json').is_file(), 'Initialize the grill first')
    require(PYTHON.exists(), 'Run grill_herdr.py install on this host first')
    # Fail before opening a tab if the environment is incomplete.
    subprocess.run([str(PYTHON), '-c', 'import textual'], check=True, capture_output=True)
    caller = context()
    with locked(session):
        path = session / 'herdr.json'
        owner = read(path) if path.exists() else caller
        require(owner['host'] == caller['host'] and owner['socket'] == caller['socket']
                and owner['workspace'] == caller['workspace'],
                'Grill belongs to a different Herdr server/workspace')
        require(not owner.get('finished'), 'This grill is finished; start a new one')
        env = owner_env(owner)
        ensure_server(session)
        if owner.get('tab') and tab_present(owner, env):
            owned_pane(owner, env)
            with (session / 'tui.lock').open('a') as tui_lock:
                try:
                    fcntl.flock(tui_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    info = herdr(['pane', 'process-info', '--pane', owner['pane']], env)['process_info']
                    require(info['foreground_process_group_id'] == info['shell_pid'],
                            'Grill tab contains another foreground process; refusing to type into it')
                    fcntl.flock(tui_lock, fcntl.LOCK_UN)
                    command = shlex.join([str(PYTHON), str(ROOT / 'scripts/grill_tui.py'), '--session', str(session)])
                    herdr(['pane', 'run', owner['pane'], command], env)
            if args.focus:
                herdr(['tab', 'focus', owner['tab']], env)
            print(json.dumps({'tab': owner['tab'], 'reused': True}))
            return
        state = read(session / 'state.json')
        created = herdr(['tab', 'create', '--workspace', owner['workspace'],
                        '--cwd', state['cwd'], '--label', 'Grill', '--no-focus'], env)
        owner.update(tab=created['tab']['tab_id'], pane=created['root_pane']['pane_id'],
                     terminal=created['root_pane']['terminal_id'])
        write(path, owner)
        command = shlex.join([str(PYTHON), str(ROOT / 'scripts/grill_tui.py'), '--session', str(session)])
        try:
            herdr(['pane', 'run', owner['pane'], command], env)
            if args.focus:
                herdr(['tab', 'focus', owner['tab']], env)
        except Exception:
            # This tab was just created by us and contains only our command.
            herdr(['tab', 'close', owner['tab']], env)
            raise
        registry = DATA / 'active' / (registry_key(owner) + '.json')
        write(registry, {'session': str(session)})
        print(json.dumps({'tab': owner['tab'], 'workspace': owner['workspace'],
                          'host': owner['host'], 'session': str(session)}, ensure_ascii=False))


def finish(args):
    session = Path(args.session).resolve()
    # Called once, after the owner confirmed the final picture; an open round keeps the tab.
    status = summary(load_state(session), session)
    require(status['submitted'], 'The latest round has not been submitted; leave the tab open')
    with locked(session):
        owner = read(session / 'herdr.json')
        if owner.get('finished'):
            print(json.dumps({'finished': True, 'already_finished': True}))
            return
        env = owner_env(owner)
        if tab_present(owner, env):
            owned_pane(owner, env)
            info = herdr(['pane', 'process-info', '--pane', owner['pane']], env)['process_info']
            at_shell = info['foreground_process_group_id'] == info['shell_pid']
            processes = info.get('foreground_processes', [])
            is_tui = bool(processes) and all(
                str(ROOT / 'scripts/grill_tui.py') in p.get('argv', [])
                and str(session) in p.get('argv', []) for p in processes)
            require(at_shell or is_tui, 'Another process occupies the Grill tab; refusing to close it')
            herdr(['tab', 'close', owner['tab']], env)
        client = Client(session)
        if client.healthy():
            client.request('/api/shutdown', {})
        else:
            with offline(session) as store:
                store.finish()
        owner['finished'] = True
        write(session / 'herdr.json', owner)
        registry = DATA / 'active' / (registry_key(owner) + '.json')
        if registry.exists() and read(registry).get('session') == str(session):
            registry.unlink()
        print(json.dumps({'finished': True, 'result_file': status['result_file']}, ensure_ascii=False))


def agent_pane(session):
    owner = read(Path(session) / 'herdr.json')
    env = owner_env(owner)
    pane = herdr(['pane', 'get', owner['parent_pane']], env)['pane']
    require(pane['terminal_id'] == owner['parent_terminal'], 'Original agent terminal is gone')
    return pane, env


def return_to_agent(session):
    pane, env = agent_pane(session)
    herdr(['tab', 'focus', pane['tab_id']], env)


def submit_notice(session):
    # Only the newest result file: the agent reads what changed, and the owner can open it too.
    return f'{SUBMIT_NOTICE}. Файл: {read(Path(session) / "status.json")["result_file"]}'


def notify_agent(session):
    """Type the submit notice into the agent's input, after any text already there."""
    pane, env = agent_pane(session)
    herdr(['pane', 'send-text', pane['pane_id'], submit_notice(session)], env)
    # A separate Enter keeps agent TUIs from folding it into the text as a pasted newline.
    time.sleep(.2)
    herdr(['pane', 'send-keys', pane['pane_id'], 'enter'], env)


def wait(args):
    deadline = time.monotonic() + args.timeout
    path = Path(args.session) / 'status.json'
    while True:
        status = read(path)
        if status['submitted'] or time.monotonic() >= deadline:
            print(json.dumps(status, ensure_ascii=False))
            return
        time.sleep(.5)


def install(args):
    DATA.mkdir(parents=True, exist_ok=True)
    if shutil.which('uv'):
        if not PYTHON.exists():
            subprocess.run(['uv', 'venv', '--python', '>=3.11', str(PYTHON.parent.parent)], check=True)
        subprocess.run(['uv', 'pip', 'install', '--python', str(PYTHON), '-r', str(ROOT / 'requirements.txt')], check=True)
    else:
        if not PYTHON.exists():
            subprocess.run([sys.executable, '-m', 'venv', str(PYTHON.parent.parent)], check=True)
        subprocess.run([str(PYTHON), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')], check=True)
    print(json.dumps({'python': str(PYTHON)}))


def reopen(args):
    caller = context()
    entry = DATA / 'active' / (registry_key(caller) + '.json')
    require(entry.exists(), 'No Grill round registered in this workspace')
    args.session = read(entry)['session']
    args.focus = True
    open_tab(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='action', required=True)
    for name, fn in [('open', open_tab), ('finish', finish), ('wait', wait)]:
        p = subs.add_parser(name)
        p.add_argument('--session', required=True)
        if name == 'open':
            p.add_argument('--focus', action='store_true')
        if name == 'wait':
            p.add_argument('--timeout', type=float, default=45)
        p.set_defaults(func=fn)
    for name, fn in [('install', install), ('reopen', reopen)]:
        subs.add_parser(name).set_defaults(func=fn)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'grill: {exc}\n')


if __name__ == '__main__':
    main()
