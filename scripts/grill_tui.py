#!/usr/bin/env python3
"""Three-column Grill interface. Run on the same host as the round server."""
import argparse
import asyncio
import copy
import fcntl
import json
import subprocess
import time
from pathlib import Path
from rich.console import Group
from rich.segment import Segment
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.drivers.linux_driver import LinuxDriver
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button, Footer, Input, Label, OptionList, Select, Static, TextArea
from grill_client import Client
from grill_harness import HARNESSES, label
from grill_ui import read, write

CUSTOM = '__custom__'
# The owner's terminal theme (2026 Dark): one gray family, one accent, status colors only for state.
PALETTE = {'bg': '#121314', 'panel': '#161718', 'surface': '#1c1d1f', 'raised': '#232527',
           'line': '#2a2b2c', 'line-strong': '#3a3d41', 'text': '#bbbebf', 'strong': '#e2e5e7',
           'muted': '#8b949e', 'faint': '#5c636b', 'accent': '#79c0ff', 'accent-dim': '#1d3a57',
           'ok': '#7ee787', 'warn': '#cd9731', 'err': '#ff7b72',
           # Herdr's own divider and selected-row colors, so the tab reads as part of Herdr.
           'divider': '#33363a', 'selection': '#2a2b2c'}
MODES = {'single': 'Один вариант', 'multiple': 'Несколько вариантов', 'text': 'Свободный ответ'}
SUBMITTED = 'Все ответы отправлены. Нажмите «К агенту» и напишите агенту «Готово».'
ALL_CONFIRMED = 'Все ответы подтверждены. Нажмите «Отправить все ответы» или Enter на этой кнопке.'
AGENT_NOTIFIED = 'Все ответы отправлены, агент получил «Готово» и путь к файлу: {path}'


class CellMouseDriver(LinuxDriver):
    """Use cell coordinates and SIGWINCH across multiplexers and SSH clients.

    Textual 8 enables pixel mouse together with in-band resize. Herdr clients
    may supply only cell coordinates; mixing these modes misplaces clicks.
    The Textual version is pinned because this driver hook is private.
    """
    def _query_in_band_window_resize(self):
        self.write("\x1b[?2048l\x1b[?1016l\x1b[?1006h")
        self.flush()


class Composer(TextArea):
    """Enter submits; Shift+Enter or Ctrl+J inserts a line break."""

    class Submitted(Message):
        def __init__(self, composer):
            super().__init__()
            self.composer = composer

    async def _on_key(self, event):
        if event.key == 'enter':
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self))
            return
        if event.key in ('shift+enter', 'ctrl+j', 'alt+enter'):
            event.stop()
            event.prevent_default()
            self.insert('\n')
            return
        await super()._on_key(event)


class ChoiceRow:
    """An option row; a recommended one gets an amber stripe along its right edge.

    The stripe spans exactly the option's lines, not the gap below it.
    """
    def __init__(self, body, recommended, gap):
        self.body, self.recommended, self.gap = body, recommended, gap

    def __rich_console__(self, console, options):
        width = options.max_width - (2 if self.recommended else 0)
        for line in console.render_lines(self.body, options.update_width(width), pad=True):
            yield from line
            if self.recommended:
                yield Segment(' ▐', console.get_style(PALETTE['warn']))
            yield Segment.line()
        if self.gap:
            yield Segment.line()  # Breathing room; a drawn separator would show on a transparent background.


class ChoiceList(OptionList):
    """Options with the description under the label; SelectionList shows one line only."""
    # Space toggles. Enter marks the highlighted option; Enter on a marked one confirms the answer.
    BINDINGS = [Binding('space', 'select', 'Выбрать', show=False),
                Binding('enter', 'submit', 'Подтвердить', show=False)]

    class Toggled(Message):
        def __init__(self, choices):
            super().__init__()
            self.choices = choices

    class Submitted(Message):
        pass

    def action_submit(self):
        index = self.highlighted
        if index is not None and self.choices[index]['id'] not in self.selected:
            self.action_select()
        else:
            self.post_message(self.Submitted())

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.choices, self.mode, self.selected, self.recommended = [], 'single', [], []

    def load(self, options, selected, mode, recommended=()):
        self.choices, self.mode, self.selected = options, mode, list(selected)
        self.recommended = list(recommended)
        self.redraw(0)

    def redraw(self, highlighted=None):
        highlighted = self.highlighted if highlighted is None else highlighted
        self.clear_options()
        for index, o in enumerate(self.choices):
            chosen = o['id'] in self.selected
            if self.mode == 'multiple':
                mark = '■' if chosen else '□'
            else:
                mark = '●' if chosen else '○'
            recommended = o['id'] in self.recommended
            title = Text(o['label'], style=f"bold {PALETTE['accent' if chosen else 'strong']}")
            if recommended:
                # The circle and the blue belong to the owner's choice; the model's advice stays amber, on the right.
                head = Table.grid(padding=(0, 1), expand=True)
                head.add_column(ratio=1)
                head.add_column(justify='right')
                head.add_row(title, Text('совет агента', style=PALETTE['warn']))
                title = head
            body = Group(title, Text(o['description'], style=PALETTE['muted'])) if o.get('description') else title
            row = Table.grid(padding=(0, 1), expand=True)
            row.add_column(width=1)
            row.add_column(ratio=1)
            row.add_row(Text(mark, style=PALETTE['accent' if chosen else 'faint']), body)
            self.add_option(ChoiceRow(row, recommended, index < len(self.choices) - 1))
        if self.choices:
            self.highlighted = min(highlighted or 0, len(self.choices) - 1)

    @on(OptionList.OptionSelected)
    def toggle(self, event):
        event.stop()
        value = self.choices[event.option_index]['id']
        if value in self.selected:
            self.selected.remove(value)
        elif self.mode == 'multiple':
            self.selected.append(value)
        else:
            self.selected = [value]
        # Keep the owner's order stable: the round's option order, not click order.
        self.selected = [o['id'] for o in self.choices if o['id'] in self.selected]
        self.redraw()
        self.post_message(self.Toggled(self))


def chat_message(role, text, author):
    """One chat turn: author line, then the text, framed by role."""
    color = PALETTE['accent'] if role == 'user' else PALETTE['strong']
    body = Text.assemble((author, f'bold {color}'), '\n', text)
    return Static(body, classes='msg ' + ('msg-user' if role == 'user' else 'msg-agent'))


class AgentPicker(Vertical):
    """Harness, model and effort for new chats, inline in the discussion panel.

    Lists come from the installed CLIs via the server catalog.
    """

    class Changed(Message):
        def __init__(self, runtime, restart=False):
            super().__init__()
            self.runtime, self.restart = runtime, restart

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.catalog = {'ready': False, 'harnesses': {}}
        self.harnesses = {}
        self.choice = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id='agent-row'):
            yield Select([('…', '')], allow_blank=False, compact=True, id='agent-harness')
            yield Select([('…', CUSTOM)], allow_blank=False, compact=True, id='agent-model')
            yield Select([('…', '')], allow_blank=False, compact=True, id='agent-effort')
        yield Input(placeholder='ID модели, как его принимает CLI · Enter', compact=True, id='agent-custom')
        with Horizontal(id='agent-chat'):
            yield Static('', id='agent-note')
            yield Button('Новый чат', id='agent-restart', compact=True)

    def load(self, catalog, current, parent, chat):
        """Show `current` (the runtime for new chats) without emitting Changed."""
        self.catalog = catalog
        known = catalog.get('harnesses') or {}
        self.harnesses = {hid: known.get(hid) or {'label': h.label, 'available': h.available(),
                          'models': [], 'efforts': h.fallback_efforts} for hid, h in HARNESSES.items()}
        # Remember the last choice per harness, so switching back restores it.
        self.choice = {r['harness']: dict(r) for r in (parent, current) if r.get('harness') in self.harnesses}
        # With no CLI installed, still list all; the server reports the missing one.
        available = ([(e['label'], hid) for hid, e in self.harnesses.items() if e['available']]
                     or [(e['label'], hid) for hid, e in self.harnesses.items()])
        hid = current['harness'] if any(h == current['harness'] for _, h in available) else available[0][1]
        harness = self.query_one('#agent-harness', Select)
        with self.prevent(Select.Changed):
            harness.set_options(available)
            harness.value = hid
            self.fill_models()
        note = self.query_one('#agent-note', Static)
        note.update(f'Этот чат ведёт {label(chat)}' if chat else '')
        self.query_one('#agent-chat').display = bool(chat)

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

    def runtime(self):
        model = self.query_one('#agent-model', Select).value
        if model == CUSTOM:
            model = self.query_one('#agent-custom', Input).value.strip()
        if not model:
            return None
        return {'harness': self.query_one('#agent-harness', Select).value, 'model': model,
                'effort': self.query_one('#agent-effort', Select).value}

    def emit(self, restart=False):
        runtime = self.runtime()
        if runtime:
            self.choice[runtime['harness']] = dict(runtime)
            self.post_message(self.Changed(runtime, restart))

    @on(Select.Changed)
    def changed(self, event):
        event.stop()
        harness = self.query_one('#agent-harness', Select).value
        if harness not in self.harnesses:
            return  # Placeholder values before the first load.
        if event.select.id == 'agent-harness':
            with self.prevent(Select.Changed):
                self.fill_models()
        elif event.select.id == 'agent-model':
            with self.prevent(Select.Changed):
                self.fill_efforts()
            if event.value == CUSTOM:
                self.query_one('#agent-custom', Input).focus()
                return  # Applied once the owner types the model ID.
        self.emit()

    @on(Input.Submitted, '#agent-custom')
    def custom(self, event):
        event.stop()
        self.emit()

    @on(Button.Pressed, '#agent-restart')
    def restart(self, event):
        event.stop()
        self.emit(restart=True)


class GrillApp(App):
    TITLE = 'Grill'
    ENABLE_COMMAND_PALETTE = False
    # Priority: TextArea binds F6 to select_line, so typing after F6 replaced the answer line.
    BINDINGS = [Binding('f2', 'questions', 'Вопросы', priority=True),
                Binding('f3', 'discussion', 'Чат', priority=True),
                Binding('f4', 'answer_field', 'Ответ', priority=True),
                Binding('f5', 'agent', 'Агент', priority=True),
                Binding('f6', 'message_field', 'Сообщение', priority=True),
                Binding('f7', 'toggle_discussion', 'Скрыть чат', priority=True),
                ('ctrl+s', 'confirm', 'Подтвердить'), ('ctrl+q', 'leave', 'Выйти')]
    CSS = '''
    Screen { background: ansi_default; color: $text; }
    Button { border: none; height: 1; min-width: 0; padding: 0 2;
             background: $raised; color: $text; text-style: none; }
    Button:hover { background: $line-strong; color: $strong; }
    Button:focus { background: $accent-dim; color: $strong; text-style: bold; }
    Button.-primary, Button.-success { background: $accent; color: $bg; text-style: bold; }
    Button.-primary:hover, Button.-success:hover { background: $strong; color: $bg; }
    Button.-primary:focus, Button.-success:focus { background: $strong; color: $bg; }
    Button:disabled { background: $surface; color: $faint; text-style: none; }

    #header { height: 1; padding: 0 1; }
    #heading { width: 1fr; }
    #progress { width: auto; color: $muted; padding: 0 2; }
    #header Button { margin-left: 1; }

    #body { height: 1fr; }
    #questions { width: 30; padding: 1 0 0 0; border-right: solid $divider; }
    #questions .caption { padding: 0 2; }
    #question-list { height: 1fr; background: ansi_default; border: none; padding: 0 1; }
    #question-list > .option-list--option { padding: 0 1; }
    #question-list > .option-list--option-highlighted { background: $selection; color: $strong; text-style: none; }
    #question-list:focus > .option-list--option-highlighted { background: $accent-dim; }
    #question-list > .option-list--option-hover { background: $surface; }

    #center { width: 1fr; padding: 1 3 0 3; }
    #discussion { width: 38%; padding: 1 2 0 2; border-left: solid $divider; }
    .caption { height: 1; color: $muted; text-style: bold; }
    #question-scroll { height: 1fr; scrollbar-size-vertical: 1; }
    #question-meta { height: 1; color: $muted; }
    #question-title { height: auto; color: $strong; text-style: bold; margin: 1 0 1 0; }
    #question-body { height: auto; max-width: 96; }
    #recommendation { height: auto; max-width: 96; margin-top: 1; padding: 0 1;
                      background: $surface; border-left: outer $accent; }
    #choices { height: auto; max-height: 20; margin-top: 1; max-width: 96;
               background: $bg; border: none; padding: 0; }
    #choices, #choices:focus { background: ansi_default; }
    #choices > .option-list--option { padding: 0 1; }
    #choices > .option-list--option-highlighted { background: ansi_default; text-style: none; }
    #choices:focus > .option-list--option-highlighted { background: $surface; }
    #choices > .option-list--option-hover { background: $surface; }
    #choices-hint { width: 1fr; height: auto; max-width: 96; margin-top: 1; padding: 0 1; }

    .field-head { height: 1; margin-top: 1; }
    .field-head .caption { width: 1fr; }
    .hint { width: auto; color: $faint; }
    TextArea { height: 6; background: $surface; border: tall $surface; padding: 0 1; }
    TextArea:focus { border: tall $accent; }
    TextArea > .text-area--cursor-line { background: $surface; }
    #answer { height: 7; }
    #answer-actions { height: 1; margin: 1 0 1 0; }
    #answer-state { width: 1fr; color: $muted; }
    #answer-state.-confirmed { color: $ok; }

    #discussion-head { height: 1; }
    #discussion-head .caption { width: 1fr; }
    #agent-picker { height: auto; margin-top: 1; }
    #agent-row { height: 1; }
    #agent-row Select { width: 1fr; margin-right: 1; }
    #agent-harness { max-width: 16; }
    #agent-effort { max-width: 12; margin-right: 0; }
    #agent-row SelectCurrent { background: $raised; color: $text; border: none; padding: 0 1; }
    #agent-row Select:focus > SelectCurrent { background: $accent-dim; color: $strong; }
    #agent-row SelectOverlay { background: $raised; border: tall $line-strong; }
    #agent-custom { margin-top: 1; background: $raised; border: none; }
    #agent-custom:focus { background: $accent-dim; }
    #agent-chat { height: auto; margin-top: 1; }
    #agent-note { width: 1fr; height: auto; color: $muted; }
    #chat-scroll { height: 1fr; margin-top: 1; scrollbar-size-vertical: 1; }
    .msg { height: auto; padding: 0 1; margin-bottom: 1; }
    .msg-user { background: $surface; border-left: outer $accent; }
    .msg-agent { border-left: outer $line-strong; }
    #chat-empty { color: $faint; padding: 0 1; }
    #chat-status { height: auto; max-height: 5; color: $warn; }
    #chat-status.-error { color: $err; }
    #message { height: 5; }
    #chat-buttons, #draft-buttons { height: 1; margin: 1 0 0 0; }
    #chat-buttons Button, #draft-buttons Button { margin-right: 1; }
    #draft { height: auto; max-height: 8; margin-top: 1; padding: 0 1;
             background: $surface; border-left: outer $ok; }
    #draft-buttons { margin-bottom: 1; }

    #status { height: 1; padding: 0 1; color: $muted; }
    #status.-error { color: $err; }
    Footer, Footer > FooterKey, Footer > FooterKey .footer-key--key { background: ansi_default; }
    Footer > FooterKey { color: $muted; }
    Footer > FooterKey .footer-key--key { color: $accent; }
    Toast { background: $raised; }
    .narrow #progress { display: none; }
    .narrow #questions { width: 100%; }
    .narrow #center, .narrow #discussion { width: 100%; }
    .narrow #questions, .narrow #discussion { border: none; }
    .narrow #center { padding: 1 1 0 1; }
    '''

    def get_css_variables(self):
        return {**super().get_css_variables(), **PALETTE}

    def __init__(self, session, client=None):
        # Native ANSI lets `ansi_default` keep the terminal's own (possibly transparent) background.
        super().__init__(driver_class=CellMouseDriver, ansi_color=True)
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
        # Wide-screen panels the owner hid with F2 / F7; they stay hidden across redraws.
        self.hide_questions = False
        self.hide_discussion = False
        self.pending_file = self.session / 'tui-pending.json'
        self.submit_notice = SUBMITTED
        self.catalog = {'ready': False, 'harnesses': {}}
        self.catalog_checked = time.monotonic()

    def compose(self) -> ComposeResult:
        with Horizontal(id='header'):
            yield Static('Grill', id='heading')
            yield Static('', id='progress')
            yield Button('Вопросы', id='toggle-questions', compact=True)
            yield Button('Вопрос / чат', id='toggle-chat', compact=True)
            yield Button('К агенту', id='return', compact=True)
            yield Button('Отправить все ответы', id='submit', variant='success', compact=True)
        with Horizontal(id='body'):
            with Vertical(id='questions'):
                yield Label('Вопросы', classes='caption')
                yield OptionList(id='question-list')
            with Vertical(id='center'):
                with VerticalScroll(id='question-scroll'):
                    yield Static('', id='question-meta')
                    yield Static('', id='question-title', markup=False)
                    yield Static('', id='question-body', markup=False)
                    yield Static('', id='recommendation')
                    yield ChoiceList(id='choices')
                    yield Static('↑↓ Enter выбрать · Enter ещё раз подтвердить · Space снять',
                                 id='choices-hint', classes='hint')
                with Horizontal(classes='field-head'):
                    yield Label('Ваш ответ', classes='caption')
                    yield Static('Enter сохранить · Shift+Enter перенос', classes='hint')
                yield Composer(id='answer', soft_wrap=True, tab_behavior='focus')
                with Horizontal(id='answer-actions'):
                    yield Static('', id='answer-state')
                    yield Button('Подтвердить ответ', id='confirm', variant='primary', compact=True)
            with Vertical(id='discussion'):
                with Horizontal(id='discussion-head'):
                    yield Label('Обсуждение', classes='caption')
                yield AgentPicker(id='agent-picker')
                with VerticalScroll(id='chat-scroll'):
                    yield Static('', id='chat-empty')
                yield Static('', id='chat-status', markup=False)
                with Horizontal(classes='field-head'):
                    yield Label('Сообщение', classes='caption')
                    yield Static('Enter отправить · Shift+Enter перенос', classes='hint')
                yield Composer(id='message', soft_wrap=True, tab_behavior='focus')
                with Horizontal(id='chat-buttons'):
                    yield Button('Отправить', id='send', variant='primary', compact=True)
                    yield Button('Остановить', id='stop', compact=True)
                    yield Button('Черновик', id='summarize', compact=True)
                yield Static('', id='draft', markup=False)
                with Horizontal(id='draft-buttons'):
                    yield Button('Вставить в мой ответ', id='insert', compact=True)
        yield Static('Подключение…', id='status', markup=False)
        yield Footer()

    async def api(self, path, data=None):
        async with self.api_lock:
            return await asyncio.to_thread(self.client.request, path, data)

    def status(self, message, error=False):
        try:
            line = self.query_one('#status', Static)
        except NoMatches:
            return  # A late timer during teardown; there is nowhere to report.
        line.set_class(error, '-error')
        line.update(message)

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
            self.query_one('#heading', Static).update(Text.assemble(
                ('Grill', f"bold {PALETTE['accent']}"), ('  ·  ', PALETTE['faint']),
                (self.state['round']['title'], f"bold {PALETTE['strong']}")))
            self.refresh_list()
            await self.fetch_catalog()
            self.load_question()
            self.apply_layout()
            # Start where the answer goes, never on a button that Enter would press.
            choices = self.query_one('#choices', ChoiceList)
            (choices if choices.display else self.query_one('#answer')).focus()
            self.set_interval(.6, self.tick)
            await self.tick()
        except (OSError, ValueError) as exc:
            self.status(f'Не удалось открыть раунд: {exc}. Закройте и повторите open.', error=True)

    async def notify_agent(self):
        """Wake the agent in its own pane; without Herdr the owner does it by hand."""
        if not (self.session / 'herdr.json').exists():
            return SUBMITTED
        try:
            from grill_herdr import notify_agent
            await asyncio.to_thread(notify_agent, self.session)
            return AGENT_NOTIFIED.format(path=self.session / 'answers.json')
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
            for q in self.state["round"]["questions"]:
                answer = self.state['answers'][q['id']]
                if answer['confirmed']:
                    mark = ('✓', PALETTE['ok'])
                elif answer['selected'] or answer['text'].strip():
                    mark = ('◐', PALETTE['warn'])
                else:
                    mark = ('○', PALETTE['faint'])
                # A grid keeps wrapped titles aligned under the first line.
                row = Table.grid(padding=(0, 1))
                row.add_column(width=1)
                row.add_column(ratio=1)
                row.add_row(Text(mark[0], style=mark[1]), Text(q['title']))
                listing.add_option(row)
            listing.highlighted = self.index
        total = len(self.state['answers'])
        count = sum(a['confirmed'] for a in self.state['answers'].values())
        self.query_one('#progress', Static).update(f'подтверждено {count} из {total}')

    def load_question(self):
        q = self.q
        answer = self.state['answers'][q['id']]
        questions = self.state['round']['questions']
        self.query_one('#question-meta', Static).update(
            f'Вопрос {self.index + 1} из {len(questions)}  ·  {MODES.get(q.get("mode", "single"), "")}')
        self.query_one('#question-title', Static).update(q['title'])
        self.query_one('#question-body', Static).update(q['body'])
        self.query_one('#recommendation', Static).update(Text.assemble(
            ('Рекомендация', f"bold {PALETTE['accent']}"), '\n', q['recommendation']))
        choices = self.query_one('#choices', ChoiceList)
        with self.prevent(TextArea.Changed):
            choices.load(q.get('options', []), answer['selected'], q.get('mode', 'single'),
                         q.get('recommended', []))
            choices.display = q.get('mode') != 'text' and bool(q.get('options'))
            self.query_one('#choices-hint').display = choices.display
            self.query_one('#answer', TextArea).load_text(answer['text'])
            self.query_one('#message', TextArea).load_text(self.state['branches'][q['id']].get('input_draft', ''))
        self.query_one('#question-scroll').scroll_home(animate=False)
        self.chat_fingerprint = None
        self.refresh_chat()
        self.sync_picker()
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
        self.query_one('#agent-picker').disabled = submitted
        answer = self.state['answers'][self.q['id']]
        confirmed = answer['confirmed']
        self.query_one('#confirm', Button).label = 'Подтверждено ✓' if confirmed else 'Подтвердить ответ'
        state = self.query_one('#answer-state', Static)
        state.set_class(confirmed, '-confirmed')
        state.update('Ответ подтверждён' if confirmed else
                     'Черновик, не подтверждён' if answer['selected'] or answer['text'].strip() else 'Ответа пока нет')

    def chat_runtime(self):
        branch = self.state['branches'][self.q['id']]
        return branch['runtime'] if branch.get('thread_id') and branch.get('runtime') else self.state['runtime']

    def refresh_chat(self):
        branch = self.state['branches'][self.q['id']]
        fingerprint = json.dumps(branch.get('messages', []), ensure_ascii=False)
        if fingerprint != self.chat_fingerprint:
            self.chat_fingerprint = fingerprint
            scroll = self.query_one('#chat-scroll', VerticalScroll)
            scroll.query('.msg').remove()
            runtime = self.chat_runtime()
            agent = HARNESSES[runtime['harness']].label if runtime.get('harness') in HARNESSES else 'Агент'
            scroll.mount_all([chat_message(m['role'], m['text'], 'Вы' if m['role'] == 'user' else agent)
                              for m in branch['messages']])
            empty = self.query_one('#chat-empty', Static)
            empty.display = not branch['messages']
            empty.update('Спросите агента об этом вопросе. Он видит только его контекст, '
                         'а переписка не попадёт основному агенту.')
            scroll.call_after_refresh(scroll.scroll_end, animate=False)
        status = self.query_one('#chat-status', Static)
        status.set_class(bool(branch.get('error')), '-error')
        progress = ''
        if branch['status'] == 'running':
            runtime = self.chat_runtime()
            agent = HARNESSES[runtime['harness']].label if runtime.get('harness') in HARNESSES else 'Агент'
            elapsed = int(time.time() - branch['started_at']) if branch.get('started_at') else 0
            progress = f'{agent} отвечает · {elapsed // 60}:{elapsed % 60:02d}'
            if branch.get('activity'):
                progress += ' · ' + branch['activity']
        status.update(branch.get('error') or progress)
        summary = branch.get('summary', '')
        self.query_one('#draft', Static).update(summary)
        self.query_one('#draft').display = bool(summary)
        self.query_one('#draft-buttons').display = bool(summary)

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
            if not self.catalog.get('ready') and time.monotonic() - self.catalog_checked > 5:
                # The server lists models in the background; pick them up once ready.
                self.catalog_checked = time.monotonic()
                await self.fetch_catalog()
                if self.catalog.get('ready'):
                    self.sync_picker()
            self.refresh_chat()
            self.refresh_controls()
            if self.state['submitted']:
                self.status(self.submit_notice)
            elif not self.dirty and not self.draft_dirty:
                self.status(ALL_CONFIRMED if all(a['confirmed'] for a in self.state['answers'].values())
                            else 'Сохранено. Когда ответите на все вопросы, нажмите «Отправить все ответы».')
        except (OSError, ValueError) as exc:
            self.status(f'Нет сохранения на сервере: {exc}. Черновики сохранены локально; сообщения повторно не отправляются.',
                        error=True)
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

    @on(Composer.Submitted)
    async def composer_submitted(self, event):
        if event.composer.id == 'answer':
            await self.confirm_and_advance()
        else:
            self.query_one('#send', Button).press()

    @on(ChoiceList.Submitted)
    async def choices_submitted(self, event):
        await self.confirm_and_advance()

    async def confirm_and_advance(self):
        """Enter: confirm a non-empty answer and move to the next unconfirmed question."""
        if not self.state or self.state['submitted']:
            return
        answer = self.state['answers'][self.q['id']]
        if not answer['selected'] and not answer['text'].strip():
            return  # Nothing chosen or written: Enter does nothing.
        await self.action_confirm()
        if not answer['confirmed']:
            return
        questions = self.state['round']['questions']
        order = questions[self.index + 1:] + questions[:self.index]
        pending = next((q for q in order if not self.state['answers'][q['id']]['confirmed']), None)
        if pending is None:
            self.query_one('#submit', Button).focus()
            self.status(ALL_CONFIRMED)
            return
        self.show_question(questions.index(pending))
        choices = self.query_one('#choices', ChoiceList)
        (choices if choices.display else self.query_one('#answer')).focus()

    def show_question(self, index):
        self.index = index
        self.load_question()
        self.query_one('#question-list', OptionList).highlighted = index
        self.compact_view = 'center'
        self.apply_layout()

    @on(ChoiceList.Toggled)
    def selected(self, event):
        if not self.state or self.state['submitted']:
            return
        choices = event.choices
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
            self.status(f'Черновик сохранён локально: {exc}', error=True)
        self.show_question(event.option_index)

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
            self.status(f'Подтверждение пока не сохранено на сервере: {exc}', error=True)

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
                if (self.session / 'herdr.json').exists():
                    # The round is read-only now and the agent is already working: go there.
                    try:
                        from grill_herdr import return_to_agent
                        await asyncio.to_thread(return_to_agent, self.session)
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        self.status(f'{self.submit_notice} Не удалось переключиться на агента: {exc}', error=True)
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
            self.status(f'Действие не подтверждено: {exc}. Проверьте состояние перед повторной отправкой.', error=True)

    async def fetch_catalog(self):
        try:
            self.catalog = await self.api('/api/catalog')
        except (OSError, ValueError) as exc:
            self.status(f'Список моделей недоступен: {exc}', error=True)

    def sync_picker(self):
        branch = self.state['branches'][self.q['id']]
        chat = branch['runtime'] if branch.get('thread_id') else None
        parent = self.state.get('parent_runtime', self.state['runtime'])
        self.query_one('#agent-picker', AgentPicker).load(self.catalog, self.state['runtime'], parent, chat)

    async def action_agent(self):
        """F5: move to the agent controls of the discussion panel."""
        self.compact_view = 'discussion'
        self.hide_discussion = False
        self.expanded_chat = False
        self.apply_layout()
        self.query_one('#agent-harness', Select).focus()

    @on(AgentPicker.Changed)
    async def agent_changed(self, event):
        if not self.state or self.state['submitted']:
            return
        try:
            await self.api('/api/runtime', {'question_id': self.q['id'], 'runtime': event.runtime,
                                            'restart': event.restart})
            await self.tick()
            if event.restart:
                self.chat_fingerprint = None
                self.refresh_chat()
            self.sync_picker()
            self.status(('Чат начат заново. ' if event.restart else '') +
                        'Агент для новых чатов: ' + label(self.state['runtime']))
        except (OSError, ValueError) as exc:
            self.sync_picker()
            self.status(f'Настройки не применены: {exc}', error=True)

    def action_answer_field(self):
        self.compact_view = 'center'
        self.expanded_chat = False
        self.apply_layout()
        editor = self.query_one('#answer', TextArea)
        # Continue the answer instead of typing in front of it after a reload.
        editor.move_cursor(editor.document.end)
        editor.focus()

    def action_message_field(self):
        self.compact_view = 'discussion'
        self.hide_discussion = False
        self.apply_layout()
        self.query_one('#message').focus()

    def action_questions(self):
        self.compact_view = 'center' if self.compact_view == 'questions' else 'questions'
        self.hide_questions = not self.hide_questions
        self.apply_layout()

    def action_toggle_discussion(self):
        if self.size.width < 110:
            self.compact_view = 'center' if self.compact_view == 'discussion' else 'discussion'
        else:
            self.hide_discussion = not self.hide_discussion
            self.expanded_chat = False
        self.apply_layout()

    def action_discussion(self):
        self.compact_view = 'center' if self.compact_view == 'discussion' else 'discussion'
        if self.size.width >= 110:
            self.expanded_chat = not self.expanded_chat
            self.hide_discussion = False
        self.apply_layout()

    def on_resize(self):
        if self.is_mounted:
            self.call_after_refresh(self.apply_layout)

    def apply_layout(self):
        narrow = self.size.width < 110
        self.set_class(narrow, 'narrow')
        # Wide screens show every column; the view toggles only matter when narrow.
        for ident in ('toggle-questions', 'toggle-chat'):
            self.query_one('#' + ident).display = narrow
        hidden = {'questions': self.hide_questions, 'center': False, 'discussion': self.hide_discussion}
        for name in ('questions', 'center', 'discussion'):
            self.query_one('#' + name).display = name == self.compact_view if narrow else (
                name == 'discussion' if self.expanded_chat else not hidden[name])
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
