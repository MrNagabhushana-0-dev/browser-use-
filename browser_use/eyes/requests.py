"""What the page fetched: a passive, redacted log of a tab's network requests (after BrowserSkill's network evidence).

It only listens to Network events; nothing is intercepted, delayed or changed. Bodies are read on request, one at a
time, and redacted before they are cut to size, so a secret is never half kept. Redaction goes by key name (password,
token, api_key, ...) like BrowserSkill's, and also by value: a JWT, bearer token, API key or card number is masked
wherever it sits, under an innocent key or in free text.
"""

import json
import logging
import re
from collections import deque
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

MASK = '[redacted]'
BODY_CHARS = 3000
JSON_PARSE_LIMIT = 512 * 1024  # bigger JSON is not parsed, so it is not shown: a cut could keep half a secret

SECRET_KEY = re.compile(
	r'^(?:authorization|proxy-authorization|cookie|set-cookie|password|passwd|pwd|pass|secret|client[_-]?secret'
	r'|(?:(?:access|refresh|id|auth|csrf|xsrf|bearer|session)[_-]?)?token|api[_-]?key|apikey|x-api-key|private[_-]?key'
	r'|session(?:[_-]?(?:id|key))?|sid|otp|one[_-]?time[_-]?code|cvc|cvv|card[_-]?number|ssn|credentials?|signature|sig)$',
	re.I,
)
SECRET_VALUES = [
	re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----'),
	re.compile(r'\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}'),  # JWT
	re.compile(r'\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}'),  # Stripe-style
	re.compile(r'\bsk-[A-Za-z0-9_-]{20,}'),  # OpenAI/Anthropic-style
	re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}'),  # GitHub
	re.compile(r'\bAKIA[0-9A-Z]{16}\b'),  # AWS access key id
	re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b'),  # Google API key
	re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}'),  # Slack
]
BEARER = re.compile(r'\b(Bearer|Basic|Token)\s+[\w.+/~=-]{8,}', re.I)
KEY_VALUE = re.compile(
	r'((?:password|passwd|pwd|secret|client_secret|(?:access|refresh|id|auth|csrf)?_?token|api[_-]?key|session_?id)'
	r'["\']?\s*[=:]\s*)("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s,;&}<]+)',
	re.I,
)
CARD = re.compile(r'\b(?:\d[ -]?){12,18}\d\b')
API_TYPES = frozenset({'XHR', 'Fetch', 'EventSource', 'WebSocket'})


def _luhn(digits: str) -> bool:
	total = 0
	for i, d in enumerate(reversed(digits)):
		n = int(d) * (2 if i % 2 else 1)
		total += n - 9 if n > 9 else n
	return total % 10 == 0


def secret_key(key: str) -> bool:
	"""Whether a field name (JSON key, query or form parameter, header) names a secret. camelCase and a.b[c] paths count."""
	parts = re.split(r'[.\[\]]+', re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', key))
	return any(
		SECRET_KEY.match(p) or re.search(r'(?:^|[_-])(?:password|passwd|secret|token)(?:[_-]|$)', p, re.I) for p in parts if p
	)


def redact_text(text: str) -> str:
	"""Mask secrets by what they look like, wherever they are."""
	for pattern in SECRET_VALUES:
		text = pattern.sub(MASK, text)
	text = BEARER.sub(lambda m: f'{m.group(1)} {MASK}', text)
	text = KEY_VALUE.sub(lambda m: m.group(1) + MASK, text)

	def card(m: re.Match) -> str:
		digits = re.sub(r'\D', '', m.group(0))
		return MASK if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

	return CARD.sub(card, text)


def redact_url(url: str) -> str:
	try:
		parts = urlsplit(url)
	except ValueError:
		return redact_text(url)
	netloc = parts.hostname or ''
	if ':' in netloc:
		netloc = f'[{netloc}]'
	if parts.port:
		netloc += f':{parts.port}'
	query = urlencode([(k, MASK if secret_key(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)], safe='[]/:')
	return redact_text(urlunsplit((parts.scheme, netloc, parts.path, query, '')))


def _redact_json(value: Any, depth: int = 0) -> Any:
	if depth > 30:
		return '[too deep]'
	if isinstance(value, dict):
		return {k: MASK if secret_key(str(k)) else _redact_json(v, depth + 1) for k, v in value.items()}
	if isinstance(value, list):
		return [_redact_json(v, depth + 1) for v in value]
	if isinstance(value, str):
		return redact_text(value)
	return value


def redact_body(text: str, mime: str) -> tuple[str, bool]:
	"""(redacted text, whether anything was masked). Redacted whole, before any cut."""
	if 'json' in mime or text.lstrip()[:1] in ('{', '['):
		if len(text) > JSON_PARSE_LIMIT:
			return f'[JSON body of {len(text):,} characters not shown: too big to check for secrets]', True
		try:
			parsed = json.loads(text)
		except ValueError:
			pass
		else:
			out = json.dumps(_redact_json(parsed), ensure_ascii=False, separators=(',', ':'))
			return out, out != json.dumps(parsed, ensure_ascii=False, separators=(',', ':'))
	if 'x-www-form-urlencoded' in mime:
		pairs = parse_qsl(text, keep_blank_values=True)
		out = urlencode([(k, MASK if secret_key(k) else redact_text(v)) for k, v in pairs], safe='[]')
		return out, out != urlencode(pairs)
	if 'html' in mime:
		# A server-rendered secret field's initial value would be in its value attribute.
		def field(m: re.Match) -> str:
			tag = m.group(0)
			attrs = ' '.join(re.findall(r'\b(?:type|name|id|autocomplete)\s*=\s*["\']?([^"\'\s>]+)', tag, re.I))
			if re.search(r'password|one-time-code|cc-|secret|token', attrs, re.I) or any(secret_key(a) for a in attrs.split()):
				return re.sub(r'\bvalue\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', f'value="{MASK}"', tag, flags=re.I)
			return tag

		out = redact_text(re.sub(r'<input\b[^>]*>', field, text, flags=re.I))
		return out, out != text
	out = redact_text(text)
	return out, out != text


class Request(BaseModel):
	"""One request the page made, as recorded from Network events."""

	model_config = ConfigDict(extra='forbid')

	seq: int
	request_id: str
	method: str
	url: str  # redacted
	type: str = ''
	status: int | None = None
	mime: str = ''
	bytes: int | None = None
	ms: float | None = None
	error: str = ''
	from_cache: bool = False
	started: float = 0.0  # CDP monotonic seconds

	@property
	def failed(self) -> bool:
		return bool(self.error) or (self.status or 0) >= 400

	@property
	def api(self) -> bool:
		return self.type in API_TYPES or 'json' in self.mime


class RequestLog:
	"""Records what one tab requests, from Network events only. Bounded (oldest dropped); never intercepts."""

	def __init__(self, browser_session: Any, limit: int = 400):
		self.browser_session = browser_session
		self.limit = limit
		self._by_id: dict[str, Request] = {}
		self._order: deque[Request] = deque()
		self._seq = 0
		self._session_id: str | None = None
		self._cdp: Any = None
		self._restore: list[tuple[str, Any, Any]] = []

	@property
	def running(self) -> bool:
		return bool(self._restore)

	@property
	def last_seq(self) -> int:
		return self._seq

	async def start(self, target_id: str | None = None) -> None:
		if self.running:
			return
		cdp = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
		self._cdp, self._session_id = cdp, cdp.session_id
		for method, fn in (
			('Network.requestWillBeSent', self._sent),
			('Network.responseReceived', self._response),
			('Network.loadingFinished', self._finished),
			('Network.loadingFailed', self._failed),
		):
			self._chain(method, fn)
		await cdp.cdp_client.send.Network.enable(session_id=self._session_id)  # left on at stop: others use it too

	async def stop(self) -> None:
		registry = self.browser_session.cdp_client._event_registry
		for method, incumbent, ours in reversed(self._restore):
			if registry._handlers.get(method) is ours:
				if incumbent is not None:
					registry.register(method, incumbent)
				else:
					registry.unregister(method)
		self._restore.clear()

	def _chain(self, method: str, fn: Any) -> None:
		# cdp-use keeps one callback per event (downloads and HAR watchdogs live on Network events): chain, don't take.
		registry = self.browser_session.cdp_client._event_registry
		incumbent = registry._handlers.get(method)

		def both(event: Any, session_id: str | None = None) -> Any:
			if session_id == self._session_id:
				try:
					fn(event)
				except Exception as e:
					logger.debug(f'request log: {method} failed: {e}')
			return incumbent(event, session_id) if incumbent is not None else None

		self._restore.append((method, incumbent, both))
		registry.register(method, both)

	# -- events --------------------------------------------------------------------------

	def _sent(self, e: dict) -> None:
		rid = e['requestId']
		old = self._by_id.get(rid)
		if old is not None and e.get('redirectResponse'):  # a redirect: the old hop ends here, the next one starts
			old.status = e['redirectResponse'].get('status')
			old.ms = round((e.get('timestamp', 0) - old.started) * 1000, 1)
			del self._by_id[rid]
		self._seq += 1
		req = Request(
			seq=self._seq,
			request_id=rid,
			method=e.get('request', {}).get('method', 'GET'),
			url=redact_url(e.get('request', {}).get('url', '')),
			type=e.get('type', ''),
			started=e.get('timestamp', 0.0),
		)
		self._by_id[rid] = req
		self._order.append(req)
		while len(self._order) > self.limit:
			gone = self._order.popleft()
			if self._by_id.get(gone.request_id) is gone:
				del self._by_id[gone.request_id]

	def _response(self, e: dict) -> None:
		req = self._by_id.get(e['requestId'])
		if req is None:
			return
		r = e.get('response', {})
		req.status, req.mime = r.get('status'), r.get('mimeType', '')
		req.from_cache = bool(r.get('fromDiskCache') or r.get('fromServiceWorker') or r.get('fromPrefetchCache'))
		req.type = req.type or e.get('type', '')

	def _finished(self, e: dict) -> None:
		req = self._by_id.get(e['requestId'])
		if req is not None:
			req.bytes = int(e.get('encodedDataLength', 0))
			req.ms = round((e.get('timestamp', req.started) - req.started) * 1000, 1)

	def _failed(self, e: dict) -> None:
		req = self._by_id.get(e['requestId'])
		if req is not None:
			req.error = (
				'blocked' if e.get('blockedReason') else ('canceled' if e.get('canceled') else e.get('errorText', 'failed'))
			)
			req.ms = round((e.get('timestamp', req.started) - req.started) * 1000, 1)

	# -- reading -------------------------------------------------------------------------

	def entries(self, since: int = 0, only: str = 'all', limit: int = 30) -> list[Request]:
		assert only in ('all', 'failed', 'api'), only
		picked = [r for r in self._order if r.seq > since and (only == 'all' or getattr(r, only))]
		return picked[-limit:] if limit > 0 else picked

	async def body(self, seq: int, max_chars: int = BODY_CHARS) -> str:
		req = next((r for r in self._order if r.seq == seq), None)
		if req is None:
			return (
				f'No request #{seq} in the log (it holds #{self._order[0].seq}-#{self._seq}).'
				if self._order
				else 'The log is empty.'
			)
		assert self._cdp is not None
		try:
			got = await self._cdp.cdp_client.send.Network.getResponseBody(
				params={'requestId': req.request_id}, session_id=self._session_id
			)
		except Exception as e:
			return f'#{seq} has no body to read ({str(e)[:120]}).'
		if got.get('base64Encoded'):
			return f'#{seq} is binary ({req.mime or "unknown type"}, {req.bytes or 0:,} bytes on the wire); not shown.'
		text, masked = redact_body(got.get('body', ''), req.mime)
		cut = len(text) > max_chars
		note = f' (secrets masked as {MASK})' if masked else ''
		tail = f'\n[... {len(text) - max_chars:,} more characters]' if cut else ''
		return f'#{seq} {req.method} {req.url} -> {req.status} {req.mime}{note}:\n{text[:max_chars]}{tail}'


def render(requests: list[Request], since: int, last_seq: int, page_url: str = '') -> str:
	"""Compact lines, one per request, plus where to carry on from."""
	origin = '{0.scheme}://{0.netloc}'.format(urlsplit(page_url)) if page_url.startswith('http') else ''
	failed = sum(r.failed for r in requests)
	head = f'{len(requests)} request{"" if len(requests) == 1 else "s"} after #{since}' + (f', {failed} failed' if failed else '')
	lines = [head + f'. Next: since={last_seq}.']
	for r in requests:
		url = r.url[len(origin) :] or '/' if origin and r.url.startswith(origin) else r.url
		outcome = f'✗ {r.error}' if r.error else str(r.status or '...')
		size = f' · {r.bytes:,} B' if r.bytes else ''
		ms = f' · {r.ms:.0f} ms' if r.ms is not None else ''
		cache = ' · cache' if r.from_cache else ''
		lines.append(
			f'#{r.seq} {r.method} {outcome} {r.type.lower() or "?"} {url[:160]}{" · " + r.mime if r.mime else ""}{size}{ms}{cache}'
		)
	return '\n'.join(lines)
