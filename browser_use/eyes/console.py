"""What the page said in its console: a passive log of a tab's console messages, uncaught exceptions and the browser's
own log entries (a 404 for an image, a blocked script), after Claude in Chrome's read_console_messages.

It only listens. Text is masked like network bodies (browser_use/eyes/requests.py): keys, tokens and card numbers by
name and by shape, because pages log them more often than they should.
"""

import re
from collections import deque
from typing import Any

from pydantic import BaseModel, ConfigDict

from browser_use.eyes.listen import TabListener
from browser_use.eyes.requests import redact_text

LEVELS = {'debug': 0, 'log': 0, 'verbose': 0, 'info': 1, 'warning': 2, 'error': 3}
TEXT_CHARS = 600


class ConsoleEntry(BaseModel):
	model_config = ConfigDict(extra='forbid')

	seq: int
	level: str  # log, info, warning, error
	source: str  # console, exception, or the browser's own source (network, javascript, security, ...)
	text: str  # masked, cut to TEXT_CHARS
	where: str = ''  # url:line, when known

	def line(self) -> str:
		place = f' ({self.where})' if self.where else ''
		origin = '' if self.source == 'console' else f'[{self.source}] '
		return f'#{self.seq} {self.level} {origin}{self.text}{place}'


def _level(name: str) -> str:
	name = {'warn': 'warning', 'verbose': 'log', 'debug': 'log', 'trace': 'log', 'dir': 'log', 'table': 'log'}.get(name, name)
	return name if name in LEVELS else 'log'


def _value(arg: dict[str, Any]) -> str:
	if 'value' in arg:
		return str(arg['value'])
	return str(arg.get('description') or arg.get('unserializableValue') or arg.get('type', ''))


def _where(url: str | None, line: int | None) -> str:
	if not url:
		return ''
	short = url if len(url) <= 80 else '...' + url[-77:]
	return f'{short}:{line + 1}' if isinstance(line, int) else short


class ConsoleLog(TabListener):
	"""Console messages, exceptions and browser log entries of one tab, oldest dropped past `limit`."""

	def __init__(self, browser_session: Any, limit: int = 300):
		super().__init__(browser_session)
		self.limit = limit
		self._entries: deque[ConsoleEntry] = deque()
		self._seq = 0

	@property
	def last_seq(self) -> int:
		return self._seq

	def events(self) -> list[tuple[str, Any]]:
		return [
			('Runtime.consoleAPICalled', self._console),
			('Runtime.exceptionThrown', self._exception),
			('Log.entryAdded', self._browser),
		]

	async def enable(self, cdp: Any) -> None:
		await cdp.cdp_client.send.Runtime.enable(session_id=self._session_id)
		await cdp.cdp_client.send.Log.enable(session_id=self._session_id)

	def _add(self, level: str, source: str, text: str, where: str = '') -> None:
		self._seq += 1
		clean = redact_text(' '.join(text.split()))
		cut = clean if len(clean) <= TEXT_CHARS else clean[: TEXT_CHARS - 1] + '…'
		self._entries.append(ConsoleEntry(seq=self._seq, level=level, source=source, text=cut, where=where))
		while len(self._entries) > self.limit:
			self._entries.popleft()

	def _console(self, e: dict) -> None:
		frames = (e.get('stackTrace') or {}).get('callFrames') or [{}]
		text = ' '.join(_value(a) for a in e.get('args', []))
		self._add(_level(e.get('type', 'log')), 'console', text, _where(frames[0].get('url'), frames[0].get('lineNumber')))

	def _exception(self, e: dict) -> None:
		d = e.get('exceptionDetails', {})
		text = (d.get('exception') or {}).get('description') or d.get('text') or 'exception'
		self._add('error', 'exception', text.split('\n    at ')[0], _where(d.get('url'), d.get('lineNumber')))

	def _browser(self, e: dict) -> None:
		entry = e.get('entry', {})
		self._add(
			_level(entry.get('level', 'info')),
			entry.get('source', 'other'),
			entry.get('text', ''),
			_where(entry.get('url'), entry.get('lineNumber')),
		)

	def entries(self, since: int = 0, level: str = 'all', pattern: str = '', limit: int = 30) -> list[ConsoleEntry]:
		"""`level`: all, warning (warnings and errors) or error. `pattern`: a regular expression, case-insensitive."""
		assert level in ('all', 'warning', 'error'), level
		floor = 0 if level == 'all' else LEVELS[level]
		try:
			match = re.compile(pattern, re.I) if pattern else None
		except re.error as e:
			raise ValueError(f'pattern is not a regular expression: {e}') from e
		picked = [
			x for x in self._entries if x.seq > since and LEVELS[x.level] >= floor and (match is None or match.search(x.text))
		]
		return picked[-limit:] if limit > 0 else picked


def render(entries: list[ConsoleEntry], since: int, last_seq: int) -> str:
	errors = sum(e.level == 'error' for e in entries)
	head = f'{len(entries)} console entr{"y" if len(entries) == 1 else "ies"} after #{since}'
	head += f', {errors} error{"" if errors == 1 else "s"}' if errors else ''
	return '\n'.join([f'{head}. Next: since={last_seq}.', *(e.line() for e in entries)])
