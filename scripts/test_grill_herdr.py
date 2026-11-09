"""Guard against cross-host/session and modified-tab cleanup."""
import contextlib
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import grill_herdr as h


class OwnershipTests(unittest.TestCase):
    def test_other_host_and_restarted_endpoint_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'socket'
            path.touch()
            stat = path.stat()
            owner = {'host': h.socket.gethostname(), 'socket': str(path),
                     'socket_identity': [stat.st_dev,stat.st_ino], 'env': {'HERDR_SESSION':'one'}}
            self.assertEqual(h.owner_env(owner)['HERDR_SESSION'], 'one')
            owner['host'] = 'different-host'
            with self.assertRaisesRegex(ValueError, 'another host'):
                h.owner_env(owner)
            owner['host'] = h.socket.gethostname()
            owner['socket_identity'][1] += 1
            with self.assertRaisesRegex(ValueError, 'endpoint changed'):
                h.owner_env(owner)

    def test_matching_ids_in_another_session_are_not_same_registry(self):
        self.assertNotEqual(h.registry_key({'socket':'/s/one','workspace':'w1'}),
                            h.registry_key({'socket':'/s/two','workspace':'w1'}))

    def test_added_or_replaced_pane_prevents_close(self):
        owner = {'workspace':'w1','tab':'w1:t2','pane':'w1:p2','terminal':'term-a'}
        pane = {'tab_id':'w1:t2','pane_id':'w1:p2','terminal_id':'term-a'}
        with patch.object(h, 'herdr', return_value={'panes':[pane]}):
            self.assertEqual(h.owned_pane(owner, {}), pane)
        for panes in ([pane,pane], [{**pane,'terminal_id':'term-b'}], []):
            with patch.object(h, 'herdr', return_value={'panes':panes}):
                with self.assertRaisesRegex(ValueError, 'topology changed'):
                    h.owned_pane(owner, {})

    def test_stale_bin_path_falls_back_to_path(self):
        done = h.subprocess.CompletedProcess([], 0, stdout='{"result": {}}', stderr='')
        with patch.object(h.shutil, 'which', return_value='/usr/bin/herdr'), \
             patch.object(h.subprocess, 'run', return_value=done) as run:
            h.herdr(['pane', 'current'], env={'HERDR_BIN_PATH': '/gone/herdr (deleted)'})
        self.assertEqual(run.call_args.args[0][0], '/usr/bin/herdr')

    def test_notice_goes_only_to_owned_agent_pane(self):
        with tempfile.TemporaryDirectory() as tmp:
            h.write(Path(tmp)/'herdr.json', {'parent_pane': 'w1:p1', 'parent_terminal': 'term-a'})
            # The notice names only the newest result file, never an earlier round's.
            newest = str(Path(tmp).resolve() / 'submissions/0002.json')
            h.write(Path(tmp)/'status.json', {'submitted': True, 'result_file': newest})
            h.write(Path(newest), {'round_id': 'r2', 'answers': [], 'reopen': []})
            pane = {'pane_id': 'w1:p1', 'tab_id': 'w1:t1', 'terminal_id': 'term-a'}
            calls = []
            def fake(args, env=None):
                calls.append(args)
                return {'pane': pane}
            with patch.object(h, 'owner_env', return_value={}), patch.object(h, 'herdr', fake), \
                 patch.object(h.time, 'sleep'):
                h.notify_agent(tmp)
                notice = h.SUBMIT_NOTICE + '. Файл: ' + newest
                self.assertEqual(calls[1:], [['pane', 'send-text', 'w1:p1', notice],
                                             ['pane', 'send-keys', 'w1:p1', 'enter']])
                # A reopen-only submit has no round and says so.
                h.write(Path(newest), {'round_id': None, 'answers': [], 'reopen': [{'question_id': 'Q1'}]})
                calls.clear()
                h.notify_agent(tmp)
                self.assertEqual(calls[1][3], h.REOPEN_NOTICE + '. Файл: ' + newest)
                pane['terminal_id'] = 'term-b'
                calls.clear()
                with self.assertRaisesRegex(ValueError, 'terminal is gone'):
                    h.notify_agent(tmp)
                self.assertEqual(len(calls), 1)

    def test_compact_answers_do_not_include_option_descriptions(self):
        import test_grill_ui
        f = test_grill_ui.RoundTests()
        f.setUp()
        try:
            for qid in f.store.state['answers']:
                f.store.answer(qid, {'selected': ['local'] if qid=='Q1' else [],
                                    'text': 'Принято', 'confirmed':True})
            result = f.store.submit()
            self.assertEqual(result['answers'][0], {'question_id':'Q1','number':1,'selected':['local'],'text':'Принято'})
            self.assertTrue(h.read(f.root/'status.json')['submitted'])
        finally:
            f.tearDown()

    def test_run_tui_repeats_an_enter_the_new_shell_dropped(self):
        owner = {'pane': 'w1:p2'}
        calls, entered = [], []
        def fake(args, env=None):
            calls.append(args)
            if args[:2] == ['pane', 'send-keys']:
                entered.append(True)
            # The shell sits at its prompt: nothing runs in the foreground.
            return {'process_info': {'foreground_process_group_id': 7, 'shell_pid': 7}}
        clock = iter(range(0, 1000))
        with patch.object(h, 'herdr', fake), patch.object(h.time, 'sleep'), \
             patch.object(h.time, 'monotonic', lambda: next(clock)), \
             patch.object(h, 'tui_alive', lambda session: bool(entered)):
            h.run_tui(owner, {}, '/tmp/s')
        self.assertEqual(calls[0][:3], ['pane', 'run', 'w1:p2'])
        self.assertEqual([c for c in calls if c[1] == 'send-keys'], [['pane', 'send-keys', 'w1:p2', 'enter']])
        # A TUI that never starts is reported, and Enter is pressed only once.
        calls.clear(); entered.clear()
        clock = iter(range(0, 1000))
        with patch.object(h, 'herdr', fake), patch.object(h.time, 'sleep'), \
             patch.object(h.time, 'monotonic', lambda: next(clock)), \
             patch.object(h, 'tui_alive', lambda session: False):
            with self.assertRaisesRegex(ValueError, 'did not start'):
                h.run_tui(owner, {}, '/tmp/s')
        self.assertEqual(sum(c[1] == 'send-keys' for c in calls), 1)

    def test_finish_waits_for_the_latest_round(self):
        import test_grill_ui
        f = test_grill_ui.RoundTests()
        f.setUp()
        try:
            test_grill_ui.answer_all(f.store)
            f.store.submit()
            f.store.add_round(test_grill_ui.next_round())
            # The tab stays for the open round; nothing in Herdr is touched.
            with patch.object(h, 'herdr') as herdr:
                with self.assertRaisesRegex(ValueError, 'latest round'):
                    h.finish(h.argparse.Namespace(session=str(f.root)))
            herdr.assert_not_called()
            # A pending reopen request would be lost; it is sent or cancelled first.
            test_grill_ui.answer_all(f.store)
            f.store.submit()
            f.store.reopen('Q1', 'Новые факты')
            with patch.object(h, 'herdr') as herdr:
                with self.assertRaisesRegex(ValueError, 'Reopen requests'):
                    h.finish(h.argparse.Namespace(session=str(f.root)))
            herdr.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'Reopen requests'):
                f.store.finish()
        finally:
            f.tearDown()


class HerdrAppTests(unittest.TestCase):
    """The Herdr app runs terminals in herdr-core; the window adopts them by their labels."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Path(self.tmp.name).resolve()
        h.write(self.session/'state.json', {'cwd': '/work'})
        self.owner = {'backend': 'hc', 'host': h.socket.gethostname(), 'socket': '/run/hc.sock',
                      'env': {'HC_TERMINAL_SOCKET': '/run/hc.sock'}, 'hc': '', 'workspace': 'sp1',
                      'parent_tab': 'tb1', 'parent_terminal': 't7', 'parent_created': 100}
        self.tui = ['python', str(h.ROOT/'scripts/grill_tui.py'), '--session', str(self.session)]
        self.terminals = {'t7': {'state': 'live', 'created_ms': 100, 'pid': 1,
                                 'labels': {'herdr.space': 'sp1', 'herdr.tab': 'tb1'}}}
        self.calls, self.shown = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def hc(self, args, env=None):
        self.calls.append(args)
        if args[0] == 'get':
            if args[1] not in self.terminals:
                raise ValueError(f'hc get: no terminal "{args[1]}"')
            return self.terminals[args[1]]
        if args[0] == 'run':
            tab = next(a.split('=', 1)[1] for a in args if a.startswith('herdr.tab='))
            self.terminals['t20'] = {'state': 'live', 'pid': 5, 'argv': self.tui,
                                     'labels': {'herdr.space': 'sp1', 'herdr.tab': tab}}
            return {'address': 't20'}
        if args[0] == 'kill':
            self.terminals[args[1]]['state'] = 'finished'
        if args[0] == 'rm':
            self.terminals.pop(args[1], None)
        return {}

    def window(self, args, env):
        self.shown.append(args[1:])
        return True

    @contextlib.contextmanager
    def patched(self, tui=True):
        with patch.object(h, 'hc', self.hc), patch.object(h, 'window', self.window), \
             patch.object(h, 'ensure_server'), patch.object(h, 'tui_alive', lambda s, seen=iter(tui if isinstance(tui, list) else [tui]): next(seen, True)), \
             patch.object(h, 'DATA', self.session/'data'), patch.object(h.time, 'sleep'), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            yield out

    def open(self, focus=False):
        h.hc_open(h.argparse.Namespace(focus=focus), self.session, dict(self.owner))
        return h.read(self.session/'herdr.json')

    def test_herdr_app_terminal_selects_hc_unless_old_herdr_is_inner(self):
        with patch.dict(h.os.environ, {'HC_TERMINAL_ID': 't7'}, clear=True), \
             patch.object(h, 'hc_context', return_value='hc'):
            self.assertEqual(h.context(), 'hc')
        with patch.dict(h.os.environ, {'HC_TERMINAL_ID': 't7', 'HERDR_ENV': '1'}, clear=True), \
             patch.object(h, 'hc_context', return_value='hc'), patch.object(h, 'herdr', side_effect=ValueError('old')):
            with self.assertRaisesRegex(ValueError, 'socket|old'):
                h.context()

    def test_json_flag_never_lands_in_the_program_argv(self):
        done = h.subprocess.CompletedProcess([], 0, stdout='{"address": "t3"}', stderr='')
        with patch.object(h.subprocess, 'run', return_value=done) as run:
            h.hc(['run', '--bg', '--', 'python', 'tui.py'], {'GRILL_HC': '/bin/hc'})
        self.assertEqual(run.call_args.args[0], ['/bin/hc', 'run', '--json', '--bg', '--', 'python', 'tui.py'])
        failed = h.subprocess.CompletedProcess([], 3, stdout='{"error": {"message": "no terminal"}}', stderr='')
        with patch.object(h.subprocess, 'run', return_value=failed):
            with self.assertRaisesRegex(ValueError, 'no terminal'):
                h.hc(['get', 't9'], {'GRILL_HC': '/bin/hc'})

    def test_open_starts_a_labelled_tab_then_reuses_it(self):
        with self.patched():
            owner = self.open(focus=True)
        run = next(c for c in self.calls if c[0] == 'run')
        self.assertIn('--no-parent', run)
        self.assertIn('herdr.space=sp1', run)
        self.assertIn('herdr.tab=' + owner['tab'], run)
        self.assertEqual(run[run.index('--') + 1:], h.tui_command(self.session))
        self.assertEqual(owner['terminal'], 't20')
        self.assertIn(['tab.rename', '--id', owner['tab'], '--name', 'Grill'], self.shown)
        self.assertEqual(self.shown[-1], ['tab.select', '--id', owner['tab']])
        self.calls.clear()
        with self.patched():
            self.assertEqual(self.open(), owner)
        self.assertFalse([c for c in self.calls if c[0] in ('run', 'kill')])

    def test_closed_tab_or_recovered_shell_gets_a_new_tab(self):
        with self.patched():
            first = self.open()
        self.terminals['t20']['state'] = 'finished'
        with self.patched():
            second = self.open()
        self.assertNotEqual(second['tab'], first['tab'])
        self.assertIn(['rm', 't20'], self.calls)
        # After a core restart a shell sits where the TUI was: replace it.
        self.terminals['t20']['foreground'] = {'pid': 5, 'argv': ['zsh']}
        self.calls.clear()
        # The lock is free until the new TUI takes it.
        with self.patched(tui=[False]):
            self.open()
        self.assertIn(['kill', 't20'], self.calls)
        # Anything the owner started there is left alone.
        self.terminals['t20'] = {**self.terminals['t20'], 'state': 'live', 'foreground': {'pid': 9, 'argv': ['vim']}}
        with self.patched(tui=[False]):
            with self.assertRaisesRegex(ValueError, 'another foreground process'):
                self.open()

    def test_foreign_terminal_at_the_recorded_address_is_refused(self):
        with self.patched():
            owner = self.open()
        self.terminals['t20']['labels']['herdr.tab'] = 'tb-other'
        with self.patched():
            with self.assertRaisesRegex(ValueError, 'terminal changed'):
                h.hc_close(owner, self.session)
        self.assertNotIn(['kill', 't20'], self.calls)

    def test_other_space_is_refused(self):
        with self.patched():
            self.open()
            with self.assertRaisesRegex(ValueError, 'different Herdr'):
                h.hc_open(h.argparse.Namespace(focus=False), self.session, {**self.owner, 'workspace': 'sp2'})

    def test_notice_goes_to_the_same_agent_terminal(self):
        h.write(self.session/'herdr.json', self.owner)
        newest = str(self.session/'submissions/0001.json')
        h.write(self.session/'status.json', {'submitted': True, 'result_file': newest})
        h.write(Path(newest), {'round_id': 'r1', 'answers': [], 'reopen': []})
        with self.patched():
            h.notify_agent(self.session)
            h.return_to_agent(self.session)
        self.assertIn(['send', 't7', h.SUBMIT_NOTICE + '. Файл: ' + newest], self.calls)
        self.assertEqual(self.shown, [['space.select', '--id', 'sp1'], ['tab.select', '--id', 'tb1']])
        # Another program now holds the address: no text is typed into it.
        self.terminals['t7']['created_ms'] = 200
        self.calls.clear()
        with self.patched():
            with self.assertRaisesRegex(ValueError, 'terminal is gone'):
                h.notify_agent(self.session)
        self.assertFalse([c for c in self.calls if c[0] == 'send'])

    def test_finish_closes_only_the_grill_terminal(self):
        with self.patched():
            owner = self.open()
            h.hc_close(owner, self.session)
        self.assertEqual([c for c in self.calls if c[0] in ('kill', 'rm')], [['kill', 't20'], ['rm', 't20']])


class HandoffTests(unittest.TestCase):
    """A live handoff keeps panes and processes; a restart kills the TUI."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Path(self.tmp.name).resolve()
        sock = self.session/'herdr.sock'
        sock.touch()
        stat = sock.stat()
        self.identity = [stat.st_dev, stat.st_ino]
        self.owner = {'host': h.socket.gethostname(), 'socket': str(sock),
                      'socket_identity': [stat.st_dev, stat.st_ino+1], 'env': {},
                      'workspace': 'w1', 'tab': 'w1:t2', 'pane': 'w1:p2', 'terminal': 'term-old',
                      'parent_pane': 'w1:p1', 'parent_terminal': 'agent-old', 'parent_shell': 40}
        h.write(self.session/'herdr.json', self.owner)
        # A notice follows a submit, which always leaves the newest result in status.json.
        h.write(self.session/'status.json', {'submitted': True, 'result_file': str(self.session/'submissions/0001.json')})
        h.write(self.session/'submissions/0001.json', {'round_id': 'r1', 'answers': [], 'reopen': []})
        tui =['python', str(h.ROOT/'scripts/grill_tui.py'), '--session', str(self.session)]
        self.processes = {'w1:p2': {'shell_pid': 50, 'foreground_processes': [{'pid': 51, 'argv': tui}]},
                          'w1:p1': {'shell_pid': 40}}
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, args, env=None):
        self.calls.append(args)
        if args[:2] == ['pane', 'list']:
            return {'panes': [{'tab_id': 'w1:t1', 'pane_id': 'w1:p1', 'terminal_id': 'agent-new'},
                              {'tab_id': 'w1:t2', 'pane_id': 'w1:p2', 'terminal_id': 'term-new'}]}
        if args[:2] == ['pane', 'process-info']:
            return {'process_info': self.processes[args[3]]}
        if args[:2] == ['pane', 'get']:
            return {'pane': {'pane_id': 'w1:p1', 'tab_id': 'w1:t1', 'workspace_id': 'w1', 'terminal_id': 'agent-new'}}
        return {}

    @contextlib.contextmanager
    def tui_running(self):
        # A second open file description conflicts with the TUI's flock, as in the real process.
        with (self.session/'tui.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def test_handoff_with_live_tui_rebinds_terminals(self):
        with self.tui_running(), patch.object(h, 'herdr', self.fake):
            h.owner_env(self.owner, self.session)
            saved = h.read(self.session/'herdr.json')
            self.assertEqual(saved['socket_identity'], self.identity)
            self.assertEqual((saved['terminal'], saved['parent_terminal']), ('term-new', 'agent-new'))
            self.calls.clear()
            h.owner_env(saved, self.session)
            self.assertEqual(self.calls, [])

    def test_notice_reaches_agent_after_handoff(self):
        with self.tui_running(), patch.object(h, 'herdr', self.fake), patch.object(h.time, 'sleep'):
            h.notify_agent(self.session)
        self.assertEqual(self.calls[-1], ['pane', 'send-keys', 'w1:p1', 'enter'])

    def test_restart_without_live_tui_is_refused(self):
        with patch.object(h, 'herdr', self.fake):
            with self.assertRaisesRegex(ValueError, 'endpoint changed; refusing'):
                h.owner_env(self.owner, self.session)
            with self.assertRaisesRegex(ValueError, 'endpoint changed; refusing'):
                h.owner_env(self.owner)
        self.assertEqual(self.calls, [])
        self.assertEqual(h.read(self.session/'herdr.json')['terminal'], 'term-old')

    def test_restored_panes_with_new_processes_are_refused(self):
        restored = [('w1:p1', {'shell_pid': 41}, 'new shell'),
                    ('w1:p2', {'shell_pid': 52}, 'does not run this round')]
        for pane, info, reason in restored:
            with self.subTest(reason=reason), self.tui_running(), patch.object(h, 'herdr', self.fake), \
                 patch.dict(self.processes, {pane: info}):
                with self.assertRaisesRegex(ValueError, reason):
                    h.owner_env(dict(self.owner), self.session)
        self.assertEqual(h.read(self.session/'herdr.json')['terminal'], 'term-old')


class VenvTests(unittest.TestCase):
    """The TUI venv is rebuilt when missing or built from other requirements."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)/'grill-ui'
        self.python = data/'venv/bin/python'
        self.patches = [patch.object(h, 'DATA', data), patch.object(h, 'PYTHON', self.python),
                        patch.object(h, 'STAMP', data/'venv/grill-requirements.sha256'),
                        patch.object(h.shutil, 'which', return_value='/usr/bin/uv')]
        for item in self.patches:
            item.start()
        self.steps = []

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def run_step(self, args, **kwargs):
        # Installer output must not mix with the JSON that open prints.
        self.assertIs(kwargs['stdout'], h.sys.stderr)
        self.steps.append(args[:3])
        if args[:2] == ['uv', 'venv']:
            self.python.parent.mkdir(parents=True)
            self.python.touch()

    def test_install_on_missing_or_changed_requirements_only(self):
        with patch.object(h.subprocess, 'run', self.run_step), quiet():
            h.ensure_venv()
            self.assertEqual(self.steps, [['uv', 'venv', '--python'], ['uv', 'pip', 'install']])
            self.assertEqual(h.STAMP.read_text().strip(), h.requirements_digest())
            self.steps.clear()
            h.ensure_venv()
            self.assertEqual(self.steps, [])
            with patch.object(h, 'requirements_digest', return_value='other'):
                h.ensure_venv()
            self.assertEqual(self.steps, [['uv', 'pip', 'install']])

    def test_failed_install_leaves_no_stamp(self):
        def fail(args, **kwargs):
            self.run_step(args, **kwargs)
            if args[1] == 'pip':
                raise h.subprocess.CalledProcessError(1, args)
        with patch.object(h.subprocess, 'run', fail), quiet():
            with self.assertRaisesRegex(ValueError, 'install failed.*grill_herdr.py install'):
                h.ensure_venv()
        self.assertFalse(h.STAMP.exists())

    def test_explicit_install_keeps_stdout_json(self):
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        h.STAMP.write_text(h.requirements_digest())
        out = io.StringIO()
        with patch.object(h.subprocess, 'run', self.run_step), contextlib.redirect_stdout(out):
            h.install(None)
        self.assertEqual(self.steps, [['uv', 'pip', 'install']])
        self.assertEqual(json.loads(out.getvalue()), {'python': str(self.python)})


def quiet():
    return contextlib.redirect_stderr(io.StringIO())


if __name__ == '__main__':
    unittest.main()
