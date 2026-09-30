"""Guard against cross-host/session and modified-tab cleanup."""
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
        finally:
            f.tearDown()


if __name__ == '__main__':
    unittest.main()
