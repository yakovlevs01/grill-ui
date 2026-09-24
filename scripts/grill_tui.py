#!/usr/bin/env python3
"""Three-column Grill interface. Run on the same host as the round server."""
import argparse
import asyncio
import copy
import fcntl
import json
import subprocess
from pathlib import Path
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.drivers.linux_driver import LinuxDriver
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Input, Label, OptionList, Select, SelectionList, Static, TextArea
from grill_client import Client
from grill_harness import HARNESSES, label
from grill_ui import read, write

CUSTOM = '__custom__'
SUBMITTED = 'Все ответы отправлены. Нажмите «К агенту» и напишите агенту «Готово».'
AGENT_NOTIFIED = 'Все ответы отправлены, агент получил «Готово». Нажмите «К агенту».'


class CellMouseDriver(LinuxDriver):
    """Use cell coordinates and SIGWINCH across multiplexers and SSH clients.

    Textual 8 enables pixel mouse together with in-band resize. Herdr clients
    may supply only cell coordinates; mixing these modes misplaces clicks.
    The Textual version is pinned because this driver hook is private.
    """
    def _query_in_band_window_resize(self):
        self.write("\x1b[?2048l\x1b[?1016l\x1b[?1006h")
        self.flush()


class AgentScreen(ModalScreen):
    """Harness, model and effort for new chats. Lists come from the installed CLIs."""
    BINDINGS = [('escape', 'cancel', 'Отмена')]
    CSS = '''
    AgentScreen { align: center middle; }
    #agent-box { width: 68; max-width: 100%; height: auto; max-height: 100%;
                 padding: 0 2 1 2; background: #192631; border: solid #80c8bc; }
    #agent-box Label { margin-top: 1; color: #80c8bc; text-style: bold; }
    #agent-box Select, #agent-box Input { width: 100%; }
    #agent-custom { margin-top: 1; }
    #agent-restart { margin-top: 1; }
    #agent-note { height: auto; color: #acbac5; margin-top: 1; }
    #agent-error { height: auto; color: #e1bd80; }
    #agent-buttons { height: auto; margin-top: 1; }
    #agent-buttons Button { width: 1fr; }
    '''

    def __init__(self, catalog, current, parent, chat):
        super().__init__()
        known = catalog.get('harnesses') or {}
        self.ready = catalog.get('ready', False)
        self.harnesses = {hid: known.get(hid) or {'label': h.label, 'available': h.available(),
                          'models': [], 'efforts': h.fallback_efforts} for hid, h in HARNESSES.items()}
        # Remember the last choice per harness, so switching back restores it.
        self.choice = {r['harness']: dict(r) for r in (parent, current) if r.get('harness') in self.harnesses}
        self.current = current
        self.chat = chat

    def models(self, hid):
        entry = self.harnesses[hid]
        models = list(entry['models'])
        remembered = self.choice.get(hid, {}).get('model')
        if remembered and all(m['id'] != remembered for m in models):
            models.insert(0, {'id': remembered, 'label': remembered, 'efforts': entry['efforts']})
        return models

    def efforts(self, hid, model):
        listed = next((m for m in self.models(hid) if m['id'] == model), None)
        return listed['efforts'] if listed else self.harnesses[hid]['efforts']

    def compose(self) -> ComposeResult:
        # With no CLI installed, still show all; the server reports the missing one.
        available = ([(e['label'], hid) for hid, e in self.harnesses.items() if e['available']]
                     or [(e['label'], hid) for hid, e in self.harnesses.items()])
        hid = self.current['harness'] if self.harnesses.get(self.current['harness'], {}).get('available') else available[0][1]
        with Vertical(id='agent-box'):
            yield Label('ХАРНЕСС')
            yield Select(available, allow_blank=False, value=hid, id='agent-harness')
            yield Label('МОДЕЛЬ')
            yield Select([('…', CUSTOM)], allow_blank=False, id='agent-model')
            yield Input(placeholder='ID модели, как его принимает CLI', id='agent-custom')
            yield Label('EFFORT')
            yield Select([('…', '')], allow_blank=False, id='agent-effort')
            if self.chat:
                yield Checkbox('Начать чат этого вопроса заново', id='agent-restart')
            yield Static(self.note(), id='agent-note', markup=False)
            yield Static('', id='agent-error', markup=False)
            with Horizontal(id='agent-buttons'):
                yield Button('Применить', id='agent-apply', variant='primary')
                yield Button('Отмена', id='agent-cancel')

    def note(self):
        lines = ['Применяется к новым чатам.']
        if self.chat:
            lines.append(f'Начатый чат продолжит {label(self.chat)}: история хранится в его сессии.')
        missing = [e['label'] for e in self.harnesses.values() if not e['available']]
        if missing:
            lines.append('Не установлен: ' + ', '.join(missing) + '.')
        if not self.ready:
            lines.append('Список моделей ещё загружается; ID можно ввести вручную.')
        return ' '.join(lines)

    def on_mount(self):
        self.fill_models()

    def fill_models(self):
        hid = self.query_one('#agent-harness', Select).value
        models = self.models(hid)
        wanted = self.choice.get(hid, {}).get('model') or (models[0]['id'] if models else CUSTOM)
        select = self.query_one('#agent-model', Select)
        with self.prevent(Select.Changed):
            select.set_options([(m['label'], m['id']) for m in models] + [('Другая модель…', CUSTOM)])
            select.value = wanted
        self.fill_efforts()

    def fill_efforts(self):
        hid = self.query_one('#agent-harness', Select).value
        model = self.query_one('#agent-model', Select).value
        self.query_one('#agent-custom').display = model == CUSTOM
        efforts = self.efforts(hid, model)
        listed = next((m for m in self.models(hid) if m['id'] == model), {})
        previous = self.query_one('#agent-effort', Select).value
        wanted = next((e for e in (previous, self.choice.get(hid, {}).get('effort'),
                                   listed.get('default_effort'), 'high') if e in efforts), efforts[0])
        select = self.query_one('#agent-effort', Select)
        with self.prevent(Select.Changed):
            select.set_options([(e, e) for e in efforts])
            select.value = wanted

    @on(Select.Changed)
    def changed(self, event):
        if event.select.id == 'agent-harness':
            self.fill_models()
        elif event.select.id == 'agent-model':
            self.fill_efforts()

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'agent-cancel':
            self.dismiss(None)
            return
        model = self.query_one('#agent-model', Select).value
        if model == CUSTOM:
            model = self.query_one('#agent-custom', Input).value.strip()
        if not model:
            self.query_one('#agent-error', Static).update('Укажите ID модели.')
            return
        restart = bool(self.chat) and self.query_one('#agent-restart', Checkbox).value
        self.dismiss({'runtime': {'harness': self.query_one('#agent-harness', Select).value, 'model': model,
                                  'effort': self.query_one('#agent-effort', Select).value}, 'restart': restart})

    def action_cancel(self):
        self.dismiss(None)


class GrillApp(App):
    TITLE = 'Grill'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [('f2', 'questions', 'Вопросы'), ('f3', 'discussion', 'Чат'),
                ('f4', 'answer_field', 'Ответ'), ('f5', 'agent', 'Агент'), ('f6', 'message_field', 'Сообщение'),
                ('ctrl+s', 'confirm', 'Подтвердить'), ('ctrl+q', 'leave', 'Выйти')]
    CSS = '''
    Screen { background: #111923; color: #e4e9ed; }
    #heading { height: 2; padding: 0 1; background: #1d2c3b; text-style: bold; }
    #toolbar { height: 3; }
    #toolbar Button { min-width: 12; margin-right: 1; }
    #body { height: 1fr; }
    #questions { width: 23%; min-width: 19; border-right: solid #344857; padding: 0 1; }
    #question-list { height: 1fr; background: #111923; }
    #center { width: 43%; padding: 0 1; }
    #discussion { width: 34%; border-left: solid #344857; padding: 0 1; }
    .caption { height: 1; color: #80c8bc; text-style: bold; margin-top: 1; }
    #question-scroll { height: 1fr; }
    #question-text { height: auto; margin-bottom: 1; }
    #recommendation { height: auto; color: #9dcfc4; margin-bottom: 1; }
    #choices { height: auto; max-height: 14; background: #192631; }
    #choices > .selection-list--button, #choices > .selection-list--button-highlighted {
        color: #344857; background: #344857;
    }
    #choices > .selection-list--button-selected, #choices > .selection-list--button-selected-highlighted {
        color: #80c8bc; background: #344857;
    }
    #option-details { height: auto; color: #acbac5; margin-top: 1; }
    TextArea { height: 7; border: solid #344857; background: #192631; }
    TextArea:focus { border: solid #80c8bc; }
    #answer { height: 8; }
    #confirm { width: 100%; }
    #chat-scroll { height: 1fr; }
    #chat-log { height: auto; }
    #chat-status { height: auto; max-height: 5; color: #e1bd80; }
    #chat-buttons, #draft-buttons { height: auto; min-height: 3; }
    #discussion Button { min-width: 8; width: 1fr; }
    #agent { width: 100%; height: 3; }
    #draft { height: auto; max-height: 8; color: #9dcfc4; }
    #status { height: auto; min-height: 1; max-height: 3; padding: 0 1; background: #1d2c3b; }
    #submit { min-width: 25; }
    .narrow #questions { width: 100%; }
    .narrow #center, .narrow #discussion { width: 100%; border: none; }
    '''

    def __init__(self, session, client=None):
        super().__init__(driver_class=CellMouseDriver)
        self.session = Path(session).resolve()
        self.client = client or Client(session)
        self.state = None
        self.index = 0
        self.dirty = set()
        self.draft_dirty = set()
        self.api_lock = asyncio.Lock()
        self.chat_fingerprint = None
        self.compact_view = 'center'
        self.expanded_chat = False
        self.pending_file = self.session / 'tui-pending.json'
        self.submit_notice = SUBMITTED

    def compose(self) -> ComposeResult:
        yield Static('Grill · загрузка…', id='heading', markup=False)
        with Horizontal(id='toolbar'):
            yield Button('Вопросы', id='toggle-questions')
            yield Button('Вопрос / чат', id='toggle-chat')
            yield Button('К агенту', id='return')
            yield Button('Отправить все ответы', id='submit', variant='success')
        with Horizontal(id='body'):
            with Vertical(id='questions'):
                yield Label('ВОПРОСЫ', classes='caption')
                yield OptionList(id='question-list')
            with Vertical(id='center'):
                with VerticalScroll(id='question-scroll'):
                    yield Static('', id='question-text', markup=False)
                    yield Static('', id='recommendation', markup=False)
                    yield SelectionList(id='choices')
                    yield Static('', id='option-details', markup=False)
                yield Label('ВАШ ОТВЕТ / КОММЕНТАРИЙ', classes='caption')
                yield TextArea(id='answer', soft_wrap=True, tab_behavior='focus')
                yield Button('Подтвердить ответ', id='confirm', variant='primary')
            with Vertical(id='discussion'):
                yield Label('ОТДЕЛЬНОЕ ОБСУЖДЕНИЕ', classes='caption')
                yield Button('Агент', id='agent')
                with VerticalScroll(id='chat-scroll'):
                    yield Static('', id='chat-log', markup=False)
                yield Static('', id='chat-status', markup=False)
                yield TextArea(id='message', soft_wrap=True, tab_behavior='focus')
                with Horizontal(id='chat-buttons'):
                    yield Button('Отправить', id='send', variant='primary')
                    yield Button('Остановить', id='stop')
                yield Static('', id='draft', markup=False)
                with Horizontal(id='draft-buttons'):
                    yield Button('Черновик', id='summarize')
                    yield Button('В ответ', id='insert')
        yield Static('Подключение…', id='status', markup=False)
        yield Footer()

    async def api(self, path, data=None):
        async with self.api_lock:
            return await asyncio.to_thread(self.client.request, path, data)

    def status(self, message):
        self.query_one('#status', Static).update(message)

    @property
    def q(self):
        return self.state['round']['questions'][self.index]

    async def on_mount(self):
        try:
            self.state = await self.api('/api/state')
            if self.pending_file.exists() and not self.state['submitted']:
                pending = read(self.pending_file)
                if pending.get('round_id') == self.state['round']['id']:
                    for qid, answer in pending.get('answers', {}).items():
                        if qid in self.state['answers']:
                            self.state['answers'][qid] = answer
                            self.dirty.add(qid)
                    for qid, draft in pending.get('drafts', {}).items():
                        if qid in self.state['branches']:
                            self.state['branches'][qid]['input_draft'] = draft
                            self.draft_dirty.add(qid)
            self.query_one('#heading', Static).update('Grill · ' + self.state['round']['title'])
            self.refresh_list()
            self.load_question()
            self.apply_layout()
            self.set_interval(.6, self.tick)
            await self.tick()
        except (OSError, ValueError) as exc:
            self.status(f'Не удалось открыть раунд: {exc}. Закройте и повторите open.')

    async def notify_agent(self):
        """Wake the agent in its own pane; without Herdr the owner does it by hand."""
        if not (self.session / 'herdr.json').exists():
            return SUBMITTED
        try:
            from grill_herdr import notify_agent
            await asyncio.to_thread(notify_agent, self.session)
            return AGENT_NOTIFIED
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return f'Не удалось написать агенту: {exc}. ' + SUBMITTED

    def checkpoint(self):
        # Preserve edits even if the server becomes unreachable before autosave.
        write(self.pending_file, {'round_id': self.state['round']['id'],
            'answers': {k: self.state['answers'][k] for k in self.dirty},
            'drafts': {k: self.state['branches'][k].get('input_draft', '') for k in self.draft_dirty}})

    def refresh_list(self):
        listing = self.query_one('#question-list', OptionList)
        with self.prevent(OptionList.OptionSelected):
            listing.clear_options()
            for q in self.state['round']['questions']:
                mark = '✓' if self.state['answers'][q['id']]['confirmed'] else '○'
                listing.add_option(Text(f'{mark} {q["title"]}'))
            listing.highlighted = self.index

    def load_question(self):
        q = self.q
        answer = self.state['answers'][q['id']]
        self.query_one('#question-text', Static).update(q['title'] + '\n\n' + q['body'])
        self.query_one('#recommendation', Static).update('Рекомендация\n' + q['recommendation'])
        choices = self.query_one('#choices', SelectionList)
        with self.prevent(SelectionList.SelectedChanged, SelectionList.SelectionToggled, TextArea.Changed):
            choices.clear_options()
            choices.add_options([(Text(o['label']), o['id'], o['id'] in answer['selected'])
                                 for o in q.get('options', [])])
            choices.display = q.get('mode') != 'text' and bool(q.get('options'))
            self.query_one('#answer', TextArea).load_text(answer['text'])
            self.query_one('#message', TextArea).load_text(self.state['branches'][q['id']].get('input_draft', ''))
        self.query_one('#option-details', Static).update('\n\n'.join(
            o['label'] + ': ' + o['description'] for o in q.get('options', []) if o.get('description')))
        self.query_one('#question-scroll').scroll_home(animate=False)
        self.chat_fingerprint = None
        self.refresh_chat()
        self.refresh_controls()

    def refresh_controls(self):
        submitted = self.state['submitted']
        running = self.state['branches'][self.q['id']]['status'] == 'running'
        for ident in ('answer', 'message', 'choices', 'confirm', 'submit', 'insert', 'summarize', 'send'):
            self.query_one('#' + ident).disabled = submitted
        self.query_one('#send').disabled = submitted or running
        self.query_one('#summarize').disabled = submitted or running
        self.query_one('#stop').disabled = submitted or not running
        self.query_one('#return', Button).variant = 'success' if submitted else 'default'
        agent = self.query_one('#agent', Button)
        agent.label = label(self.chat_runtime())
        agent.disabled = submitted or running
        confirmed = self.state['answers'][self.q['id']]['confirmed']
        self.query_one('#confirm', Button).label = 'Ответ подтверждён ✓' if confirmed else 'Подтвердить ответ'

    def chat_runtime(self):
        branch = self.state['branches'][self.q['id']]
        return branch['runtime'] if branch.get('thread_id') and branch.get('runtime') else self.state['runtime']

    def refresh_chat(self):
        branch = self.state['branches'][self.q['id']]
        fingerprint = json.dumps(branch.get('messages', []), ensure_ascii=False)
        if fingerprint != self.chat_fingerprint:
            self.chat_fingerprint = fingerprint
            text = '\n\n'.join(('Вы' if m['role'] == 'user' else 'Агент') + '\n' + m['text']
                               for m in branch['messages'])
            self.query_one('#chat-log', Static).update(text or 'Обсудите этот вопрос. Переписка не попадёт основному агенту.')
            self.query_one('#chat-scroll').scroll_end(animate=False)
        self.query_one('#chat-status', Static).update(branch.get('error') or (
            'Агент отвечает… Можно перейти к другому вопросу.' if branch['status'] == 'running' else ''))
        self.query_one('#draft', Static).update(branch.get('summary', ''))
        self.query_one('#draft').display = bool(branch.get('summary'))

    async def flush(self):
        for qid in list(self.dirty):
            snapshot = copy.deepcopy(self.state['answers'][qid])
            await self.api('/api/answer', {'question_id': qid, **snapshot})
            if self.state['answers'][qid] == snapshot:
                self.dirty.discard(qid)
        for qid in list(self.draft_dirty):
            snapshot = self.state['branches'][qid].get('input_draft', '')
            await self.api('/api/draft', {'question_id': qid, 'text': snapshot})
            if self.state['branches'][qid].get('input_draft', '') == snapshot:
                self.draft_dirty.discard(qid)
        self.checkpoint()

    async def tick(self):
        if not self.state:
            return
        try:
            if not self.state['submitted']:
                await self.flush()
            latest = await self.api('/api/state')
            for qid, branch in latest['branches'].items():
                draft = self.state['branches'][qid].get('input_draft', '')
                self.state['branches'][qid] = branch
                self.state['branches'][qid]['input_draft'] = draft
            self.state['submitted'] = latest['submitted']
            self.state['runtime'] = latest['runtime']
            self.refresh_chat()
            self.refresh_controls()
            if self.state['submitted']:
                self.status(self.submit_notice)
            elif not self.dirty and not self.draft_dirty:
                total = len(self.state['answers'])
                count = sum(a['confirmed'] for a in self.state['answers'].values())
                self.status(f'Сохранено · подтверждено {count}/{total} · отправка только кнопкой «Отправить все ответы»')
        except (OSError, ValueError) as exc:
            self.status(f'Нет сохранения на сервере: {exc}. Черновики сохранены локально; сообщения повторно не отправляются.')
        except NoMatches:
            pass  # The interval can fire while the app tears down its widgets.

    @on(TextArea.Changed)
    def edited(self, event):
        if not self.state or self.state['submitted']:
            return
        qid = self.q['id']
        if event.text_area.id == 'answer':
            answer = self.state['answers'][qid]
            if answer['text'] == event.text_area.text:
                return
            answer.update(text=event.text_area.text, confirmed=False)
            self.dirty.add(qid)
            self.refresh_list()
            self.refresh_controls()
        elif event.text_area.id == 'message':
            branch = self.state['branches'][qid]
            if branch.get('input_draft', '') == event.text_area.text:
                return
            branch['input_draft'] = event.text_area.text
            self.draft_dirty.add(qid)
        self.checkpoint()
        self.status('Сохраняю…')

    @on(SelectionList.SelectionToggled, '#choices')
    def selected(self, event):
        if not self.state or self.state['submitted']:
            return
        choices = event.selection_list
        if self.q.get('mode', 'single') == 'single' and event.selection.value in choices.selected:
            with self.prevent(SelectionList.SelectedChanged, SelectionList.SelectionToggled):
                for value in list(choices.selected):
                    if value != event.selection.value:
                        choices.deselect(value)
        self.state['answers'][self.q['id']].update(selected=list(choices.selected), confirmed=False)
        self.dirty.add(self.q['id'])
        self.checkpoint()
        self.refresh_list()
        self.refresh_controls()

    @on(OptionList.OptionSelected, '#question-list')
    async def question_selected(self, event):
        if not self.state:
            return
        try:
            await self.flush()
        except (OSError, ValueError) as exc:
            self.status(f'Черновик сохранён локально: {exc}')
        self.index = event.option_index
        self.load_question()
        self.compact_view = 'center'
        self.apply_layout()

    async def action_confirm(self):
        if not self.state or self.state['submitted']:
            return
        answer = self.state['answers'][self.q['id']]
        if not answer['selected'] and not answer['text'].strip():
            self.status('Выберите вариант или напишите ответ.')
            return
        answer['confirmed'] = True
        self.dirty.add(self.q['id'])
        self.checkpoint()
        try:
            await self.flush()
            self.refresh_list()
            self.refresh_controls()
            self.status('Ответ подтверждён. Перейдите к следующему вопросу.')
        except (OSError, ValueError) as exc:
            self.status(f'Подтверждение пока не сохранено на сервере: {exc}')

    @on(Button.Pressed)
    async def pressed(self, event):
        ident = event.button.id
        if ident == 'toggle-questions':
            self.action_questions()
            return
        if ident == 'toggle-chat':
            self.action_discussion()
            return
        if ident == 'agent':
            await self.action_agent()
            return
        if not self.state:
            return
        try:
            if ident == 'confirm':
                await self.action_confirm()
            elif ident == 'return':
                await self.flush()
                from grill_herdr import return_to_agent
                await asyncio.to_thread(return_to_agent, self.session)
            elif ident == 'submit':
                await self.flush()
                await self.api('/api/submit', {})
                self.state['submitted'] = True
                self.refresh_controls()
                self.submit_notice = await self.notify_agent()
                self.status(self.submit_notice)
                self.notify(self.submit_notice, title='Ответы отправлены', timeout=30)
                self.query_one('#return', Button).focus()
            elif ident in ('send', 'summarize'):
                await self.flush()
                qid = self.q['id']
                text = self.query_one('#message', TextArea).text
                if ident == 'send' and not text.strip():
                    self.status('Напишите сообщение.')
                    return
                # No automatic retries: a lost HTTP response is an ambiguous send.
                await self.api('/api/chat', {'question_id': qid,
                    'message': text if ident == 'send' else 'summary', 'summarize': ident == 'summarize'})
                if ident == 'send':
                    self.query_one('#message', TextArea).load_text('')
                await self.tick()
            elif ident == 'stop':
                await self.api('/api/stop', {'question_id': self.q['id']})
            elif ident == 'insert':
                summary = self.state['branches'][self.q['id']].get('summary')
                if summary:
                    editor = self.query_one('#answer', TextArea)
                    # Append instead of silently replacing the owner's existing text.
                    editor.load_text((editor.text.rstrip() + '\n\n' + summary).strip())
                    self.compact_view = 'center'
                    self.expanded_chat = False
                    self.apply_layout()
                    editor.focus()
        except (OSError, ValueError) as exc:
            self.status(f'Действие не подтверждено: {exc}. Проверьте состояние перед повторной отправкой.')

    async def action_agent(self):
        if not self.state or self.state['submitted']:
            return
        branch = self.state['branches'][self.q['id']]
        if branch['status'] == 'running':
            self.status('Дождитесь ответа или остановите обсуждение.')
            return
        qid = self.q['id']
        try:
            catalog = await self.api('/api/catalog')
        except (OSError, ValueError) as exc:
            self.status(f'Список моделей недоступен: {exc}')
            catalog = {'ready': False, 'harnesses': {}}
        chat = branch['runtime'] if branch.get('thread_id') else None
        parent = self.state.get('parent_runtime', self.state['runtime'])

        async def chosen(result):
            if not result:
                return
            try:
                await self.api('/api/runtime', {'question_id': qid, **result})
                await self.tick()
                self.load_question()
                self.status('Агент для новых чатов: ' + label(self.state['runtime']))
            except (OSError, ValueError) as exc:
                self.status(f'Настройки не применены: {exc}')
        self.push_screen(AgentScreen(catalog, self.state['runtime'], parent, chat), chosen)

    def action_answer_field(self):
        self.compact_view = 'center'
        self.expanded_chat = False
        self.apply_layout()
        self.query_one('#answer').focus()

    def action_message_field(self):
        self.compact_view = 'discussion'
        self.apply_layout()
        self.query_one('#message').focus()

    def action_questions(self):
        self.compact_view = 'center' if self.compact_view == 'questions' else 'questions'
        if self.size.width >= 110:
            self.query_one('#questions').display = not self.query_one('#questions').display
        else:
            self.apply_layout()

    def action_discussion(self):
        self.compact_view = 'center' if self.compact_view == 'discussion' else 'discussion'
        if self.size.width >= 110:
            self.expanded_chat = not self.expanded_chat
        self.apply_layout()

    def on_resize(self):
        if self.is_mounted:
            self.call_after_refresh(self.apply_layout)

    def apply_layout(self):
        narrow = self.size.width < 110
        self.set_class(narrow, 'narrow')
        for name in ('questions', 'center', 'discussion'):
            self.query_one('#' + name).display = name == self.compact_view if narrow else (
                name == 'discussion' if self.expanded_chat else True)
        self.query_one('#discussion').styles.width = '100%' if self.expanded_chat or narrow else '34%'

    async def action_leave(self):
        if self.state:
            self.checkpoint()
            try:
                await self.flush()
            except (OSError, ValueError):
                pass
        self.exit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True)
    args = parser.parse_args()
    with (Path(args.session) / 'tui.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, 'Этот раунд уже открыт в другом терминальном интерфейсе.\n')
        GrillApp(args.session).run()


if __name__ == '__main__':
    main()
