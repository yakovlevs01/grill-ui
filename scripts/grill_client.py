"""Local authenticated API client shared by the TUI and lifecycle commands."""
import json
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlsplit
from urllib.error import HTTPError


class Client:
    def __init__(self, session):
        self.session = Path(session).resolve()
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, data=None):
        info = json.loads((self.session / 'server.json').read_text())
        url = urlsplit(info['url'])
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.fragment:
            raise ValueError('Invalid local grill endpoint')
        req = Request(f'http://{url.netloc}{path}',
            headers={'X-Grill-Token': url.fragment, 'Content-Type': 'application/json'},
            data=None if data is None else json.dumps(data, ensure_ascii=False).encode())
        try:
            with self.opener.open(req, timeout=3) as response:
                return json.load(response)
        except HTTPError as exc:
            with exc:
                try:
                    message = json.load(exc).get('error', str(exc))
                except (ValueError, OSError):
                    message = str(exc)
            raise ValueError(message) from None

    def healthy(self):
        try:
            return self.request('/api/health')['session'] == str(self.session)
        except (OSError, ValueError, KeyError):
            return False
