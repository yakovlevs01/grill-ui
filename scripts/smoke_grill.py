#!/usr/bin/env python3
"""Real Herdr/PTY test. Optional --remote uses a saved machine in isolated client config."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid
import pyte

# Private post-mortem copies of the last run: client screen, transcript and logs.
DEBUG = Path(tempfile.gettempdir())/'grill-smoke-debug'


class Screen(pyte.Screen):
    def report_device_status(self, mode, **kwargs):
        pass


def stop_process(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


ROOT = Path(__file__).resolve().parent.parent


def until(fn, label, timeout=40):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(.15)
    raise RuntimeError('Timed out: ' + label)


def clean_env():
    env = {k:v for k,v in os.environ.items() if not k.startswith('HERDR_')}
    env['PATH'] = str(Path.home()/'.local/bin') + ':/opt/homebrew/bin:/usr/local/bin:' + env['PATH']
    return env


def prepare():
    import grill_ui as g
    import grill_herdr as h
    tmp = Path(tempfile.mkdtemp(prefix='grill-smoke-'))
    session = 'grill-test-' + uuid.uuid4().hex[:10]
    env = clean_env()
    env['HERDR_SESSION'] = session
    binary = h.herdr_binary({**env, 'HERDR_BIN_PATH': os.environ.get('HERDR_BIN_PATH', '')})
    # A unique real session on this host; no existing session is reused.
    with (tmp/'herdr.log').open('wb') as log:
        subprocess.Popen([binary, '--session', session, 'server'], env=env,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    config = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config')))
    sock = until(lambda: next(config.glob('herdr*/sessions/'+session+'/herdr.sock'),None), 'server socket')
    env.update(HERDR_SOCKET_PATH=str(sock), HERDR_BIN_PATH=binary)
    workspace = h.herdr(['workspace','create','--cwd',str(tmp),'--label','Grill test'], env)
    pane = workspace['root_pane']
    env.update(HERDR_ENV='1', HERDR_WORKSPACE_ID=pane['workspace_id'],
               HERDR_TAB_ID=pane['tab_id'], HERDR_PANE_ID=pane['pane_id'])
    # The fake agent exercises the real exec/resume subprocess transport.
    bindir = tmp/'bin'; bindir.mkdir()
    fake = bindir/'codex'
    fake.write_text('#!' + sys.executable + '\n' + '''import sys,json,time
args=sys.argv[1:]; prompt=sys.stdin.read()
thread=args[args.index('resume')+1] if 'resume' in args else 'smoke-thread'
print(json.dumps({'type':'thread.started','thread_id':thread}),flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Ответ тестового агента'}}),flush=True)
''')
    fake.chmod(0o700)
    env['PATH'] = str(bindir)+os.pathsep+env['PATH']
    round_dir = tmp/'round'
    # A private data dir: open must build its own venv, and the owner's venv and registry stay untouched.
    env['XDG_DATA_HOME'] = str(tmp/'data')
    subprocess.run([sys.executable,str(ROOT/'scripts/grill_ui.py'),'init','--session',str(round_dir),
        '--round',str(ROOT/'assets/example-round.json'),'--cwd',str(tmp),'--harness','codex','--model','smoke','--effort','high'],
        env=env,check=True,stdout=subprocess.DEVNULL)
    result = subprocess.run([sys.executable,str(ROOT/'scripts/grill_herdr.py'),'open','--session',str(round_dir),'--focus'],
        env=env,capture_output=True,text=True)
    if result.returncode:
        raise RuntimeError(result.stderr)
    assert json.loads(result.stdout)['tab'], 'open stdout must stay JSON after installing'
    assert (tmp/'data/grill-ui/venv/grill-requirements.sha256').is_file(), 'open did not install the venv'
    info = {'tmp':str(tmp),'session':session,'round':str(round_dir),'env':{k:v for k,v in env.items() if k.startswith(('HERDR_', 'XDG_')) or k in ('PATH','LANG','LC_ALL','SHELL')},
            'binary':binary,'root':str(ROOT)}
    # Keep environment private, only expose the path to the test controller.
    g.write(tmp/'fixture.json', info)
    os.chmod(tmp/'fixture.json',0o600)
    return {'fixture':str(tmp/'fixture.json'),'session':session}


def control(path, action):
    import grill_ui as g
    import grill_herdr as h
    from grill_client import Client
    info = g.read(path)
    env = clean_env()
    env.update(info['env'])
    owner = g.read(Path(info['round'])/'herdr.json')
    client = Client(info['round'])
    if action == 'screen':
        return h.herdr(['pane','read',owner['pane'],'--source','visible','--lines','60'],env)
    if action == 'tui-running':
        with (Path(info['round'])/'tui.lock').open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
    if action == 'reopen':
        result=subprocess.run([sys.executable,str(ROOT/'scripts/grill_herdr.py'),'reopen'],
                              env=env,capture_output=True,text=True,check=True)
        return json.loads(result.stdout)
    agent_screen=lambda: re.sub(r'\s|\\n','',str(h.herdr(['pane','read',owner['parent_pane'],'--source','visible','--lines','30'],env)))
    if action == 'state':
        state = client.request('/api/state')
        return {'answer':state['answers']['Q1'], 'branch':state['branches']['Q1'],
                'submitted':state['rounds'][-1]['id'] in state['submitted_rounds']}
    if action == 'exercise':
        for qid in ('Q1','Q2','Q3'):
            client.request('/api/answer',{'question_id':qid,'selected':[], 'text':'Тест '+qid,'confirmed':True})
        client.request('/api/chat',{'question_id':'Q1','message':'Обсудим'})
        until(lambda: client.request('/api/state')['branches']['Q1']['status']!='running','first chat')
        client.request('/api/chat',{'question_id':'Q1','message':'Продолжим'})
        state=until(lambda: (s if (s:=client.request('/api/state'))['branches']['Q1']['status']!='running' else None),'second chat')
        assert len(state['branches']['Q1']['messages'])==4, state['branches']['Q1']
        assert state['branches']['Q1']['thread_id']=='smoke-thread'
        return client.request('/api/submit',{})
    if action == 'notify':
        # cat echoes the typed line and prints it again only after a real Enter.
        h.herdr(['pane','run',owner['parent_pane'],'clear; cat'],env)
        time.sleep(.5)
        h.notify_agent(info['round'])
        # The notice names the newest result file; wrapped lines are joined before matching.
        newest=re.sub(r'\s','',g.read(Path(info['round'])/'status.json')['result_file'])
        until(lambda: agent_screen().count(newest)>=2, 'notice typed and entered in agent pane')
        h.herdr(['pane','send-keys',owner['parent_pane'],'ctrl+c'],env)
        return {'notified':True}
    if action == 'next-round':
        # The agent adds round 2 to the live session; the open TUI switches to it.
        doc=g.read(ROOT/'assets/example-round.json')
        doc.update(id='smoke-r2',title='Второй раунд',questions=[
            {**doc['questions'][2],'id':qid,'title':'Второй '+qid,'depends_on':['Q1']} for qid in ('Q4','Q5')])
        g.write(Path(info['tmp'])/'round2.json',doc)
        added=subprocess.run([sys.executable,str(ROOT/'scripts/grill_ui.py'),'add-round','--session',info['round'],
                              '--round',str(Path(info['tmp'])/'round2.json')],env=env,capture_output=True,text=True,check=True)
        assert json.loads(added.stdout)['live'], added.stdout
        until(lambda: 'Раунд 2 · текущий' in str(control(path,'screen')) and 'Вопрос 4' in str(control(path,'screen')),
              'TUI switched to round 2')
        for qid in ('Q4','Q5'):
            client.request('/api/answer',{'question_id':qid,'selected':[],'text':'Второй '+qid,'confirmed':True})
        client.request('/api/reopen',{'question_id':'Q2','reason':'Нужны теги'})
        # The TUI types its notice here once the owner presses the submit button.
        h.herdr(['pane','run',owner['parent_pane'],'clear; cat'],env)
        return {'added':True}
    if action == 'second-notice':
        status=g.read(Path(info['round'])/'status.json')
        assert status['result_file'].endswith('submissions/0002.json'), status
        newest=re.sub(r'\s','',status['result_file'])
        until(lambda: agent_screen().count(newest)>=2, 'TUI notice names the new file')
        h.herdr(['pane','send-keys',owner['parent_pane'],'ctrl+c'],env)
        result=g.read(status['result_file'])
        assert result['round_id']=='smoke-r2' and [a['question_id'] for a in result['answers']]==['Q4','Q5'], result
        assert [(r['question_id'],r['number']) for r in result['reopen']]==[('Q2',2)], result
        assert g.read(Path(info['round'])/'submissions/0001.json')['round_id']=='demo-r1'
        return {'result_file':status['result_file']}
    if action == 'handoff':
        # Live handoff recreates the socket and terminal IDs; panes and their processes survive.
        sock = Path(info['env']['HERDR_SOCKET_PATH'])
        before = sock.stat().st_ino
        terminals = lambda: {p['pane_id']: p['terminal_id'] for p in
                             h.herdr(['pane','list','--workspace',owner['workspace']],env)['panes']}
        old = terminals()
        subprocess.run([info['binary'],'--session',info['session'],'server','live-handoff'],env=env,
                       capture_output=True,check=True,timeout=40)
        new = terminals()
        assert sock.stat().st_ino != before, 'handoff kept the socket'
        assert new.keys() == old.keys(), (old, new)
        assert control(path,'tui-running'), 'TUI did not survive handoff'
        return {'terminals_changed': new != old}
    if action == 'finish':
        result=subprocess.run([sys.executable,str(ROOT/'scripts/grill_herdr.py'),'finish','--session',info['round']],
                              env=env,capture_output=True,text=True,check=True)
        tabs=h.herdr(['tab','list','--workspace',owner['workspace']],env)['tabs']
        assert owner['tab'] not in [t['tab_id'] for t in tabs]
        assert owner['parent_tab'] in [t['tab_id'] for t in tabs]
        return json.loads(result.stdout)
    if action == 'cleanup':
        # Only the unique test server and its own grill server are stopped.
        if client.healthy():
            try:
                state=client.request('/api/state')
                current=state['rounds'][-1]
                if current['id'] not in state['submitted_rounds']:
                    for qid in state['answers']:
                        client.request('/api/stop',{'question_id':qid})
                    until(lambda:all(b['status']!='running' for b in client.request('/api/state')['branches'].values()),'test agents stop')
                    for q in current['questions']:
                        client.request('/api/answer',{'question_id':q['id'],'selected':[],'text':'test cleanup','confirmed':True})
                    client.request('/api/submit',{})
                client.request('/api/shutdown',{})
            except Exception:
                pass
        subprocess.run([info['binary'],'--session',info['session'],'server','stop'],env=env,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        shutil.rmtree(Path(info['tmp'])/'data',ignore_errors=True)
        # Drop only this run's unique session directory from the Herdr config.
        sessions=Path(info['env']['HERDR_SOCKET_PATH']).parent
        if sessions.name==info['session'] and sessions.name.startswith('grill-test-'):
            try:
                until(lambda: not Path(info['env']['HERDR_SOCKET_PATH']).exists(),'test server stop',timeout=10)
                shutil.rmtree(sessions,ignore_errors=True)
            except RuntimeError:
                pass  # Never mask the test's own failure from inside cleanup.
        return {'cleaned':True}
    raise ValueError(action)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--isolation',action='store_true')
    parser.add_argument('--control',nargs=2,metavar=('FIXTURE','ACTION'))
    parser.add_argument('--remote')
    parser.add_argument('--remote-root')
    parser.add_argument('--remote-python')
    args=parser.parse_args()
    if args.isolation:
        fixtures=[]
        try:
            fixtures.append(prepare())
            fixtures.append(prepare())
            first,second=fixtures
            control(first['fixture'],'exercise')
            control(first['fixture'],'finish')
            assert not control(second['fixture'],'state')['submitted']
            assert control(second['fixture'],'tui-running')
            control(second['fixture'],'exercise')
            control(second['fixture'],'finish')
            print(json.dumps({'ok':True,'mode':'two-sessions','sessions':[f['session'] for f in fixtures]}))
        finally:
            for fixture in fixtures:
                control(fixture['fixture'],'cleanup')
        return
    if args.prepare:
        print(json.dumps(prepare())); return
    if args.control:
        print(json.dumps(control(*args.control),ensure_ascii=False)); return
    import grill_herdr as h
    env=clean_env()
    binary=h.herdr_binary({**env,'HERDR_BIN_PATH':os.environ.get('HERDR_BIN_PATH','')})
    def invoke(*arguments):
        if args.remote:
            cmd=['ssh','-o','BatchMode=yes',args.remote,shlex.join([args.remote_python,
                 args.remote_root+'/scripts/smoke_grill.py',*arguments])]
        else:
            cmd=[sys.executable,str(Path(__file__)),*arguments]
        # Prepare includes the first venv install.
        out=subprocess.run(cmd,capture_output=True,text=True,timeout=180)
        if out.returncode: raise RuntimeError(out.stderr or out.stdout)
        return json.loads(out.stdout)
    info=invoke('--prepare')
    ctl=lambda action:invoke('--control',info['fixture'],action)
    client=None
    master=None
    transcript=bytearray()
    screen_buffer=Screen(160,48)
    terminal_stream=pyte.ByteStream(screen_buffer)
    screen_lock=threading.Lock()
    DEBUG.mkdir(mode=0o700,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='grill-client-', dir='/tmp') as temp:
        tmp=Path(temp)
        client_env=env.copy()
        client_env.update(XDG_CONFIG_HOME=str(tmp/'config'),XDG_STATE_HOME=str(tmp/'state'),
                          TERM='xterm-256color',COLORTERM='truecolor')
        cfg=tmp/'config/herdr';cfg.mkdir(parents=True)
        (cfg/'config.toml').write_text('onboarding = false\n[ui]\nsidebar_width = 26\n')
        try:
            if args.remote:
                add=subprocess.run([binary,'machine','add',args.remote,'--label','Grill test',
                                    '--remote-session',info['session']],env=client_env,
                                   capture_output=True,text=True,timeout=40)
                if add.returncode:
                    raise RuntimeError(add.stdout+add.stderr)
                profiles=json.loads(subprocess.check_output([binary,'machine','list','--json'],env=client_env))
                selected=profiles[0]['id']
                catalog=next(tmp.rglob('endpoints.json'))
                (catalog.parent/'endpoint-selection.json').write_text(json.dumps({'version':1,'selected_profile':selected}))
                launch=[binary,'--session','grill-client-'+uuid.uuid4().hex[:8]]
            else:
                # The real config locates the test session; a private state dir hides
                # the owner's saved machines and their live selection from this client.
                client_env=env.copy();client_env.update(XDG_STATE_HOME=str(tmp/'state'),
                                                         TERM='xterm-256color',COLORTERM='truecolor')
                launch=[binary,'--session',info['session']]
            def attach():
                nonlocal client,master
                transcript.clear()
                with screen_lock:
                    screen_buffer.reset()
                master,slave=pty.openpty()
                fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',48,160,1600,960))
                client=subprocess.Popen(launch,env=client_env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
                os.close(slave)
                fd=master
                def drain():
                    try:
                        while True:
                            chunk=os.read(fd,65536)
                            if not chunk: break
                            with screen_lock:
                                terminal_stream.feed(chunk)
                            transcript.extend(chunk)
                            if len(transcript)>2000000: del transcript[:1000000]
                    except OSError:pass
                threading.Thread(target=drain,daemon=True).start()
            attach()
            until(lambda: 'Ваш ответ' in re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', transcript.decode('utf-8',errors='ignore')), 'client displays selected machine')
            screen=until(lambda: (s if 'Ваш ответ' in str(s:=ctl('screen')) else None),'TUI screen')
            # F4 focuses the answer field; actual terminal bracketed paste crosses Herdr.
            os.write(master,b'\x1bOS')
            time.sleep(.2)
            pasted='REMOTE_PASTE_Привет\nстрока'
            os.write(master,('\x1b[200~'+pasted+'\x1b[201~').encode('utf-8'))
            until(lambda:ctl('state')['answer']['text']==pasted,'remote Unicode multiline paste')
            def click(label):
                def position():
                    # The topmost match: a header button comes before the status line's hint.
                    with screen_lock:
                        for row,line in enumerate(screen_buffer.display):
                            if label in line:
                                return (line.index(label)+4, row+1)
                time.sleep(.5)
                x,y=until(position,label+' button geometry')
                (DEBUG/'mouse.json').write_text(json.dumps({'x':x,'y':y,'screen':screen_buffer.display},ensure_ascii=False))
                # Honor the outer client's selected mouse encoding, including pixel mode.
                raw=bytes(transcript)
                if raw.rfind(b'\x1b[?1016h') > raw.rfind(b'\x1b[?1016l'):
                    x,y=(x-1)*10+5,(y-1)*20+10
                os.write(master,f'\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m'.encode())
            click('Подтвердить ответ')
            until(lambda:ctl('state')['answer']['confirmed'],'mouse confirmation through client')
            # Detach/reconnect the client while the server and tab survive.
            stop_process(client);os.close(master);master=None
            attach()
            until(lambda: any('Ваш ответ' in line for line in screen_buffer.display), 'client after reconnect')
            until(lambda:'Ваш ответ' in str(ctl('screen')),'screen after reconnect')
            os.write(master,b'\x11')
            until(lambda:not ctl('tui-running'),'TUI exit leaves server running')
            ctl('reopen')
            until(lambda:ctl('tui-running'),'reopen same tab')
            assert ctl('state')['answer']['text']==pasted
            result=ctl('exercise')
            assert len(result['answers'])==3
            assert result['file'].endswith('submissions/0001.json'), result
            assert 'Ответ тестового агента' not in str(result)
            # Round 2 in the same tab; the owner's click sends it with the reopen request.
            ctl('next-round')
            until(lambda: any('Вопрос 4' in line for line in screen_buffer.display), 'client shows round 2')
            click('Отправить все ответы')
            second=ctl('second-notice')
            # Both the TUI's notice and finish must rebind after a handoff; it also drops this client.
            ctl('handoff')
            ctl('notify')
            ctl('handoff')
            finished=ctl('finish')
            assert finished['result_file']==second['result_file'], finished
            print(json.dumps({'ok':True,'mode':'saved-machine' if args.remote else 'local',
                              'remote':args.remote,'session':info['session'],
                              'checks':['render','unicode-multiline-paste','mouse-confirm','reconnect','reopen','two-turn-chat','compact-submit','handoff','agent-notice','second-round-same-tab','reopen-request','new-file-notice','owned-tab-cleanup','auto-install']},ensure_ascii=False))
        finally:
            (DEBUG/'client-transcript.txt').write_bytes(transcript)
            with screen_lock:
                (DEBUG/'client-screen.txt').write_text('\n'.join(screen_buffer.display))
            for log in tmp.rglob('*.log'):
                (DEBUG/log.name).write_bytes(log.read_bytes())
            if client and client.poll() is None:
                stop_process(client)
            if master is not None:
                os.close(master)
            ctl('cleanup')
            if args.remote:
                subprocess.run([binary,'--session',launch[2],'server','stop'],env=client_env,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':
    main()
