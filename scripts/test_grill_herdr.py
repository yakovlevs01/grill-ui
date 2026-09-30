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
