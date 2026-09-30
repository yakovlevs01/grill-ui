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


def next_round(round_id='r2', ids=('Q4', 'Q5'), depends_on=('Q1',)):
    """A follow-up round; its questions may depend on answers of earlier rounds."""
    return {'id': round_id, 'title': 'Уточнения', 'goal': 'Уточнить хранение.', 'context': 'После первого раунда.',
            'questions': [{'id': qid, 'title': 'Вопрос ' + qid, 'body': 'Тело ' + qid, 'recommendation': 'Совет',
                           'context': 'Факты ' + qid, 'mode': 'text', 'depends_on': list(depends_on)} for qid in ids]}


def answer_all(store, text='Ответ'):
    for qid in [q['id'] for q in store.state['rounds'][-1]['questions']]:
        store.answer(qid, {'selected': [], 'text': text, 'confirmed': True})


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
        g.write(self.root / 'state.json', g.new_state(self.doc, {'model': 'test-model', 'effort': 'high'}, str(self.root)))
        self.store = g.Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dependent_question_rejected(self):
        self.doc['questions'][1]['depends_on'] = ['Q1']
        with self.assertRaisesRegex(ValueError, 'dependent'):
            g.validate_round(self.doc)

    def test_recommended_must_name_options(self):
        g.validate_round(self.doc)
        for value in (['missing'], ['local', 'sync'], 'local'):
            self.doc['questions'][0]['recommended'] = value
            with self.assertRaisesRegex(ValueError, '(?i)recommend'):
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
            self.assertEqual(first['answers'][0]['text'], '<script>literal</script>')
            self.assertEqual(Path(first['file']), self.root.resolve() / 'submissions/0001.json')
            # Nothing new to send: a second press must not produce a second result.
            with self.assertRaises(urllib.error.HTTPError) as exc:
                request('/api/submit', {})
            exc.exception.close()
            # add-round reaches the live server, so the open TUI switches without a restart.
            doc = self.root / 'r2.json'
            g.write(doc, next_round())
            added = subprocess.run([sys.executable, str(Path(g.__file__)), 'add-round', '--session', str(self.root),
                                    '--round', str(doc)], capture_output=True, text=True, timeout=10)
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertTrue(json.loads(added.stdout)['live'])
            state = json.loads(request('/api/state')[1])
            self.assertEqual([r['id'] for r in state['rounds']], ['demo-r1', 'r2'])
            self.assertEqual(state['numbers']['Q4'], 4)
            # A TUI holding the current version gets only a mark, not the whole grill.
            unchanged = json.loads(request('/api/state?since=' + state['version'])[1])
            self.assertEqual(unchanged, {'unchanged': True, 'version': state['version']})
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=5)

    def test_state_version_changes_with_every_visible_change(self):
        first = self.store.snapshot()
        self.assertEqual(self.store.snapshot(first['version']), {'unchanged': True, 'version': first['version']})
        self.store.answer('Q1', {'selected': ['local'], 'text': '', 'confirmed': False})
        second = self.store.snapshot(first['version'])
        self.assertNotIn('unchanged', second)
        self.assertEqual(second['answers']['Q1']['selected'], ['local'])
        # A restarted server never repeats a version the TUI holds.
        self.assertNotEqual(g.Store(self.root).snapshot()['version'], second['version'])

    def test_rounds_share_one_session_with_global_numbers(self):
        with self.assertRaisesRegex(ValueError, 'Submit the current round'):
            self.store.add_round(next_round())
        # The owner's agent choice (F5) must survive new rounds.
        self.store.state['runtime'].update(model='owner-pick', source='owner choice')
        answer_all(self.store)
        first = self.store.submit()
        for doc, message in ((next_round('demo-r1', ('Q9',)), 'Duplicate round'),
                             (next_round(ids=('Q2',)), 'unique across the grill'),
                             (next_round(depends_on=('Q4',)), 'dependent')):
            with self.assertRaisesRegex(ValueError, message):
                self.store.add_round(doc)
        self.store.add_round(next_round())
        self.assertEqual(self.store.state['runtime']['model'], 'owner-pick')
        self.assertEqual(self.store.snapshot()['numbers'], {'Q1': 1, 'Q2': 2, 'Q3': 3, 'Q4': 4, 'Q5': 5})
        self.assertIn('После первого раунда', self.store.context('Q4'))
        self.assertIn('Бюджет времени', self.store.context('Q1'))
        with self.assertRaisesRegex(ValueError, 'already submitted'):
            self.store.answer('Q1', {'selected': [], 'text': 'Позже', 'confirmed': True})
        answer_all(self.store, 'Второй')
        second = self.store.submit()
        # Each submit is its own file with only its own round; earlier files stay as they were.
        self.assertEqual([a['question_id'] for a in second['answers']], ['Q4', 'Q5'])
        self.assertEqual([a['number'] for a in second['answers']], [4, 5])
        self.assertEqual(g.read(first['file'])['round_id'], 'demo-r1')
        self.assertEqual(Path(second['file']).name, '0002.json')
        status = g.read(self.root / 'status.json')
        self.assertEqual((status['round_id'], status['submitted'], status['waiting'], status['result_file']),
                         ('r2', True, True, second['file']))

    def test_reopen_goes_with_next_round_or_alone(self):
        with self.assertRaisesRegex(ValueError, 'sent answer'):
            self.store.reopen('Q1', 'Передумал')
        answer_all(self.store)
        self.store.submit()
        with self.assertRaisesRegex(ValueError, 'Explain'):
            self.store.reopen('Q1', '  ')
        self.store.reopen('Q1', 'Нужен телефон')
        self.store.reopen('Q1', None)
        self.assertEqual(self.store.state['reopen'], {})
        self.store.reopen('Q2', ' Теги всё-таки нужны ')
        self.assertEqual(g.read(self.root / 'status.json')['reopen'], 1)
        self.store.add_round(next_round())
        answer_all(self.store)
        with_round = self.store.submit()
        self.assertEqual(with_round['reopen'], [{'question_id': 'Q2', 'number': 2, 'round_id': 'demo-r1',
                                                 'reason': 'Теги всё-таки нужны'}])
        self.assertEqual(self.store.state['reopen'], {})
        with self.assertRaisesRegex(ValueError, 'Nothing to submit'):
            self.store.submit()
        # While the agent prepares the next round, the requests go alone.
        self.store.reopen('Q4', 'Новые факты')
        alone = self.store.submit()
        self.assertEqual((alone['round_id'], alone['answers'], [r['number'] for r in alone['reopen']]), (None, [], [4]))
        self.assertEqual(Path(alone['file']).name, '0003.json')
        self.assertEqual(g.read(with_round['file'])['reopen'][0]['question_id'], 'Q2')

    def test_past_chats_continue_until_finish(self):
        answer_all(self.store)
        self.store.submit()
        self.store.draft('Q1', 'Ещё вопрос')
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}):
            fake_cli(self.root, 'codex', 'pass\n')
            self.store.set_runtime({'harness': 'codex', 'model': 'test-model', 'effort': 'low'}, 'Q1', restart=True)
        self.store.finish()
        for action in (lambda: self.store.draft('Q1', 'x'), lambda: self.store.start('Q1', 'x'),
                       lambda: self.store.reopen('Q1', 'x'), lambda: self.store.add_round(next_round())):
            with self.assertRaisesRegex(ValueError, 'finished'):
                action()
        self.assertFalse(g.read(self.root / 'status.json')['waiting'])

    def test_single_round_session_migrates(self):
        state = g.read(self.root / 'state.json')
        doc = state.pop('rounds')[0]
        for key in ('submitted_rounds', 'submissions', 'reopen', 'finished'):
            state.pop(key)
        state.update(round=doc, submitted=True)
        g.write(self.root / 'state.json', state)
        g.write(self.root / 'answers.json', {'round_id': doc['id'], 'submitted_at': 0, 'answers': []})
        status = subprocess.run([sys.executable, str(Path(g.__file__)), 'status', '--session', str(self.root)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(status.stdout)['result_file'], str(self.root.resolve() / 'answers.json'))
        doc2 = self.root / 'r2.json'
        g.write(doc2, next_round())
        # No server runs: add-round updates the state under the session lock.
        added = subprocess.run([sys.executable, str(Path(g.__file__)), 'add-round', '--session', str(self.root),
                                '--round', str(doc2)], capture_output=True, text=True, timeout=10)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertFalse(json.loads(added.stdout)['live'])
        store = g.Store(self.root)
        self.assertEqual((store.state['submitted_rounds'], [r['id'] for r in store.state['rounds']]),
                         (['demo-r1'], ['demo-r1', 'r2']))
        answer_all(store)
        self.assertEqual(Path(store.submit()['file']).name, '0002.json')

    def test_offline_update_refused_while_a_server_holds_the_session(self):
        with (self.root / 'server.lock').open('a') as lock:
            g.fcntl.flock(lock, g.fcntl.LOCK_EX)
            with self.assertRaisesRegex(ValueError, 'does not answer'):
                with g.offline(self.root):
                    pass

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
        search = {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'WebSearch', 'input': {'query': 'python release'}}]}}
        self.assertEqual(claude.activity(search), 'ищет в интернете «python release»')
        writing = {'type': 'stream_event', 'event': {'type': 'content_block_start', 'index': 1,
                                                      'content_block': {'type': 'text', 'text': ''}}}
        self.assertEqual(claude.activity(writing), 'пишет ответ')
        self.assertIsNone(claude.activity({'type': 'result'}))

    def test_partial_text_from_cli_events(self):
        codex, claude = harness.HARNESSES['codex'], harness.HARNESSES['claude']
        message = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'второе'}}
        self.assertEqual(codex.partial(message, ''), 'второе')
        self.assertEqual(codex.partial(message, 'первое'), 'первое\n\nвторое')
        self.assertIsNone(codex.partial({'type': 'item.started', 'item': {'type': 'web_search'}}, 'x'))
        # Shapes as Claude Code 2.1.285 prints them with --include-partial-messages.
        def stream(inner, parent=None):
            return {'type': 'stream_event', 'event': inner, 'session_id': 's', 'parent_tool_use_id': parent}
        delta = stream({'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': ' мир'}})
        block = stream({'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}})
        self.assertEqual(claude.partial(delta, 'Привет'), 'Привет мир')
        self.assertIsNone(claude.partial(block, ''))
        self.assertEqual(claude.partial(block, 'Сначала'), 'Сначала\n\n')
        for other in (stream({'type': 'content_block_delta', 'delta': {'type': 'thinking_delta', 'thinking': 'x'}}),
                      stream({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'x'}}, 'toolu_1'),
                      {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'x'}]}}):
            self.assertIsNone(claude.partial(other, 'y'))

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
                self.assertIn('web_search="live"', call['args'])
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
                for flag in ('opus', 'high', '--include-partial-messages', 'Read,Grep,Glob,WebSearch,WebFetch',
                             '--strict-mcp-config', '--disable-slash-commands'):
                    self.assertIn(flag, call['args'])
            self.store.start('Q1', 'FAIL-TEST')
            wait_done()
            self.assertEqual((branch['status'], branch['error']), ('error', 'model unavailable'))

    def test_reply_streams_while_the_turn_runs(self):
        # The fake prints part of a reply, then waits for the test to let it finish.
        fake_cli(self.root, 'claude', '''import json,sys,time
from pathlib import Path
prompt=sys.stdin.read()
def say(event): print(json.dumps(event),flush=True)
say({'type':'system','subtype':'init','session_id':'streamed'})
say({'type':'stream_event','event':{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}})
for piece in 'Первая часть':
    say({'type':'stream_event','event':{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':piece}}})
while not Path('go').exists(): time.sleep(.02)
if 'STOP-TEST' in prompt: time.sleep(30)
say({'type':'result','subtype':'success','is_error':False,'session_id':'streamed','result':'Первая часть и конец'})
''')
        fake_cli(self.root, 'codex', '''import json,sys,time
from pathlib import Path
sys.stdin.read()
def say(event): print(json.dumps(event),flush=True)
say({'type':'thread.started','thread_id':'codex-streamed'})
say({'type':'item.completed','item':{'type':'agent_message','text':'Сначала поищу'}})
while not Path('go').exists(): time.sleep(.02)
say({'type':'item.completed','item':{'type':'agent_message','text':'Ответ'}})
''')
        branch = self.store.state['branches']['Q1']
        def wait(check):
            deadline = time.monotonic() + 5
            while not check() and time.monotonic() < deadline:
                time.sleep(.02)
            with self.store.lock:
                self.assertTrue(check())
        writes = []
        def counted(path, data):
            writes.append(Path(path).name)
            real_write(path, data)
        def turn(message, streamed, stop=False):
            (self.root / 'go').unlink(missing_ok=True)
            writes.clear()
            self.store.start('Q1', message)
            wait(lambda: branch['partial'] == streamed)
            self.assertEqual(branch['status'], 'running')
            # The file catches up between deltas without a write per delta.
            wait(lambda: g.read(self.root / 'state.json')['branches']['Q1']['partial'] == streamed)
            self.assertLessEqual(writes.count('state.json'), 4)
            (self.root / 'go').touch()
            if stop:
                self.store.stop('Q1')
            wait(lambda: branch['status'] != 'running')
        real_write = g.write
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}), \
                patch.object(g, 'write', counted):
            self.store.set_runtime({'harness': 'claude', 'model': 'opus', 'effort': 'high'})
            turn('Discuss', 'Первая часть')
            self.assertEqual((branch['messages'][-1]['text'], branch['partial'], branch['status']),
                             ('Первая часть и конец', '', 'idle'))
            # A stopped turn keeps what it streamed.
            turn('STOP-TEST', 'Первая часть', stop=True)
            self.assertEqual((branch['messages'][-1]['text'], branch['partial'], branch['status']),
                             ('Первая часть', '', 'error'))
            self.store.set_runtime({'harness': 'codex', 'model': 'test-model', 'effort': 'high'}, 'Q1', restart=True)
            turn('Discuss', 'Сначала поищу')
            self.assertEqual(branch['messages'][-1]['text'], 'Сначала поищу\n\nОтвет')

    def test_restart_keeps_streamed_text(self):
        self.store.state['branches']['Q1'].update(status='running', partial='Недописанный ответ',
                                                  messages=[{'role': 'user', 'text': 'Вопрос'}])
        self.store.save()
        branch = g.Store(self.root).state['branches']['Q1']
        self.assertEqual((branch['messages'][-1], branch['partial'], branch['status']),
                         ({'role': 'assistant', 'text': 'Недописанный ответ'}, '', 'error'))


if __name__ == '__main__':
    unittest.main()
