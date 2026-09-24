"""User-flow checks without paid model calls; real Textual widgets and Store."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from textual.widgets import Button, Checkbox, Input, Select, TextArea
import grill_ui as g
from grill_tui import AgentScreen, ChoiceList, GrillApp, CUSTOM
import test_grill_ui

CATALOG = {'ready': True, 'harnesses': {
    'codex': {'label': 'Codex', 'available': True, 'efforts': ['low', 'high'],
              'models': [{'id': 'test-model', 'label': 'Test', 'efforts': ['low', 'high'], 'default_effort': 'low'}]},
    'claude': {'label': 'Claude Code', 'available': True, 'efforts': ['low', 'high', 'max'],
               'models': [{'id': 'opus', 'label': 'opus', 'efforts': ['low', 'high', 'max'], 'default_effort': None}]}}}


class LocalAPI:
    def __init__(self, store):
        self.store = store
        self.offline = False

    def request(self, path, data=None):
        if self.offline:
            raise OSError('offline')
        qid = (data or {}).get('question_id')
        if path == '/api/state':
            return copy.deepcopy(self.store.state)
        if path == '/api/answer':
            self.store.answer(qid, data)
        elif path == '/api/draft':
            self.store.draft(qid, data['text'])
        elif path == '/api/submit':
            return self.store.submit()
        elif path == '/api/catalog':
            return copy.deepcopy(CATALOG)
        elif path == '/api/runtime':
            self.store.set_runtime(data['runtime'], qid, data.get('restart') is True)
        elif path == '/api/chat':
            branch = self.store.state['branches'][qid]
            branch['messages'].extend([{'role':'user','text': data['message']},
                                       {'role':'assistant','text':'Отдельный ответ ' + qid}])
            if data.get('summarize'):
                branch['summary'] = 'Черновик ' + qid
            self.store.save()
        return {'ok':True}


class UITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = test_grill_ui.RoundTests()
        self.fixture.setUp()
        self.store = self.fixture.store
        self.api = LocalAPI(self.store)
        for name in ('codex', 'claude'):
            test_grill_ui.fake_cli(self.fixture.root, name, 'pass\n')
        self.path = patch.dict(os.environ, {'PATH': str(self.fixture.root) + os.pathsep + os.environ['PATH']})
        self.path.start()

    def tearDown(self):
        self.path.stop()
        self.fixture.tearDown()

    async def test_agent_picker_sets_new_chats_and_restarts_on_request(self):
        self.store.state['branches']['Q2'].update(thread_id='codex-thread', runtime=dict(self.store.state['runtime']),
                                                  messages=[{'role': 'user', 'text': 'Старый чат'}])
        app = GrillApp(self.fixture.root, self.api)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            self.assertIn('Codex · test-model · high', str(app.query_one('#agent').label))
            await pilot.press('f5')
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, AgentScreen)
            self.assertEqual(len(screen.query(Checkbox)), 0)
            screen.query_one('#agent-harness', Select).value = 'claude'
            await pilot.pause()
            self.assertEqual(screen.query_one('#agent-model', Select).value, 'opus')
            self.assertEqual(screen.query_one('#agent-effort', Select).value, 'high')
            # A model newer than the catalog is typed in by hand.
            screen.query_one('#agent-model', Select).value = CUSTOM
            await pilot.pause()
            self.assertTrue(screen.query_one('#agent-custom').display)
            screen.query_one('#agent-custom', Input).value = 'claude-future-9'
            screen.query_one('#agent-effort', Select).value = 'max'
            await pilot.click('#agent-apply')
            await pilot.pause()
            self.assertNotIsInstance(app.screen, AgentScreen)
            runtime = self.store.state['runtime']
            self.assertEqual((runtime['harness'], runtime['model'], runtime['effort']), ('claude', 'claude-future-9', 'max'))
            self.assertIn('Claude Code · claude-future-9 · max', str(app.query_one('#agent').label))
            # A started chat keeps its agent until the owner restarts it.
            listing = app.query_one('#question-list')
            listing.focus()
            await pilot.press('down', 'enter')
            await pilot.pause()
            self.assertIn('Codex · test-model · high', str(app.query_one('#agent').label))
            await pilot.click('#agent')
            await pilot.pause()
            screen = app.screen
            self.assertEqual(screen.query_one('#agent-harness', Select).value, 'claude')
            await pilot.click('#agent-cancel')
            await pilot.pause()
            self.assertEqual(self.store.state['branches']['Q2']['thread_id'], 'codex-thread')
            await pilot.press('f5')
            await pilot.pause()
            app.screen.query_one('#agent-restart', Checkbox).value = True
            await pilot.click('#agent-apply')
            await pilot.pause()
            branch = self.store.state['branches']['Q2']
            self.assertEqual((branch['thread_id'], branch['messages']), (None, []))
            self.assertIn('Claude Code · claude-future-9 · max', str(app.query_one('#agent').label))

    async def test_round_clicks_multiline_chat_and_submit(self):
        app = GrillApp(self.fixture.root, self.api)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            # Mouse selects an option, keyboard edits multiline Russian text.
            await pilot.click('#choices', offset=(3, 0))
            await pilot.click('#answer')
            await pilot.press('П','р','и','в','е','т','shift+enter','М','и','р','ctrl+j','!')
            await pilot.pause()
            self.assertEqual(app.query_one('#answer', TextArea).text, 'Привет\nМир\n!')
            self.assertFalse(self.store.state['answers']['Q1']['confirmed'])
            await pilot.click('#confirm')
            await pilot.pause()
            self.assertTrue(self.store.state['answers']['Q1']['confirmed'])
            self.assertEqual(self.store.state['answers']['Q1']['selected'], ['local'])
            app.query_one('#message', TextArea).load_text('Обсудим Q1')
            await pilot.pause()
            await pilot.click('#send')
            await pilot.pause()
            self.assertEqual(len(self.store.state['branches']['Q1']['messages']), 2)
            # Switch using the list, preserve the first answer and isolate chats.
            listing = app.query_one('#question-list')
            listing.focus()
            await pilot.press('down', 'enter')
            await pilot.pause()
            self.assertEqual(app.q['id'], 'Q2')
            self.assertEqual(app.query_one('#message', TextArea).text, '')
            app.query_one('#answer', TextArea).load_text('Название и заметку')
            await pilot.pause()
            await pilot.click('#confirm')
            listing.focus()
            await pilot.press('down', 'enter')
            await pilot.pause()
            app.query_one('#answer', TextArea).load_text('Вернуться к трём статьям')
            await pilot.pause()
            await pilot.click('#confirm')
            await pilot.click('#submit')
            await pilot.pause()
            result = g.read(self.fixture.root/'answers.json')
            self.assertEqual(len(result['answers']), 3)
            self.assertNotIn('Отдельный ответ', str(result))
            self.assertNotIn('question', result['answers'][0])
            self.assertTrue(app.query_one('#answer').disabled)
            back = app.query_one('#return', Button)
            self.assertIs(app.focused, back)
            self.assertEqual(back.variant, 'success')
            self.assertTrue(any('Готово' in str(n.message) for n in app._notifications))

    async def test_f4_appends_and_f6_moves_to_chat_without_touching_answer(self):
        app = GrillApp(self.fixture.root, self.api)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            app.query_one('#answer', TextArea).load_text('Мой ')
            await pilot.press('f4', 'О', 'т', 'в', 'е', 'т')
            await pilot.press('f6', 'Ч', 'а', 'т')
            await pilot.pause()
            self.assertEqual(app.query_one('#answer', TextArea).text, 'Мой Ответ')
            self.assertEqual(app.query_one('#message', TextArea).text, 'Чат')
            await pilot.press('f5')
            await pilot.pause()
            self.assertIsInstance(app.screen, AgentScreen)
            await pilot.press('f2', 'f3', 'f4', 'f5', 'f6')
            await pilot.pause()
            self.assertIsInstance(app.screen, AgentScreen)

    async def test_enter_confirms_answer_and_sends_message(self):
        app = GrillApp(self.fixture.root, self.api)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            await pilot.press('f4', 'Д', 'а', 'enter')
            await pilot.pause()
            self.assertEqual(self.store.state['answers']['Q1'], {'selected': [], 'text': 'Да', 'confirmed': True})
            self.assertIs(app.focused, app.query_one('#answer'))
            await pilot.press('f6', 'В', 'о', 'п', 'р', 'о', 'с', 'enter')
            await pilot.pause()
            messages = self.store.state['branches']['Q1']['messages']
            self.assertEqual(messages[0], {'role': 'user', 'text': 'Вопрос'})
            self.assertEqual(app.query_one('#message', TextArea).text, '')
            self.assertEqual(len(app.query('.msg')), 2)
            self.assertEqual(len(app.query('.msg-user')), 1)

    async def test_submit_types_notice_into_agent_pane(self):
        import grill_herdr
        for qid in self.store.state['answers']:
            self.store.answer(qid, {'selected': [], 'text': 'Ответ', 'confirmed': True})
        g.write(self.fixture.root/'herdr.json', {})
        for effect, expected in ((None, 'агент получил «Готово»'),
                                 (ValueError('Original agent terminal is gone'), 'Не удалось написать агенту')):
            self.store.state['submitted'] = False
            app = GrillApp(self.fixture.root, self.api)
            with patch.object(grill_herdr, 'notify_agent', side_effect=effect) as notify:
                async with app.run_test(size=(150, 45)) as pilot:
                    await pilot.pause()
                    await pilot.click('#submit')
                    await pilot.pause()
                    notify.assert_called_once_with(self.fixture.root.resolve())
                    self.assertIn(expected, app.submit_notice)
                    self.assertIs(app.focused, app.query_one('#return', Button))

    async def test_offline_recovery_switching_and_narrow_layout(self):
        app = GrillApp(self.fixture.root, self.api)
        async with app.run_test(size=(85, 32)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one('#discussion').display)
            self.api.offline = True
            app.query_one('#answer', TextArea).load_text('Несохранённый\nрусский текст')
            await pilot.pause()
            await app.tick()
            self.assertTrue(app.pending_file.exists())
            app.action_discussion()
            self.assertTrue(app.query_one('#discussion').display)
        self.api.offline = False
        restored = GrillApp(self.fixture.root, self.api)
        async with restored.run_test(size=(150,45)) as pilot:
            await pilot.pause()
            self.assertEqual(restored.query_one('#answer', TextArea).text, 'Несохранённый\nрусский текст')
            await restored.flush()
            self.assertEqual(self.store.state['answers']['Q1']['text'], 'Несохранённый\nрусский текст')
            await pilot.resize_terminal(75, 28)
            await pilot.pause()
            self.assertTrue(restored.query_one('#center').display)
            self.assertFalse(restored.query_one('#questions').display)
            self.assertFalse(restored.query_one('#discussion').display)

    async def test_single_selection_edit_unconfirms_and_summary_is_explicit(self):
        app = GrillApp(self.fixture.root, self.api)
        async with app.run_test(size=(150,45)) as pilot:
            await pilot.pause()
            choices = app.query_one('#choices', ChoiceList)
            await pilot.click('#choices', offset=(3, 0))
            await pilot.press('down','space')
            await pilot.pause()
            self.assertEqual(choices.selected, ['sync'])
            await app.action_confirm()
            app.query_one('#answer', TextArea).load_text('С оговоркой')
            await pilot.pause()
            self.assertFalse(app.state['answers']['Q1']['confirmed'])
            app.query_one('#message', TextArea).load_text('Помоги')
            await pilot.pause()
            await pilot.click('#send')
            await pilot.click('#summarize')
            await pilot.pause()
            self.assertEqual(app.query_one('#answer', TextArea).text, 'С оговоркой')
            await pilot.click('#insert')
            await pilot.pause()
            self.assertEqual(app.query_one('#answer', TextArea).text, 'С оговоркой\n\nЧерновик Q1')
            self.assertFalse(self.store.state['submitted'])


if __name__ == '__main__':
    unittest.main()
