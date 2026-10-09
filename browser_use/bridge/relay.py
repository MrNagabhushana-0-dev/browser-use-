"""Local relay between CDP clients and the Retinat bridge extension running in the person's own browser.

The extension holds a `chrome.debugger` session on each tab the person shares and relays CDP over a WebSocket.
This relay turns that into a browser-level CDP endpoint (`/json/version`, `Target.*`, `Browser.getVersion`), so an
unchanged `BrowserSession(cdp_url=relay.cdp_url)` - and so Retinat and the MCP servers - attach to those tabs.

The browser is launched by the person, not by us: no `--remote-debugging-port`, no `--enable-automation`, their
own profile and logins. The AI sees only shared tabs, and `policy.refusal` limits it to what a person could do.
"""

import asyncio
import itertools
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from browser_use.bridge.policy import FILES, PASSIVE_GLOBS, POLICY_FILE, refusal

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9333
# Fixed by the "key" in extension/manifest.json, so the relay can tell its own extension from any other caller.
EXTENSION_ID = 'lcdhfliibkimhbimdfhogcmjedlkoemg'
LOCAL_HOSTS = ('127.0.0.1', 'localhost', '[::1]')
MAX_MESSAGE = 200 * 1024 * 1024
# An MV3 service worker is stopped after 30 s without extension events; a message its own JS handles resets that
# (Chrome 116+), protocol-level pings do not. Measured: without this, an idle worker dropped off at 30 s.
KEEPALIVE_S = 20.0
EXPECTED_VERSION: str = json.loads((POLICY_FILE.parent / 'manifest.json').read_text())['version']
# Cookie reads any tab can make; their results are cut down to the shared sites (see _only_shared_sites).
COOKIE_READS = frozenset({'Network.getCookies', 'Network.getAllCookies', 'Storage.getCookies'})
COOKIE_HEADERS = frozenset({'cookie', 'set-cookie', 'cookie2', 'set-cookie2'})
COOKIE_LISTS = ('associatedCookies', 'blockedCookies', 'exemptedCookies')
# Domains whose calls name the site whose data they read (securityOrigin, storageKey, ...), for any site at all.
SITE_DATA_DOMAINS = frozenset({'DOMStorage', 'IndexedDB', 'CacheStorage', 'Storage'})
SITE_KEYS = ('securityOrigin', 'storageKey', 'origin', 'ownerOrigin')
BROWSER_TARGET = {'targetId': 'browser', 'type': 'browser', 'title': '', 'url': '', 'attached': True, 'canAccessOpener': False}


class BridgeError(Exception):
	"""A CDP call through the bridge failed; the message is shown to the CDP client as the error."""


@dataclass
class _Client:
	"""One CDP client connection and the sessions it holds."""

	ws: web.WebSocketResponse
	discover: bool = False
	auto_attach: bool = False
	sessions: dict[str, int] = field(default_factory=dict)  # session id -> tab id
	by_tab: dict[int, str] = field(default_factory=dict)  # tab id -> session id
	outbox: asyncio.Queue = field(default_factory=asyncio.Queue)  # keeps events and replies in send order


class BridgeRelay:
	"""Serves a browser-level CDP endpoint backed by the tabs the person shares through the extension.

	>>> relay = BridgeRelay()
	>>> await relay.start()
	>>> await relay.wait_for_extension()
	>>> session = BrowserSession(cdp_url=relay.cdp_url)
	"""

	def __init__(
		self,
		host: str = '127.0.0.1',
		port: int = DEFAULT_PORT,
		extension_ids: frozenset[str] = frozenset({EXTENSION_ID}),
		command_timeout: float = 30.0,
	):
		assert host in ('127.0.0.1', 'localhost', '::1'), 'the bridge listens on loopback only'
		self.host = host
		self.port = port
		self.extension_ids = extension_ids
		self.command_timeout = command_timeout
		self.hello: dict[str, Any] = {}
		self.holder: str = 'agent'
		self.stopped = False  # the person pressed Cancel on the debugging bar; cleared when they share again
		self.tabs: dict[int, dict[str, Any]] = {}  # tab id -> CDP TargetInfo
		self._token = secrets.token_hex(16)
		self._children: dict[str, int] = {}  # child (OOPIF/worker) session id -> tab id
		self._clients: list[_Client] = []
		self._ext: web.WebSocketResponse | None = None
		self._ext_ready = asyncio.Event()
		self._some_tab = asyncio.Event()
		self._pending: dict[int, asyncio.Future] = {}
		self._ids = itertools.count(1)
		self._runner: web.AppRunner | None = None
		self._tasks: set[asyncio.Task] = set()
		self._reload_asked: set[tuple[str, str]] = set()  # (version, policy) an extension was asked to reload from

	# -- lifecycle ---------------------------------------------------------------------------------

	@property
	def cdp_url(self) -> str:
		"""The http URL to hand to BrowserSession(cdp_url=...) or `retinat --cdp-url`."""
		return f'http://127.0.0.1:{self.port}'

	@property
	def human_driving(self) -> bool:
		return self.holder == 'human'

	async def start(self) -> 'BridgeRelay':
		app = web.Application()
		app.router.add_get('/json/version', self._version)
		app.router.add_get('/json/version/', self._version)
		app.router.add_get('/json', self._list)
		app.router.add_get('/json/list', self._list)
		app.router.add_get('/bridge/status', self._status)
		app.router.add_get('/extension', self._extension_socket)
		app.router.add_get(f'/cdp/{self._token}', self._client_socket)
		self._runner = web.AppRunner(app, access_log=None)
		await self._runner.setup()
		site = web.TCPSite(self._runner, self.host, self.port)
		await site.start()
		if self.port == 0:
			self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
		self._log_listening()
		return self

	async def stop(self) -> None:
		for client in list(self._clients):
			await client.ws.close()
		if self._ext is not None:
			await self._ext.close()
		for task in list(self._tasks):
			task.cancel()
		if self._runner is not None:
			await self._runner.cleanup()
			self._runner = None

	async def wait_for_extension(self, timeout: float = 60.0) -> dict[str, Any]:
		"""Wait until the extension in the person's browser has dialled in; returns its hello."""
		await asyncio.wait_for(self._ext_ready.wait(), timeout)
		return self.hello

	async def wait_for_tab(self, timeout: float = 60.0) -> dict[str, Any]:
		"""Wait until the person has shared at least one tab; returns its TargetInfo."""
		await asyncio.wait_for(self._some_tab.wait(), timeout)
		return next(iter(self.tabs.values()))

	async def status(self, ping_timeout: float = 3.0) -> dict[str, Any]:
		"""Each link of person -> extension -> relay -> AI as plain data, for `doctor`. Pings the extension."""
		extension = None
		if self._ext is not None and self.hello:
			answers_ms, sites = None, None
			try:
				started = asyncio.get_running_loop().time()
				sites = (await asyncio.wait_for(self._ext_call('ping'), ping_timeout)).get('sites')
				answers_ms = round((asyncio.get_running_loop().time() - started) * 1000, 1)
			except (BridgeError, TimeoutError):
				pass
			extension = {
				'version': self.hello.get('extension'),
				'manifest': self.hello.get('manifest'),
				'userAgent': self.hello.get('userAgent', ''),
				'answers_ms': answers_ms,
				'sites': sites,  # allowed, always, declined, and the site being asked about
			}
		return {
			'relay': 'retinat-bridge',
			'url': self.cdp_url,
			'clients': len(self._clients),
			'expected_version': EXPECTED_VERSION,
			'expected_policy': list(PASSIVE_GLOBS),
			'extension': extension,
			'policy': self.hello.get('policy') if extension else None,
			'holder': self.holder,
			'stopped': self.stopped,
			'tabs': [{'title': t.get('title', ''), 'url': t.get('url', '')} for t in self.tabs.values()],
		}

	async def forget(self, site: str) -> dict[str, Any]:
		"""Take a site (an origin) back: no longer allowed, always allowed or declined, and a shared tab on it stops
		being shared. It only ever takes access away. Returns the sites as they are now."""
		return (await self._ext_call('forget', site=site)).get('sites') or {}

	async def ask_every_time(self, site: str) -> dict[str, Any]:
		"""Have the person asked about `site` (an origin, or `https://*.example.com`) on every visit from now on, with no
		Always: it leaves the allowed and Always lists. It only ever takes access away; only the person can stop it, from
		the extension's Sites list. Returns the sites as they are now."""
		return (await self._ext_call('everyTime', site=site)).get('sites') or {}

	def set_holder(self, holder: str) -> None:
		"""Who drives the shared tabs. The extension's popup and shortcut set this too."""
		assert holder in ('agent', 'human'), holder
		self.holder = holder

	# -- HTTP discovery ----------------------------------------------------------------------------

	def _local_only(self, request: web.Request) -> None:
		# Host check defeats DNS rebinding. Browsers send Origin from web pages and extensions alike; CDP clients
		# (BrowserSession, Retinat) do not. Our own extension never uses these endpoints: it has /extension.
		host = (request.host or '').rsplit(':', 1)[0]
		if host not in LOCAL_HOSTS:
			raise web.HTTPForbidden(text='the bridge only answers on loopback')
		if request.headers.get('Origin'):
			raise web.HTTPForbidden(text='web pages and other extensions may not use the bridge')

	async def _version(self, request: web.Request) -> web.Response:
		self._local_only(request)
		ua = self.hello.get('userAgent', '')
		return web.json_response(
			{
				'Browser': _product(ua),
				'Protocol-Version': '1.3',
				'User-Agent': ua,
				'webSocketDebuggerUrl': f'ws://127.0.0.1:{self.port}/cdp/{self._token}',
			}
		)

	async def _status(self, request: web.Request) -> web.Response:
		self._local_only(request)
		return web.json_response(await self.status())

	async def _list(self, request: web.Request) -> web.Response:
		self._local_only(request)
		return web.json_response([{**info, 'id': info['targetId']} for info in self.tabs.values()])

	# -- extension side ----------------------------------------------------------------------------

	async def _extension_socket(self, request: web.Request) -> web.WebSocketResponse:
		origin = request.headers.get('Origin', '')
		if origin.removeprefix('chrome-extension://') not in self.extension_ids:
			raise web.HTTPForbidden(text='only the Retinat bridge extension may connect here')
		ws = web.WebSocketResponse(max_msg_size=MAX_MESSAGE, heartbeat=20.0)
		await ws.prepare(request)
		if self._ext is not None:
			await self._ext.close()  # the newest connection wins, e.g. after the service worker restarted
		self._ext = ws
		keepalive = asyncio.create_task(self._keep_alive(ws))
		try:
			async for msg in ws:
				if msg.type == WSMsgType.TEXT:
					self._on_extension(json.loads(msg.data))
		finally:
			keepalive.cancel()
			if self._ext is ws:
				self._ext = None
				self._ext_ready.clear()
				for tab_id in list(self.tabs):
					self._on_unshared(tab_id, 'the extension disconnected')
				for fut in self._pending.values():
					if not fut.done():
						fut.set_exception(BridgeError('the browser extension disconnected'))
				self._pending.clear()
		return ws

	async def _keep_alive(self, ws: web.WebSocketResponse) -> None:
		while not ws.closed:
			await asyncio.sleep(KEEPALIVE_S)
			try:
				await self._ext_call('ping')
			except (BridgeError, TimeoutError):
				pass  # a dead connection is noticed by the read loop

	def _on_extension(self, msg: dict[str, Any]) -> None:
		if 'id' in msg:
			fut = self._pending.pop(msg['id'], None)
			if fut is not None and not fut.done():
				if 'error' in msg:
					fut.set_exception(BridgeError(msg['error']))
				else:
					fut.set_result(msg.get('result') or {})
			return
		event = msg.get('event')
		if event == 'cdp':
			self._on_cdp_event(msg['tabId'], msg.get('sessionId'), msg['method'], msg.get('params') or {})
		elif event == 'shared':
			self._on_shared(msg['tab'])
		elif event == 'unshared':
			self._on_unshared(msg['tabId'], msg.get('why', ''))
		elif event == 'changed':
			self._on_changed(msg['tab'])
		elif event == 'control':
			self.set_holder(msg['holder'])
			self.stopped = bool(msg.get('stopped'))
			self._log_control(msg)
		elif event == 'offered':
			self._log_offered(msg)
		elif event == 'site':
			self._log_site(msg)
		elif event == 'hello':
			self.hello = msg
			self.holder = msg.get('holder', 'agent')
			self.stopped = bool(msg.get('stopped'))
			self._ext_ready.set()
			self._log_extension(msg)
			self._reload_if_outdated(msg)

	def _reload_if_outdated(self, hello: dict[str, Any]) -> None:
		"""Ask an extension running older code than this relay ships to reload itself, once per (version, policy),
		so files on disk that really are old cannot make it reload forever."""
		version, policy = hello.get('extension'), hello.get('policy')
		if version == EXPECTED_VERSION and policy == list(PASSIVE_GLOBS):
			return
		key = (str(version), json.dumps(policy))
		if key in self._reload_asked:
			return
		self._reload_asked.add(key)
		task = asyncio.create_task(self._ask_reload(hello))
		self._tasks.add(task)
		task.add_done_callback(self._tasks.discard)

	async def _ask_reload(self, hello: dict[str, Any]) -> None:
		try:
			await asyncio.wait_for(self._ext_call('reload'), 5)
			self._log_reload(hello, None)
		except (BridgeError, TimeoutError) as e:
			self._log_reload(hello, e)  # an extension from before 'reload' existed: the person reloads it once

	async def _ext_call(self, op: str, **kwargs: Any) -> dict[str, Any]:
		ws = self._ext
		if ws is None:
			raise BridgeError('the browser extension is not connected; open your browser with the Retinat bridge extension')
		msg_id = next(self._ids)
		fut: asyncio.Future = asyncio.get_running_loop().create_future()
		self._pending[msg_id] = fut
		try:
			await ws.send_str(json.dumps({'id': msg_id, 'op': op, **kwargs}))  # a failed send must not leave it pending
			return await asyncio.wait_for(fut, self.command_timeout)
		finally:
			self._pending.pop(msg_id, None)

	def _on_shared(self, info: dict[str, Any]) -> None:
		tab_id = info['tabId']
		known = tab_id in self.tabs
		self.tabs[tab_id] = _target_info(info)
		self._some_tab.set()
		if known:
			return
		for client in self._clients:
			if client.discover:
				self._emit(client, 'Target.targetCreated', {'targetInfo': self.tabs[tab_id]})
			if client.auto_attach:
				self._attach(client, tab_id)

	def _on_unshared(self, tab_id: int, why: str) -> None:
		info = self.tabs.pop(tab_id, None)
		if info is None:
			return
		if not self.tabs:
			self._some_tab.clear()
		self._children = {sid: tid for sid, tid in self._children.items() if tid != tab_id}
		for client in self._clients:
			sid = client.by_tab.pop(tab_id, None)
			if sid is not None:
				client.sessions.pop(sid, None)
				self._emit(client, 'Target.detachedFromTarget', {'sessionId': sid, 'targetId': info['targetId']})
			if client.discover:
				self._emit(client, 'Target.targetDestroyed', {'targetId': info['targetId']})
		logger.debug(f'🔗 Tab {tab_id} unshared: {why}')

	def _on_changed(self, info: dict[str, Any]) -> None:
		if info['tabId'] not in self.tabs:
			return
		self.tabs[info['tabId']] = _target_info(info)
		for client in self._clients:
			if client.discover:
				self._emit(client, 'Target.targetInfoChanged', {'targetInfo': self.tabs[info['tabId']]})

	def _on_cdp_event(self, tab_id: int, child: str | None, method: str, params: dict[str, Any]) -> None:
		if method == 'Target.attachedToTarget':
			self._children[params['sessionId']] = tab_id
		elif method == 'Target.detachedFromTarget':
			self._children.pop(params.get('sessionId', ''), None)
		if method.startswith('Network.'):
			params = self._without_cookies(params)
		for client in self._clients:
			sid = client.by_tab.get(tab_id)
			if sid is not None:
				self._emit(client, method, params, child or sid)

	# -- client side -------------------------------------------------------------------------------

	async def _client_socket(self, request: web.Request) -> web.WebSocketResponse:
		self._local_only(request)
		ws = web.WebSocketResponse(max_msg_size=MAX_MESSAGE)
		await ws.prepare(request)
		client = _Client(ws)
		self._clients.append(client)
		writer = asyncio.create_task(self._write(client))
		try:
			async for msg in ws:
				if msg.type == WSMsgType.TEXT:
					task = asyncio.create_task(self._serve(client, json.loads(msg.data)))
					self._tasks.add(task)
					task.add_done_callback(self._tasks.discard)
		finally:
			self._clients.remove(client)
			writer.cancel()
		return ws

	@staticmethod
	async def _write(client: _Client) -> None:
		while True:
			text = await client.outbox.get()
			if client.ws.closed:
				return
			await client.ws.send_str(text)

	async def _serve(self, client: _Client, msg: dict[str, Any]) -> None:
		sid = msg.get('sessionId')
		reply: dict[str, Any] = {'id': msg['id']}
		if sid:
			reply['sessionId'] = sid
		try:
			if sid:
				reply['result'] = await self._session_call(client, sid, msg['method'], msg.get('params') or {})
			else:
				reply['result'] = await self._browser_call(client, msg['method'], msg.get('params') or {})
		except BridgeError as e:
			reply['error'] = {'code': -32000, 'message': str(e)}
		except TimeoutError:
			reply['error'] = {'code': -32000, 'message': f'{msg["method"]} timed out in the browser extension'}
		client.outbox.put_nowait(json.dumps(reply))

	async def _session_call(self, client: _Client, sid: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
		tab_id = client.sessions.get(sid)
		child = None
		if tab_id is None:
			tab_id = self._children.get(sid)
			child = sid
		if tab_id is None or tab_id not in self.tabs:
			raise BridgeError(f'No session with given id {sid}')
		if method == 'Target.getTargetInfo' and child is None:
			return {'targetInfo': self.tabs[tab_id]}
		self._check(method)
		self._check_site(method, params)
		if method == 'Input.dispatchDragEvent' and (params.get('data') or {}).get('files'):
			raise BridgeError(f'{method} with files is refused through the extension bridge: {FILES}')
		result = await self._ext_call('send', tabId=tab_id, sessionId=child, method=method, params=params)
		if method in COOKIE_READS:
			result = {**result, 'cookies': self._only_shared_sites(result.get('cookies', []))}
		return result

	async def _browser_call(self, client: _Client, method: str, params: dict[str, Any]) -> dict[str, Any]:
		if method == 'Browser.getVersion':
			ua = self.hello.get('userAgent', '')
			return {'protocolVersion': '1.3', 'product': _product(ua), 'revision': '', 'userAgent': ua, 'jsVersion': ''}
		if method == 'Target.setDiscoverTargets':
			client.discover = bool(params.get('discover'))
			if client.discover:
				for info in self.tabs.values():
					self._emit(client, 'Target.targetCreated', {'targetInfo': info})
			return {}
		if method == 'Target.getTargets':
			return {'targetInfos': list(self.tabs.values())}
		if method == 'Target.getTargetInfo':
			target_id = params.get('targetId')
			if not target_id:
				return {'targetInfo': BROWSER_TARGET}
			return {'targetInfo': self.tabs[self._tab_for(target_id)]}
		if method == 'Target.setAutoAttach':
			client.auto_attach = bool(params.get('autoAttach'))
			if client.auto_attach:
				for tab_id in list(self.tabs):
					if tab_id not in client.by_tab:
						self._attach(client, tab_id)
			return {}
		if method == 'Target.attachToTarget':
			return {'sessionId': self._attach(client, self._tab_for(params['targetId']))}
		if method == 'Target.detachFromTarget':
			tab_id = client.sessions.pop(params.get('sessionId', ''), None)
			if tab_id is not None:
				client.by_tab.pop(tab_id, None)
				self._emit(client, 'Target.detachedFromTarget', {'sessionId': params['sessionId']})
			return {}
		self._check(method)
		if method == 'Target.createTarget':
			info = await self._ext_call('open', url=params.get('url') or 'about:blank')
			self._on_shared(info)  # idempotent with the extension's own 'shared' event
			return {'targetId': info['targetId']}
		if method == 'Target.closeTarget':
			await self._ext_call('close', tabId=self._tab_for(params['targetId']))
			return {'success': True}
		if method == 'Target.activateTarget':
			await self._ext_call('activate', tabId=self._tab_for(params['targetId']))
			return {}
		if method == 'Storage.getCookies':
			return await self._shared_cookies()
		raise BridgeError(f"'{method}' wasn't found (not available through the extension bridge)")

	def _check(self, method: str) -> None:
		why = refusal(method, self.human_driving, self.stopped)
		if why:
			raise BridgeError(why)

	def _tab_for(self, target_id: str) -> int:
		for tab_id, info in self.tabs.items():
			if info['targetId'] == target_id:
				return tab_id
		raise BridgeError(f'No target with given id {target_id} (only tabs the person shared are visible)')

	def _attach(self, client: _Client, tab_id: int) -> str:
		"""Give `client` a session on a shared tab and announce it; the extension attaches lazily on first use."""
		sid = client.by_tab.get(tab_id)
		if sid is None:
			sid = secrets.token_hex(16).upper()
			client.by_tab[tab_id] = sid
			client.sessions[sid] = tab_id
		event = {'sessionId': sid, 'targetInfo': {**self.tabs[tab_id], 'attached': True}, 'waitingForDebugger': False}
		self._emit(client, 'Target.attachedToTarget', event)
		return sid

	async def _shared_cookies(self) -> dict[str, Any]:
		"""Cookies for the sites of the shared tabs only - never the rest of the person's browser."""
		urls = [info['url'] for info in self.tabs.values() if info['url'].startswith(('http://', 'https://'))]
		if not urls:
			return {'cookies': []}
		tab_id = next(iter(self.tabs))
		result = await self._ext_call('send', tabId=tab_id, method='Network.getCookies', params={'urls': urls})
		return {**result, 'cookies': self._only_shared_sites(result.get('cookies', []))}

	def _shared_origins(self) -> set[tuple[str, str, int | None]]:
		origins = set()
		for info in self.tabs.values():
			url = urlparse(info['url'])
			if url.scheme in ('http', 'https') and url.hostname:
				origins.add((url.scheme, url.hostname, url.port or (443 if url.scheme == 'https' else 80)))
		return origins

	def _check_site(self, method: str, params: dict[str, Any]) -> None:
		"""Site data (local storage, IndexedDB, caches, ...) of shared tabs' own sites only. These calls take any
		origin, so through one shared tab they would read every site the person uses."""
		if method.split('.', 1)[0] not in SITE_DATA_DOMAINS:
			return
		named = [params.get(k) for k in SITE_KEYS]
		for holder in ('storageId', 'storageBucket'):
			inner = params.get(holder)
			if isinstance(inner, dict):
				named += [inner.get(k) for k in SITE_KEYS]
		if isinstance(params.get('cacheId'), str):
			named.append(params['cacheId'].split('|', 1)[0])
		shared = self._shared_origins()
		for value in named:
			if not isinstance(value, str) or not value:
				continue
			url = urlparse(value.split('^', 1)[0])  # a storage key may carry a partition after ^
			site = (url.scheme, url.hostname or '', url.port or (443 if url.scheme == 'https' else 80))
			if site not in shared:
				raise BridgeError(
					f'{method} is refused through the extension bridge: it reads the data of {value}, a site the person'
					" has not shared; only the shared tabs' own sites can be read"
				)

	def _without_cookies(self, params: dict[str, Any]) -> dict[str, Any]:
		"""Network events carry raw Cookie and Set-Cookie headers for every request a shared page makes, to any host.
		Raw cookie headers never leave the relay (a shared site's cookies stay readable through the cookie reads),
		and parsed cookie lists are cut down to shared sites, as those reads are."""

		def headers(h: Any) -> Any:
			return {k: v for k, v in h.items() if k.lower() not in COOKIE_HEADERS} if isinstance(h, dict) else h

		out = dict(params)
		out.pop('headersText', None)
		if 'headers' in out:
			out['headers'] = headers(out['headers'])
		for part in ('request', 'response', 'redirectResponse'):
			if isinstance(out.get(part), dict):
				inner = {k: v for k, v in out[part].items() if k not in ('headersText', 'requestHeadersText')}
				for key in ('headers', 'requestHeaders'):
					if key in inner:
						inner[key] = headers(inner[key])
				out[part] = inner
		for key in COOKIE_LISTS:
			if isinstance(out.get(key), list):
				out[key] = [
					e
					for e in out[key]
					if isinstance(e, dict) and isinstance(e.get('cookie'), dict) and self._only_shared_sites([e['cookie']])
				]
		return out

	def _only_shared_sites(self, cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
		"""The cookies a shared tab's own site would send. Any tab can read every cookie in the browser through
		CDP (HttpOnly ones too); the person shared tabs, not their whole cookie jar."""
		hosts = {urlparse(info['url']).hostname for info in self.tabs.values() if info['url'].startswith(('http://', 'https://'))}
		hosts.discard(None)

		def sent_to_a_shared_site(cookie: dict[str, Any]) -> bool:
			domain = str(cookie.get('domain', '')).lstrip('.')
			return bool(domain) and any(h == domain or h.endswith('.' + domain) for h in hosts if h)

		return [c for c in cookies if sent_to_a_shared_site(c)]

	def _emit(self, client: _Client, method: str, params: dict[str, Any], session_id: str | None = None) -> None:
		if client.ws.closed:
			return
		msg: dict[str, Any] = {'method': method, 'params': params}
		if session_id:
			msg['sessionId'] = session_id
		client.outbox.put_nowait(json.dumps(msg))

	# -- logging -----------------------------------------------------------------------------------

	def _log_listening(self) -> None:
		logger.info(f'🔗 Bridge relay listening on {self.cdp_url} (extension connects to ws://127.0.0.1:{self.port}/extension)')

	def _log_control(self, msg: dict[str, Any]) -> None:
		who = 'the person' if msg['holder'] == 'human' else 'the AI'
		logger.info(f'🔗 Wheel to {who}: {msg.get("why", "")}')

	def _log_offered(self, msg: dict[str, Any]) -> None:
		logger.info(f'🔗 A tab opened from a shared tab waits for the person to share it ({msg.get("why", "")})')

	def _log_site(self, msg: dict[str, Any]) -> None:
		logger.info(f'🔗 Asked about {msg.get("origin")}, the person answered {msg.get("answer")}')

	def _log_reload(self, hello: dict[str, Any], error: BaseException | None) -> None:
		if error is None:
			logger.info(f'🔗 The extension ran older code ({hello.get("extension")}); it is reloading itself')
		else:
			logger.warning(
				f'🔗 The extension runs older code ({hello.get("extension")}) and cannot reload itself ({error}); '
				'press the reload arrow on its card at chrome://extensions once'
			)

	def _log_extension(self, hello: dict[str, Any]) -> None:
		logger.info(f'🔗 Bridge extension connected from {_product(hello.get("userAgent", ""))} (MV{hello.get("manifest")})')


def _target_info(info: dict[str, Any]) -> dict[str, Any]:
	"""The CDP TargetInfo the extension reports, with its tabId kept for routing."""
	return {
		'targetId': info['targetId'],
		'type': info.get('type', 'page'),
		'title': info.get('title', ''),
		'url': info.get('url', ''),
		'attached': True,
		'canAccessOpener': False,
		'browserContextId': info.get('browserContextId', 'default'),
		'tabId': info['tabId'],
	}


def _product(user_agent: str) -> str:
	"""Browser/version from a Chromium user agent, preferring the branded name (Edg, OPR, ...) over Chrome."""
	for brand, name in (('Edg/', 'Edge'), ('OPR/', 'Opera'), ('Vivaldi/', 'Vivaldi'), ('Chrome/', 'Chrome')):
		if brand in user_agent:
			return f'{name}/{user_agent.split(brand, 1)[1].split(" ", 1)[0]}'
	return 'Chrome/unknown'
