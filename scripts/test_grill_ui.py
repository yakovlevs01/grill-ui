#!/usr/bin/env python3
"""Behavior checks for round boundaries and the agent transports, no model calls."""
import copy
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
import grill_ui as g
import grill_harness as harness


def parentless_env(**extra):
    # Tests may run inside a Codex or Claude Code session; hide that caller.
    env = {k: v for k, v in os.environ.items() if k not in harness.PARENT_ENV}
    env.update(extra)
    return patch.dict(os.environ, env, clear=True)


def fake_cli(folder, name, body):
    cli = Path(folder) / name
    cli.write_text('#!' + sys.executable + '\n' + body)
    cli.chmod(0o700)
    return cli


class RoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.doc = g.read(g.ASSETS / 'example-round.json')
        state = {'round': self.doc, 'runtime': {'model': 'test-model', 'effort': 'high'},
                 'cwd': str(self.root), 'submitted': False,
                 'answers': {q['id']: {'selected': [], 'text': '', 'confirmed': False} for q in self.doc['questions']},
                 'branches': {q['id']: {'thread_id': None, 'messages': [], 'status': 'idle', 'summary': '', 'error': None} for q in self.doc['questions']}}
        g.write(self.root / 'state.json', state)
        self.store = g.Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dependent_question_rejected(self):
        self.doc['questions'][1]['depends_on'] = ['Q1']
        with self.assertRaisesRegex(ValueError, 'dependent'):
            g.validate_round(self.doc)

    def test_answer_contract(self):
        for value in ({'selected': [], 'text': '', 'confirmed': True},
                      {'selected': ['missing'], 'text': ''},
                      {'selected': ['local','sync'], 'text': ''}):
            with self.assertRaises(ValueError):
                self.store.answer('Q1', value)
        self.store.answer('Q2', {'selected': ['title','note'], 'text': 'Both', 'confirmed': True})
        self.store.answer('Q2', {'selected': ['title'], 'text': 'Changed', 'confirmed': False})
        self.assertFalse(self.store.state['answers']['Q2']['confirmed'])

    def test_submit_requires_all_confirmed_and_excludes_chats(self):
        with self.assertRaises(ValueError):
            self.store.submit()
        for qid in self.store.state['answers']:
            self.store.answer(qid, {'selected': [], 'text': 'User answer', 'confirmed': True})
        self.store.state['branches']['Q1']['messages'] = [{'role':'assistant','text':'PRIVATE-TRANSCRIPT'}]
        self.store.state['branches']['Q1']['summary'] = 'UNACCEPTED-SUMMARY'
        result = self.store.submit()
        self.assertNotIn('PRIVATE-TRANSCRIPT', json.dumps(result))
        self.assertNotIn('UNACCEPTED-SUMMARY', json.dumps(result))
        self.assertEqual(len(result['answers']), 3)
        with self.assertRaises(ValueError):
            self.store.answer('Q1', {'selected': [], 'text': 'Rewrite'})

    def test_recovery_and_context_scope(self):
        self.store.state['branches']['Q1']['status'] = 'running'
        self.store.state['branches']['Q2']['messages'] = [{'text':'OTHER-QUESTION-PRIVATE'}]
        self.store.save()
        recovered = g.Store(self.root)
        self.assertEqual(recovered.state['branches']['Q1']['status'], 'error')
        self.assertNotIn('OTHER-QUESTION-PRIVATE', recovered.context('Q1'))

    def test_no_guessed_parent_defaults(self):
        with parentless_env(CODEX_HOME=str(self.root), CLAUDE_CONFIG_DIR=str(self.root)):
            with self.assertRaisesRegex(ValueError, 'Cannot identify'):
                g.resolve_runtime()
            with self.assertRaisesRegex(ValueError, 'Cannot identify'):
                g.resolve_runtime(harness='claude')
            runtime = g.resolve_runtime('exact-model', 'high', harness='codex')
            self.assertEqual((runtime['harness'], runtime['model']), ('codex', 'exact-model'))
        with parentless_env(CODEX_THREAD_ID='a', CLAUDECODE='1'):
            with self.assertRaisesRegex(ValueError, '--harness'):
                g.resolve_runtime()

    def test_claude_parent_from_transcript(self):
        project = self.root / 'projects' / '-work'
        project.mkdir(parents=True)
        lines = [{'type': 'assistant', 'message': {'model': 'claude-test-1'}},
                 {'type': 'assistant', 'message': {'model': '<synthetic>'}}]
        (project / 'abc-123.jsonl').write_text(''.join(json.dumps(l) + '\n' for l in lines))
        with parentless_env(CLAUDE_CONFIG_DIR=str(self.root), CLAUDE_CODE_SESSION_ID='abc-123', CLAUDE_EFFORT='xhigh'):
            runtime = g.resolve_runtime()
        self.assertEqual((runtime['harness'], runtime['model'], runtime['effort']), ('claude', 'claude-test-1', 'xhigh'))
        # Another session's effort is unknown; the env value belongs to this process.
        with parentless_env(CLAUDE_CONFIG_DIR=str(self.root), CLAUDE_CODE_SESSION_ID='other', CLAUDE_EFFORT='xhigh'):
            with self.assertRaisesRegex(ValueError, 'Cannot identify'):
                g.resolve_runtime(parent='abc-123')

    def test_catalog_comes_from_installed_clis(self):
        fake_cli(self.root, 'codex', '''import json
print(json.dumps({'models': [
 {'slug': 'next-model', 'display_name': 'Next', 'visibility': 'list', 'default_reasoning_level': 'medium',
  'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'ultra'}]},
 {'slug': 'hidden', 'visibility': 'hide', 'supported_reasoning_levels': []}]}))
''')
        fake_cli(self.root, 'claude', '''print("  --effort <level>   Effort level\\n        (low, high, turbo)\\n  --model <model>")''')
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}):
            known = harness.catalog()
        self.assertEqual([m['id'] for m in known['codex']['models']], ['next-model'])
        self.assertEqual(known['codex']['models'][0]['efforts'], ['low', 'ultra'])
        self.assertEqual(known['claude']['efforts'], ['low', 'high', 'turbo'])
        self.assertIn('opus', [m['id'] for m in known['claude']['models']])
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}):
            self.assertEqual(harness.validate({'harness': 'codex', 'model': 'next-model', 'effort': 'ultra'}, known)['model'], 'next-model')
            # Unlisted IDs are allowed: a model newer than the catalog still works.
            harness.validate({'harness': 'claude', 'model': 'claude-future-9', 'effort': 'turbo'}, known)
            for bad in ({'harness': 'codex', 'model': 'next-model', 'effort': 'high'},
                        {'harness': 'claude', 'model': '--dangerous', 'effort': 'low'},
                        {'harness': 'other', 'model': 'm', 'effort': 'low'}):
                with self.assertRaises(ValueError):
                    harness.validate(bad, known)

    def test_owner_choice_pins_started_chats(self):
        self.store.state['runtime']['service_tier'] = 'priority'
        self.store.state['parent_runtime']['service_tier'] = 'priority'
        self.store.state['branches']['Q1'].update(thread_id='old-thread', runtime=dict(self.store.state['runtime']),
                                                  messages=[{'role': 'user', 'text': 'x'}])
        for name in ('codex', 'claude'):
            fake_cli(self.root, name, 'pass\n')
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}):
            self.store.set_runtime({'harness': 'claude', 'model': 'opus', 'effort': 'max'})
            runtime = self.store.state['runtime']
            self.assertEqual((runtime['harness'], runtime['source']), ('claude', 'owner choice'))
            self.assertNotIn('service_tier', runtime)
            self.assertEqual(self.store.state['branches']['Q1']['runtime']['harness'], 'codex')
            self.store.set_runtime({'harness': 'codex', 'model': 'test-model', 'effort': 'low'})
            self.assertEqual(self.store.state['runtime']['service_tier'], 'priority')
            self.store.set_runtime({'harness': 'claude', 'model': 'opus', 'effort': 'max'}, 'Q1', restart=True)
        branch = self.store.state['branches']['Q1']
        self.assertEqual((branch['thread_id'], branch['messages'], branch['runtime']), (None, [], None))

    def test_legacy_session_is_codex(self):
        state = g.read(self.root / 'state.json')
        state['branches']['Q1']['thread_id'] = 'legacy'
        for branch in state['branches'].values():
            branch.pop('runtime', None)
        g.write(self.root / 'state.json', state)
        store = g.Store(self.root)
        self.assertEqual(store.state['runtime']['harness'], 'codex')
        self.assertEqual(store.state['branches']['Q1']['runtime']['harness'], 'codex')
        self.assertIsNone(store.state['branches']['Q2']['runtime'])

    def test_http_auth_and_single_server(self):
        proc = subprocess.Popen([sys.executable, str(Path(g.__file__)), 'serve', '--session', str(self.root)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            url = proc.stdout.readline().strip()
            base, token = url.split('/#')
            def request(path, data=None, supplied_token=token, origin=None):
                headers = {'X-Grill-Token': supplied_token, 'Content-Type':'application/json'}
                if origin:
                    headers['Origin'] = origin
                req = urllib.request.Request(base+path, headers=headers,
                    data=json.dumps(data).encode() if data is not None else None)
                with urllib.request.urlopen(req, timeout=3) as response:
                    return response.status, response.read()
            self.assertEqual(request('/api/state')[0], 200)
            for opts in ({'supplied_token':''}, {'origin':'https://foreign.example'}):
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    request('/api/state', **opts)
                self.assertEqual(exc.exception.code, 403)
                exc.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as exc:
                request('/api/submit', {})
            exc.exception.close()
            other = subprocess.run([sys.executable,str(Path(g.__file__)),'serve','--session',str(self.root)],
                                   capture_output=True,text=True,timeout=3)
            self.assertNotEqual(other.returncode, 0)
            self.assertIn('already running', other.stderr)
            for qid in self.store.state['answers']:
                request('/api/answer', {'question_id':qid,'selected':[],'text':'<script>literal</script>','confirmed':True})
            first = json.loads(request('/api/submit', {})[1])
            second = json.loads(request('/api/submit', {})[1])
            self.assertEqual(first, second)
            self.assertEqual(first['answers'][0]['text'], '<script>literal</script>')
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=5)

    def test_activity_phrases_from_cli_events(self):
        codex, claude = harness.HARNESSES['codex'], harness.HARNESSES['claude']
        started = {'type': 'item.started', 'item': {'type': 'command_execution',
                   'command': '/usr/bin/zsh -lc "rg -c \'^##\' SKILL.md"', 'status': 'in_progress'}}
        self.assertEqual(codex.activity(started), "выполняет rg -c '^##' SKILL.md")
        self.assertEqual(codex.activity({'type': 'turn.started'}), 'думает')
        self.assertEqual(codex.activity({'type': 'item.completed', 'item': {'type': 'agent_message'}}), 'пишет ответ')
        self.assertIsNone(codex.activity({'type': 'thread.started'}))
        read = {'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': 'Сейчас посмотрю'},
            {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': '/repo/scripts/grill_tui.py'}}]}}
        self.assertEqual(claude.activity(read), 'читает grill_tui.py')
        self.assertIsNone(claude.activity({'type': 'result'}))

    def test_transport_multiturn_and_failure(self):
        # A fake CLI checks real argv/stdin and persisted branch identity.
        cli = self.root / 'codex'
        cli.write_text('''#!/usr/bin/env python3
import json,sys
from pathlib import Path
prompt=sys.stdin.read()
args=sys.argv[1:]
with Path('calls.jsonl').open('a') as f: f.write(json.dumps({'args':args,'prompt':prompt})+'\\n')
thread=args[args.index('resume')+1] if 'resume' in args else 'isolated-id'
print(json.dumps({'type':'thread.started','thread_id':thread}),flush=True)
if 'FAIL-TEST' in prompt:
 print(json.dumps({'type':'turn.failed','error':{'message':'expected failure'}}));sys.exit(1)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'draft response'}}))
print(json.dumps({'type':'turn.completed'}))
''')
        cli.chmod(0o700)
        def wait_done():
            deadline = time.monotonic() + 5
            while self.store.state['branches']['Q1']['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertNotEqual(self.store.state['branches']['Q1']['status'], 'running')
            with self.store.lock:
                pass  # The status flips before the final save; let it finish.
        with patch.dict(os.environ, {'PATH':str(self.root)+os.pathsep+os.environ['PATH']}):
            self.store.start('Q1', 'Discuss')
            wait_done()
            self.store.start('Q1', 'Follow up')
            wait_done()
            self.store.start('Q1', 'summary', summarize=True)
            wait_done()
            self.assertEqual(self.store.state['branches']['Q1']['summary'], 'draft response')
            self.assertFalse(self.store.state['answers']['Q1']['confirmed'])
            calls = [json.loads(line) for line in (self.root/'calls.jsonl').read_text().splitlines()]
            self.assertNotIn('resume', calls[0]['args'])
            self.assertIn('resume', calls[1]['args'])
            self.assertIn('isolated-id', calls[1]['args'])
            for call in calls:
                self.assertIn('test-model', call['args'])
                self.assertIn('model_reasoning_effort="high"', call['args'])
                self.assertIn('sandbox_mode="read-only"', call['args'])
            self.store.start('Q1', 'FAIL-TEST')
            wait_done()
            self.assertEqual(self.store.state['branches']['Q1']['status'], 'error')

    def test_claude_transport_multiturn_and_failure(self):
        fake_cli(self.root, 'claude', '''import json,os,sys
from pathlib import Path
prompt=sys.stdin.read(); args=sys.argv[1:]
with Path('calls.jsonl').open('a') as f:
    f.write(json.dumps({'args':args,'prompt':prompt,'nested':'CLAUDECODE' in os.environ})+'\\n')
session=args[args.index('--resume')+1] if '--resume' in args else 'claude-session'
print(json.dumps({'type':'system','subtype':'hook_started','session_id':session}),flush=True)
print(json.dumps({'type':'system','subtype':'init','session_id':session}),flush=True)
if 'FAIL-TEST' in prompt:
    print(json.dumps({'type':'result','is_error':True,'session_id':session,'result':'model unavailable'}));sys.exit(1)
print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'interim'}]}}))
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':session,'result':'claude answer'}))
''')
        def wait_done():
            deadline = time.monotonic() + 5
            while self.store.state['branches']['Q1']['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertNotEqual(self.store.state['branches']['Q1']['status'], 'running')
            with self.store.lock:
                pass  # The status flips before the final save; let it finish.
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH'], 'CLAUDECODE': '1'}):
            self.store.set_runtime({'harness': 'claude', 'model': 'opus', 'effort': 'high'})
            self.store.start('Q1', 'Discuss')
            wait_done()
            self.store.start('Q1', 'Follow up')
            wait_done()
            branch = self.store.state['branches']['Q1']
            self.assertEqual(branch['thread_id'], 'claude-session')
            self.assertEqual(branch['messages'][-1], {'role': 'assistant', 'text': 'claude answer'})
            calls = [json.loads(line) for line in (self.root/'calls.jsonl').read_text().splitlines()]
            self.assertNotIn('--resume', calls[0]['args'])
            self.assertEqual(calls[1]['args'][calls[1]['args'].index('--resume') + 1], 'claude-session')
            for call in calls:
                self.assertFalse(call['nested'])
                self.assertIn('Discuss' if call is calls[0] else 'Follow up', call['prompt'])
                for flag in ('opus', 'high', 'Read,Grep,Glob', '--strict-mcp-config', '--disable-slash-commands'):
                    self.assertIn(flag, call['args'])
            self.store.start('Q1', 'FAIL-TEST')
            wait_done()
            self.assertEqual((branch['status'], branch['error']), ('error', 'model unavailable'))


if __name__ == '__main__':
    unittest.main()
