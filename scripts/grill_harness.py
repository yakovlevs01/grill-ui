"""Headless agent CLIs for isolated question chats. Python 3.11+, no deps.

Each harness knows how to find the caller's model/effort, list what the
installed CLI offers, build one read-only turn and read its JSONL events.
Model lists come from the CLI itself, so new models need no code change.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

MODEL_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}$')
EFFORT_ID = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
SESSION_ID = re.compile(r'^[A-Za-z0-9-]{1,128}$')
# Parent-session markers. A chat is a new conversation, not a child of the caller.
PARENT_ENV = ('CODEX_THREAD_ID', 'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_EFFORT',
              'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_CHILD_SESSION',
              'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_CODE_MESSAGING_SOCKET',
              'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDE_CODE_EXECPATH')


def require(value, message):
    if not value:
        raise ValueError(message)


def short(text, limit=60):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def output_of(argv, timeout=20):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


class Harness:
    id = label = binary = ''
    fallback_efforts = ['low', 'medium', 'high']

    def available(self):
        return bool(shutil.which(self.binary))

    def env(self):
        return {k: v for k, v in os.environ.items() if k not in PARENT_ENV}

    def activity(self, event):
        """A short phrase for what the agent is doing now, or None."""
        return None

    def entry(self, models, efforts):
        return {'id': self.id, 'label': self.label, 'available': self.available(),
                'models': models, 'efforts': efforts}


class Codex(Harness):
    id, label, binary = 'codex', 'Codex', 'codex'
    fallback_efforts = ['low', 'medium', 'high', 'xhigh']

    def in_parent(self):
        return bool(os.environ.get('CODEX_THREAD_ID'))

    def parent(self, session=None):
        session = session or os.environ.get('CODEX_THREAD_ID')
        if not session:
            return {}
        require(SESSION_ID.match(session), 'Invalid parent thread ID')
        root = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        current = {}
        for folder in ('sessions', 'archived_sessions'):
            for path in (root / folder).rglob(f'*{session}*.jsonl'):
                with path.open() as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get('type') == 'turn_context':
                            current = event.get('payload', {})
        settings = current.get('collaboration_mode', {}).get('settings', {})
        found = {'session': session, 'model': current.get('model') or settings.get('model'),
                 'effort': current.get('effort') or settings.get('reasoning_effort')}
        if current.get('service_tier'):
            found['service_tier'] = current['service_tier']
        return found

    def catalog(self):
        raw = self.available() and output_of([self.binary, 'debug', 'models'])
        try:
            listed = json.loads(raw)['models'] if raw else []
        except (ValueError, KeyError, TypeError):
            listed = []
        models, efforts = [], []
        for m in listed:
            if not isinstance(m, dict) or m.get('visibility') == 'hide' or not MODEL_ID.match(str(m.get('slug', ''))):
                continue
            levels = [l.get('effort') for l in m.get('supported_reasoning_levels', []) if isinstance(l, dict)]
            levels = [l for l in levels if isinstance(l, str) and EFFORT_ID.match(l)] or self.fallback_efforts
            models.append({'id': m['slug'], 'label': m.get('display_name') or m['slug'],
                           'efforts': levels, 'default_effort': m.get('default_reasoning_level')})
            efforts += [l for l in levels if l not in efforts]
        return self.entry(models, efforts or self.fallback_efforts)

    def command(self, runtime, session):
        cmd = [self.binary, 'exec', '-m', runtime['model'],
               '-c', 'model_reasoning_effort=' + json.dumps(runtime['effort']),
               '-c', 'sandbox_mode="read-only"', '-c', 'approval_policy="never"']
        if runtime.get('service_tier'):
            cmd += ['-c', 'service_tier=' + json.dumps(runtime['service_tier'])]
        if session:
            cmd += ['resume', session]
        return cmd + ['--skip-git-repo-check', '--json', '-']

    def activity(self, event):
        kind, item = event.get('type'), event.get('item') or {}
        if kind == 'turn.started':
            return 'думает'
        if kind != 'item.started' and not (kind == 'item.completed' and item.get('type') == 'agent_message'):
            return None
        kind = item.get('type')
        if kind == 'command_execution':
            # Codex wraps commands as `<shell> -lc "<command>"`; show the command itself.
            command = str(item.get('command', ''))
            inner = re.search(r'-lc\s+([\'"])(.*)\1\s*$', command, re.S)
            return 'выполняет ' + short(inner.group(2) if inner else command)
        return {'reasoning': 'думает', 'file_change': 'меняет файлы', 'web_search': 'ищет в интернете',
                'mcp_tool_call': 'вызывает ' + short(item.get('tool', 'инструмент'), 40),
                'agent_message': 'пишет ответ'}.get(kind)

    def parse(self, event):
        """Return (session ID, assistant text, failure) found in one event."""
        kind = event.get('type')
        if kind == 'thread.started':
            return event.get('thread_id'), None, None
        if kind == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
            return None, event['item'].get('text', ''), None
        if kind in ('error', 'turn.failed'):
            return None, None, event.get('message') or str(event.get('error', 'Codex turn failed'))
        return None, None, None


class Claude(Harness):
    id, label, binary = 'claude', 'Claude Code', 'claude'
    fallback_efforts = ['low', 'medium', 'high', 'xhigh', 'max']
    # Aliases always resolve to the newest model of the family.
    aliases = [('opus', 'opus · последняя Opus'), ('sonnet', 'sonnet · последняя Sonnet'),
               ('haiku', 'haiku · последняя Haiku')]

    def in_parent(self):
        return bool(os.environ.get('CLAUDE_CODE_SESSION_ID') or os.environ.get('CLAUDECODE'))

    def parent(self, session=None):
        own = os.environ.get('CLAUDE_CODE_SESSION_ID')
        session = session or own
        if not session:
            return {}
        require(SESSION_ID.match(session), 'Invalid parent session ID')
        root = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))) / 'projects'
        model = None
        for path in root.glob(f'*/{session}.jsonl'):
            with path.open() as stream:
                for line in stream:
                    if '"assistant"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    found = event.get('message', {}).get('model') if event.get('type') == 'assistant' else None
                    # Synthetic entries such as local errors carry "<synthetic>".
                    if isinstance(found, str) and MODEL_ID.match(found):
                        model = found
        # CLAUDE_EFFORT describes this process's session only.
        return {'session': session, 'model': model,
                'effort': os.environ.get('CLAUDE_EFFORT') if session == own else None}

    def catalog(self):
        help_text = (self.available() and output_of([self.binary, '--help'])) or ''
        match = re.search(r'--effort\s+<\w+>[^()]*\(([a-z0-9_, -]+)\)', help_text)
        efforts = [e.strip() for e in match.group(1).split(',')] if match else []
        efforts = [e for e in efforts if EFFORT_ID.match(e)] or self.fallback_efforts
        models = [{'id': alias, 'label': label, 'efforts': efforts, 'default_effort': None}
                  for alias, label in self.aliases]
        return self.entry(models, efforts)

    def command(self, runtime, session):
        cmd = [self.binary, '-p', '--output-format', 'stream-json', '--verbose',
               '--model', runtime['model'], '--effort', runtime['effort'],
               # Read-only discussion: no edits, shell, MCP servers or skills.
               '--tools', 'Read,Grep,Glob', '--strict-mcp-config', '--disable-slash-commands']
        if session:
            cmd += ['--resume', session]
        return cmd

    def activity(self, event):
        if event.get('type') != 'assistant':
            return None
        blocks = (event.get('message') or {}).get('content') or []
        found = None
        for block in blocks if isinstance(blocks, list) else []:
            kind = block.get('type') if isinstance(block, dict) else None
            if kind == 'tool_use':
                name, args = block.get('name', ''), block.get('input') or {}
                if name == 'Read':
                    found = 'читает ' + short(Path(str(args.get('file_path', ''))).name or 'файл', 50)
                elif name == 'Grep':
                    found = 'ищет «' + short(args.get('pattern', ''), 40) + '»'
                elif name == 'Glob':
                    found = 'ищет файлы ' + short(args.get('pattern', ''), 40)
                else:
                    found = 'вызывает ' + short(name, 40)
            elif kind == 'thinking':
                found = 'думает'
            elif kind == 'text':
                found = 'пишет ответ'
        return found

    def parse(self, event):
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            return event.get('session_id'), None, None
        if kind == 'result':
            if event.get('is_error'):
                return event.get('session_id'), None, event.get('result') or 'Claude Code turn failed'
            return event.get('session_id'), event.get('result') or '', None
        return None, None, None


HARNESSES = {h.id: h for h in (Codex(), Claude())}


def label(runtime):
    harness = HARNESSES.get(runtime.get('harness'))
    return f"{harness.label if harness else runtime.get('harness')} · {runtime.get('model')} · {runtime.get('effort')}"


def detect():
    found = [h.id for h in HARNESSES.values() if h.in_parent()]
    require(len(found) <= 1, 'Both Codex and Claude Code sessions are visible in the environment; pass --harness')
    return found[0] if found else None


def catalog():
    return {h.id: h.catalog() for h in HARNESSES.values()}


def validate(runtime, known=None):
    """Check an owner's choice; `known` is the catalog, when it has loaded."""
    require(isinstance(runtime, dict), 'Invalid agent settings')
    harness = HARNESSES.get(runtime.get('harness'))
    require(harness, 'Unknown harness')
    require(harness.available(), f'{harness.label} CLI is not installed')
    model, effort = runtime.get('model'), runtime.get('effort')
    require(isinstance(model, str) and MODEL_ID.match(model), 'Invalid model ID')
    require(isinstance(effort, str) and EFFORT_ID.match(effort), 'Invalid effort')
    entry = (known or {}).get(harness.id)
    if entry:
        listed = next((m for m in entry['models'] if m['id'] == model), None)
        allowed = listed['efforts'] if listed else entry['efforts']
        require(effort in allowed, f'{model} does not support effort {effort}')
    return {'harness': harness.id, 'model': model, 'effort': effort}
