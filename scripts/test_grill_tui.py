"""User-flow checks without paid model calls; real Textual widgets and Store."""
import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from rich.console import Console
from textual.widgets import Button, Input, Select, TextArea
import grill_ui as g
from grill_tui import ChoiceList, GrillApp, CUSTOM
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
            harness, model, effort = (app.query_one(f'#agent-{n}', Select) for n in ('harness', 'model', 'effort'))
            self.assertEqual((harness.value, model.value, effort.value), ('codex', 'test-model', 'high'))
            self.assertFalse(app.query_one('#agent-chat').display)
            await pilot.press('f5')
            await pilot.pause()
            self.assertIs(app.focused, harness)
            harness.value = 'claude'
            await pilot.pause()
            self.assertEqual((model.value, effort.value), ('opus', 'high'))
            self.assertEqual(self.store.state['runtime']['model'], 'opus')
            # A model newer than the catalog is typed in by hand.
            model.value = CUSTOM
            await pilot.pause()
            self.assertTrue(app.query_one('#agent-custom').display)
            self.assertEqual(self.store.state['runtime']['model'], 'opus')
            app.query_one('#agent-custom', Input).value = 'claude-future-9'
            effort.value = 'max'
            await pilot.pause()
            runtime = self.store.state['runtime']
            self.assertEqual((runtime['harness'], runtime['model'], runtime['effort']), ('claude', 'claude-future-9', 'max'))
            # A started chat keeps its agent until the owner restarts it.
            listing = app.query_one('#question-list')
            listing.focus()
            await pilot.press('down', 'enter')
            await pilot.pause()
            self.assertTrue(app.query_one('#agent-chat').display)
            self.assertIn('Codex · test-model · high', str(app.query_one('#agent-note').render()))
            self.assertEqual(harness.value, 'claude')
            self.assertEqual(self.store.state['branches']['Q2']['thread_id'], 'codex-thread')
            await pilot.click('#agent-restart')
            await pilot.pause()
            branch = self.store.state['branches']['Q2']
            self.assertEqual((branch['thread_id'], branch['messages']), (None, []))
            self.assertFalse(app.query_one('#agent-chat').display)

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
            self.assertIs(app.focused, app.query_one('#agent-harness', Select))
            await pilot.press('f6')
            await pilot.pause()
            self.assertIs(app.focused, app.query_one('#message', TextArea))

    async def test_enter_confirms_advances_and_sends_message(self):
        app = GrillApp(self.fixture.root, self.api)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            await pilot.press('f4', 'enter')
            await pilot.pause()
            self.assertEqual((app.q['id'], self.store.state['answers']['Q1']['confirmed']), ('Q1', False))
            await pilot.press('Д', 'а', 'enter')
            await pilot.pause()
            self.assertEqual(self.store.state['answers']['Q1'], {'selected': [], 'text': 'Да', 'confirmed': True})
            self.assertEqual(app.q['id'], 'Q2')
            self.assertIs(app.focused, app.query_one('#choices'))
            # Enter marks the highlighted option; Enter on a marked one confirms.
            await pilot.press('enter', 'down', 'enter')
            await pilot.pause()
            self.assertEqual(app.state['answers']['Q2'], {'selected': ['title', 'note'], 'text': '', 'confirmed': False})
            self.assertEqual(app.q['id'], 'Q2')
            await pilot.press('enter')
            await pilot.pause()
            self.assertEqual(self.store.state['answers']['Q2'], {'selected': ['title', 'note'], 'text': '', 'confirmed': True})
            self.assertEqual(app.q['id'], 'Q3')
            self.assertIs(app.focused, app.query_one('#answer'))
            await pilot.press('shift+enter', 'О', 'К', 'enter')
            await pilot.pause()
            self.assertEqual(self.store.state['answers']['Q3'], {'selected': [], 'text': '\nОК', 'confirmed': True})
            self.assertIs(app.focused, app.query_one('#submit'))
            await pilot.press('f6', 'В', 'о', 'п', 'р', 'о', 'с', 'enter')
            await pilot.pause()
            messages = self.store.state['branches']['Q3']['messages']
            self.assertEqual(messages[0], {'role': 'user', 'text': 'Вопрос'})
            self.assertEqual(app.query_one('#message', TextArea).text, '')
            self.assertEqual(len(app.query('.msg')), 2)
            self.assertEqual(len(app.query('.msg-user')), 1)

    async def test_f2_and_f7_hide_panels_until_pressed_again(self):
        app = GrillApp(self.fixture.root, self.api)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            await pilot.press('f2', 'f7')
            await pilot.pause()
            app.apply_layout()  # Any redraw must keep the owner's choice.
            self.assertFalse(app.query_one('#questions').display)
            self.assertFalse(app.query_one('#discussion').display)
            self.assertTrue(app.query_one('#center').display)
            await pilot.press('f6')
            await pilot.pause()
            self.assertTrue(app.query_one('#discussion').display)
            await pilot.press('f2')
            await pilot.pause()
            self.assertTrue(app.query_one('#questions').display)

    async def test_submit_types_notice_into_agent_pane(self):
        import grill_herdr
        for qid in self.store.state['answers']:
            self.store.answer(qid, {'selected': [], 'text': 'Ответ', 'confirmed': True})
        g.write(self.fixture.root/'herdr.json', {})
        for effect, expected in ((None, 'агент получил «Готово»'),
                                 (ValueError('Original agent terminal is gone'), 'Не удалось написать агенту')):
            self.store.state['submitted'] = False
            app = GrillApp(self.fixture.root, self.api)
            with patch.object(grill_herdr, 'notify_agent', side_effect=effect) as notify, \
                 patch.object(grill_herdr, 'return_to_agent') as back:
                async with app.run_test(size=(150, 45)) as pilot:
                    await pilot.pause()
                    await pilot.click('#submit')
                    await pilot.pause()
                    notify.assert_called_once_with(self.fixture.root.resolve())
                    back.assert_called_once_with(self.fixture.root.resolve())
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

    async def test_arrows_and_enter_pick_single_option_and_recommendation_is_a_tag(self):
        app = GrillApp(self.fixture.root, self.api)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            choices = app.query_one('#choices', ChoiceList)
            self.assertIs(app.focused, choices)
            self.assertEqual(choices.recommended, ['local'])
            console = Console(width=80, color_system=None)
            first, second = (console.render_lines(choices.get_option_at_index(i).prompt, pad=False) for i in (0, 1))
            first, second = ([''.join(s.text for s in line) for line in rows] for rows in (first, second))
            self.assertIn('совет агента', first[0])
            # The stripe spans every line of the option; the gap is a separator outside it.
            self.assertTrue(all(line.endswith('▐') for line in first))
            self.assertTrue(first[-1].strip())
            self.assertTrue(choices.get_option_at_index(0)._divider)
            self.assertNotIn('▐', ''.join(second))
            self.assertEqual(choices.selected, [])  # Recommended, not chosen.
            self.assertFalse(choices.get_option_at_index(0).prompt.chosen)
            await pilot.press('down', 'enter')
            await pilot.pause()
            self.assertEqual((choices.selected, app.state['answers']['Q1']['confirmed']), (['sync'], False))
            await pilot.press('up', 'enter')
            await pilot.pause()
            self.assertEqual((choices.selected, app.state['answers']['Q1']['confirmed']), (['local'], False))
            # Only the owner's choice is filled.
            self.assertEqual([choices.get_option_at_index(i).prompt.chosen for i in (0, 1)], [True, False])
            self.assertFalse(app.query_one('#answer').has_class('-filled'))
            app.query_one('#answer', TextArea).load_text('Пояснение')
            await pilot.pause()
            self.assertTrue(app.query_one('#answer').has_class('-filled'))
            app.query_one('#answer', TextArea).load_text('')
            await pilot.pause()
            choices.focus()
            await pilot.press('enter')
            await pilot.pause()
            self.assertEqual(self.store.state['answers']['Q1'], {'selected': ['local'], 'text': '', 'confirmed': True})
            self.assertEqual(app.q['id'], 'Q2')

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

    async def test_streamed_reply_grows_and_follows_only_from_the_end(self):
        branch = self.store.state['branches']['Q1']
        history = [{'role': ('user', 'assistant')[i % 2], 'text': f'Сообщение {i}\n' * 3} for i in range(12)]
        branch.update(thread_id='codex-thread', runtime=dict(self.store.state['runtime']), status='running',
                      started_at=time.time(), activity='ищет в интернете', messages=history, partial='Начало')
        app = GrillApp(self.fixture.root, self.api)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            scroll = app.query_one('#chat-scroll')
            streamed = app.partial_message
            self.assertIn('Начало', str(streamed.render()))
            status = str(app.query_one('#chat-status').render())
            self.assertIn('Codex отвечает', status)
            self.assertIn('ищет в интернете', status)
            self.assertTrue(scroll.is_vertical_scroll_end)
            branch['partial'] = 'Начало и продолжение\n' * 5
            await app.tick()
            await pilot.pause()
            self.assertIs(app.partial_message, streamed)  # Updated in place, the history is not redrawn.
            self.assertIn('продолжение', str(streamed.render()))
            self.assertTrue(scroll.is_vertical_scroll_end)
            # The owner scrolled up to reread; the growing reply does not pull the view down.
            scroll.scroll_home(animate=False)
            await pilot.pause()
            branch['partial'] += 'ещё\n' * 5
            await app.tick()
            await pilot.pause()
            await pilot.pause()  # Scrolling waits for the refresh after the update.
            self.assertEqual(scroll.scroll_y, 0)
            # Back at the end, the view follows again.
            scroll.scroll_end(animate=False)
            await pilot.pause()
            branch['partial'] += 'и ещё\n' * 5
            await app.tick()
            await pilot.pause()
            await pilot.pause()
            self.assertTrue(scroll.is_vertical_scroll_end)
            # The final reply replaces the streamed one.
            branch['messages'].append({'role': 'assistant', 'text': 'Итог'})
            branch.update(partial='', status='idle', activity='', started_at=None)
            await app.tick()
            await pilot.pause()
            self.assertIsNone(app.partial_message)
            texts = [str(m.render()) for m in scroll.query('.msg')]
            self.assertEqual(len(texts), 13)
            self.assertIn('Итог', texts[-1])
            self.assertTrue(scroll.is_vertical_scroll_end)
            self.assertEqual(str(app.query_one('#chat-status').render()), '')


if __name__ == '__main__':
    unittest.main()
