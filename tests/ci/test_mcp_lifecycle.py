"""An MCP server that is stopped takes its browser with it.

Found hands-on: restarting the Retinat server (as a client does when it reconnects) left the old server's Chromium
running with parent PID 1, for good. Real server process over real stdio, a real page; the server is stopped the way
a client stops it.
"""

import asyncio
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest
from pytest_httpserver import HTTPServer


@pytest.fixture(scope='module')
def page():
	server = HTTPServer()
	server.start()
	server.expect_request('/').respond_with_data('<!doctype html><title>Here</title><p>here</p>', content_type='text/html')
	yield server
	server.clear()
	if server.is_running():
		server.stop()


def _browsers_of(pid: int) -> list[psutil.Process]:
	try:
		return [p for p in psutil.Process(pid).children(recursive=True) if 'chrom' in p.name().lower()]
	except psutil.NoSuchProcess:
		return []


@pytest.mark.parametrize('stop', ['SIGTERM', 'client leaves'])
async def test_a_stopped_eye_server_closes_its_browser(page, tmp_path, stop):
	from mcp import ClientSession, StdioServerParameters
	from mcp.client.stdio import stdio_client

	env = {**os.environ, 'BROWSER_USE_EYES_NOW': str(tmp_path / 'now.json'), 'BROWSER_USE_CONFIG_DIR': str(tmp_path / 'config')}
	params = StdioServerParameters(command=sys.executable, args=['-m', 'browser_use.retinat', '--network', 'off'], env=env)
	browsers: list[psutil.Process] = []
	server_pid = None
	async with stdio_client(params) as (read, write):
		async with ClientSession(read, write) as client:
			await client.initialize()
			opened = await client.call_tool('eye_open', {'url': page.url_for('/')})
			assert not opened.is_error, opened
			server_pid = next(
				p.pid for p in psutil.Process().children(recursive=True) if 'browser_use.retinat' in ' '.join(p.cmdline())
			)
			browsers = _browsers_of(server_pid)
			assert browsers, 'the server started a browser'
			if stop == 'SIGTERM':
				# the client still holds the server's stdin open, as Claude Code does when it restarts a server: the server
				# must exit anyway (found hands-on: it closed its browser, then hung on its stdin reader)
				os.kill(server_pid, signal.SIGTERM)
				server = psutil.Process(server_pid)
				for _ in range(100):
					if not server.is_running() or server.status() == psutil.STATUS_ZOMBIE:
						break
					await asyncio.sleep(0.2)
				assert not server.is_running() or server.status() == psutil.STATUS_ZOMBIE, (
					'the server outlived SIGTERM while its client was still connected'
				)
	for _ in range(100):  # 'client leaves': the context exit closed stdin
		if not any(b.is_running() and b.status() != psutil.STATUS_ZOMBIE for b in browsers):
			break
		await asyncio.sleep(0.2)
	alive = [b.pid for b in browsers if b.is_running() and b.status() != psutil.STATUS_ZOMBIE]
	assert not alive, f'browser processes outlived the server ({stop}): {alive}'


async def test_a_restarted_eye_tells_its_client_to_list_the_tools_again(tmp_path):
	"""Found hands-on: Retinat was restarted with a new tool (eye_step); Claude Code reconnected in 1.7 s but kept the
	tool list of the first connection, so the new tool could not be called for the rest of the session. A client
	re-lists the tools when told the list changed: each server process says so once, on its first tool call."""
	import mcp.types as types
	from mcp import ClientSession, StdioServerParameters
	from mcp.client.stdio import stdio_client

	told: list[str] = []

	async def on_message(message) -> None:
		note = getattr(message, 'root', message)  # bare, or wrapped as older SDKs hand it over
		if isinstance(note, types.ToolListChangedNotification):
			told.append(note.method)

	env = {**os.environ, 'BROWSER_USE_EYES_NOW': str(tmp_path / 'now.json'), 'BROWSER_USE_CONFIG_DIR': str(tmp_path / 'config')}
	params = StdioServerParameters(command=sys.executable, args=['-m', 'browser_use.retinat', '--network', 'off'], env=env)
	async with stdio_client(params) as (read, write):
		async with ClientSession(read, write, message_handler=on_message) as client:
			init = await client.initialize()
			assert init.capabilities.tools is not None and init.capabilities.tools.list_changed, init.capabilities
			for _ in range(2):
				status = await client.call_tool('eye_network_status', {})
				assert not status.is_error, status
			await asyncio.sleep(0.5)
			assert told == ['notifications/tools/list_changed'], told
			listed = await client.list_tools()
			assert 'eye_step' in {t.name for t in listed.tools}


async def test_a_restarted_eye_tells_a_listening_client_to_list_the_tools_again(tmp_path):
	"""The same on the 2026-07-28 protocol, which Claude Code speaks: change notices go out on the stream a client opens
	with subscriptions/listen, so Retinat serves one and tells each new listener once that the tool list changed."""
	from mcp.client.client import Client
	from mcp.client.stdio import StdioServerParameters
	from mcp.shared.subscriptions import ToolsListChanged

	env = {**os.environ, 'BROWSER_USE_EYES_NOW': str(tmp_path / 'now.json'), 'BROWSER_USE_CONFIG_DIR': str(tmp_path / 'config')}
	params = StdioServerParameters(command=sys.executable, args=['-m', 'browser_use.retinat', '--network', 'off'], env=env)
	async with Client(params) as client:
		assert client.session.protocol_version == '2026-07-28', client.session.protocol_version
		# a stream opened first for something else does not use up the notice
		async with client.listen(resources_list_changed=True):
			await asyncio.sleep(1.0)
		async with client.listen(tools_list_changed=True) as sub:
			event = await asyncio.wait_for(anext(sub), 10)
			assert isinstance(event, ToolsListChanged), event
		listed = await client.list_tools()
		assert 'eye_step' in {t.name for t in listed.tools}


async def test_browser_use_answers_the_handshake_and_tool_list_without_loading_anything_heavy():
	"""Found when the session's machine was replaced: Retinat connected and browser-use did not (Claude Code gives a
	server 30 s). browser-use imported all ~2,100 of its modules before it answered, each read off a cold disk after
	the boot; Retinat answers from ~900 and loads the rest meanwhile. `python -m browser_use.mcp` now does the same, and
	the list it answers with is the one the server proper serves."""
	import mcp.types as types

	from browser_use.mcp.server import BrowserUseServer

	probe = (
		'import sys\n'
		'import browser_use.mcp.__main__\n'
		'from browser_use.mcp.tools import browser_tools\n'
		"heavy = ('browser_use.mcp.server', 'browser_use.mcp.client', 'browser_use.agent.views', 'lmnr', 'openai',\n"
		"         'browser_use.browser.session', 'browser_use.tools.service')\n"
		"print(','.join(t.name for t in browser_tools())); print(','.join(m for m in heavy if m in sys.modules))\n"
	)
	proc = await asyncio.create_subprocess_exec(
		sys.executable, '-c', probe, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
	)
	stdout, stderr = await asyncio.wait_for(proc.communicate(), 120)
	assert proc.returncode == 0, stderr.decode()
	names_line, heavy_line = stdout.decode().split('\n')[:2]
	assert heavy_line == '', f'the handshake path imported {heavy_line}'

	server = BrowserUseServer()
	listed = await server.server.get_request_handler('tools/list').handler(None, types.PaginatedRequestParams())  # type: ignore[arg-type]
	assert names_line.split(',') == [t.name for t in listed.tools]
	from browser_use.mcp.tools import browser_tools

	assert [t.model_dump() for t in browser_tools()] == [t.model_dump() for t in listed.tools]


async def test_browser_use_starts_without_the_eyes_extra():
	"""Found in review: the front read its call budget from the eyes package, whose `__init__` imports numpy, so a
	base install (numpy comes only with the eyes extras) could not start the server at all, where it had served its
	browser tools before. Without numpy the front starts, and lists every tool but the eyes'."""
	probe = (
		'import sys\n'
		"sys.modules['numpy'] = None  # as on a base install: import numpy fails, find_spec('numpy') is None\n"
		'from browser_use.mcp.front import BrowserUseFront\n'
		'front = BrowserUseFront()\n'
		"print(','.join(t.name for t in front.tool_list()))\n"
	)
	proc = await asyncio.create_subprocess_exec(
		sys.executable, '-c', probe, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
	)
	stdout, stderr = await asyncio.wait_for(proc.communicate(), 120)
	assert proc.returncode == 0, stderr.decode()
	names = stdout.decode().strip().split(',')
	assert 'browser_navigate' in names and not any(n.startswith('eyes_') for n in names), names


async def test_browser_use_over_stdio_takes_a_call_that_arrives_while_it_loads(tmp_path):
	"""The front answers the handshake and the list itself; the first call waits for the server proper and is answered
	by it, and the client is told once to list the tools again, as Retinat tells it."""
	import mcp.types as types
	from mcp import ClientSession, StdioServerParameters
	from mcp.client.stdio import stdio_client

	told: list[str] = []

	async def on_message(message) -> None:
		note = getattr(message, 'root', message)
		if isinstance(note, types.ToolListChangedNotification):
			told.append(note.method)

	env = {**os.environ, 'BROWSER_USE_CONFIG_DIR': str(tmp_path / 'config'), 'ANONYMIZED_TELEMETRY': 'false'}
	params = StdioServerParameters(command=sys.executable, args=['-m', 'browser_use.mcp'], env=env)
	async with stdio_client(params) as (read, write):
		async with ClientSession(read, write, message_handler=on_message) as client:
			init = await client.initialize()
			assert init.server_info.name == 'browser-use', init.server_info
			assert init.capabilities.tools is not None and init.capabilities.tools.list_changed, init.capabilities
			listed = await client.list_tools()
			assert {'browser_navigate', 'browser_list_sessions'} <= {t.name for t in listed.tools}
			for _ in range(2):
				sessions = await client.call_tool('browser_list_sessions', {})
				assert not sessions.is_error, sessions
			assert 'No active browser sessions' in sessions.content[0].text, sessions  # type: ignore[union-attr]
			await asyncio.sleep(0.5)
			assert told == ['notifications/tools/list_changed'], told


async def test_a_server_that_failed_to_load_says_so_and_loads_again_on_the_next_call(tmp_path):
	"""Found in review: the front kept a failed load for good. A restarted server whose bridge port the old process
	still held answered the handshake and the list, then failed every call with a raw protocol error for the rest of
	its life, though the port was soon free. A call now says what failed and that nothing was done, and the next call
	loads again."""
	import socket

	from mcp import ClientSession, StdioServerParameters
	from mcp.client.stdio import stdio_client

	held = socket.socket()
	held.bind(('127.0.0.1', 0))
	held.listen()
	port = held.getsockname()[1]
	env = {
		**os.environ,
		'BROWSER_USE_CONFIG_DIR': str(tmp_path / 'config'),
		'ANONYMIZED_TELEMETRY': 'false',
		'BROWSER_USE_BRIDGE': str(port),
	}
	params = StdioServerParameters(command=sys.executable, args=['-m', 'browser_use.mcp'], env=env)
	try:
		async with stdio_client(params) as (read, write):
			async with ClientSession(read, write) as client:
				await client.initialize()
				failed = await client.call_tool('browser_list_sessions', {})
				text = failed.content[0].text  # type: ignore[union-attr]
				assert failed.is_error and 'failed to load' in text and 'call again' in text, text
				held.close()
				sessions = await client.call_tool('browser_list_sessions', {})
				assert not sessions.is_error, sessions
	finally:
		held.close()


# --- bin/mcp-front.py: answers the handshake from a cache while the real server starts ---

FRONT = Path(__file__).resolve().parents[2] / 'bin' / 'mcp-front.py'
INIT = {
	'jsonrpc': '2.0',
	'id': 1,
	'method': 'initialize',
	'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '0'}},
}


class _FrontProcess:
	"""The front over real stdio, in front of a real Retinat; lines the front writes land in a queue as they arrive."""

	def __init__(self, tmp_path: Path, child_prefix: str = '') -> None:
		env = {
			**os.environ,
			'MCP_FRONT_CACHE_DIR': str(tmp_path / 'cache'),
			'MCP_FRONT_CHILD': f'{child_prefix}exec {sys.executable} -m browser_use.retinat --network off',
			'BROWSER_USE_EYES_NOW': str(tmp_path / 'now.json'),
			'BROWSER_USE_CONFIG_DIR': str(tmp_path / 'config'),
		}
		self.started = time.monotonic()
		self.proc = subprocess.Popen(
			[sys.executable, str(FRONT), 'browser_use.retinat'],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			env=env,
		)
		self.lines: queue.Queue[tuple[float, dict]] = queue.Queue()
		threading.Thread(target=self._read, daemon=True).start()

	def _read(self) -> None:
		assert self.proc.stdout is not None
		for raw in self.proc.stdout:
			self.lines.put((time.monotonic() - self.started, json.loads(raw)))

	def send(self, message: dict) -> None:
		assert self.proc.stdin is not None
		self.proc.stdin.write(json.dumps(message).encode() + b'\n')
		self.proc.stdin.flush()

	def response(self, msg_id, timeout: float) -> tuple[float, dict]:
		deadline = time.monotonic() + timeout
		while True:
			at, msg = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
			if msg.get('id') == msg_id and 'method' not in msg:
				return at, msg

	def drain(self, seconds: float) -> list[dict]:
		time.sleep(seconds)
		out = []
		while not self.lines.empty():
			out.append(self.lines.get()[1])
		return out

	def handshake(self, timeout: float, version: str = '2025-06-18') -> tuple[float, dict, float, dict]:
		self.send({**INIT, 'params': {**INIT['params'], 'protocolVersion': version}})
		init_at, init = self.response(1, timeout)
		self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
		self.send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
		tools_at, tools = self.response(2, timeout)
		return init_at, init, tools_at, tools

	def close(self) -> int:
		assert self.proc.stdin is not None
		self.proc.stdin.close()
		try:
			return self.proc.wait(timeout=20)
		except subprocess.TimeoutExpired:
			self.proc.kill()
			raise


def test_the_front_passes_the_first_handshake_through_and_caches_it(tmp_path):
	front = _FrontProcess(tmp_path)
	try:
		_, init, _, tools = front.handshake(timeout=120)
	finally:
		front.close()
	assert init['result']['serverInfo']['name'] == 'eye-mcp-unlimited', init
	assert any(t['name'] == 'eye_network_status' for t in tools['result']['tools']), tools
	(cache_file,) = (tmp_path / 'cache').glob('browser_use.retinat-*.json')
	cached = json.loads(cache_file.read_text())
	assert cached['initialize'] == {'2025-06-18': init['result']}
	assert cached['tools'] == tools['result']


def test_the_front_answers_from_its_cache_before_the_server_exists(tmp_path):
	first = _FrontProcess(tmp_path)
	try:
		_, init1, _, tools1 = first.handshake(timeout=120)
	finally:
		first.close()

	second = _FrontProcess(tmp_path, child_prefix='sleep 4; ')
	try:
		init_at, init2, tools_at, tools2 = second.handshake(timeout=10)
		assert init_at < 2 and tools_at < 2, (init_at, tools_at)
		assert init2['result']['serverInfo']['name'] == 'eye-mcp-unlimited'
		assert [t['name'] for t in tools2['result']['tools']] == [t['name'] for t in tools1['result']['tools']]
		# the real server answers the same requests later; the client must not see them twice
		later = second.drain(8)
		assert not [m for m in later if m.get('id') in (1, 2)], later
		second.send(
			{'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'eye_network_status', 'arguments': {}}}
		)
		_, called = second.response(3, timeout=60)
		assert 'result' in called, called
	finally:
		second.close()


def test_the_front_passes_an_uncached_protocol_version_through(tmp_path):
	first = _FrontProcess(tmp_path)
	try:
		first.handshake(timeout=120)
	finally:
		first.close()

	second = _FrontProcess(tmp_path, child_prefix='sleep 3; ')
	try:
		second.send({**INIT, 'params': {**INIT['params'], 'protocolVersion': '2024-11-05'}})
		at, init = second.response(1, timeout=60)
		assert at >= 3, f'answered at {at:.1f} s, before the child could have'
		assert init['result']['serverInfo']['name'] == 'eye-mcp-unlimited', init
	finally:
		second.close()


def test_an_answer_to_what_the_client_asked_before_a_cached_handshake_never_reaches_it(tmp_path):
	"""Found live on a freshly booted machine: Claude Code probed with `server/discover`, gave up on it while the real
	server was still starting, and connected with the cached `initialize`. The real server answered the probe 25 s
	later, the client met "a response for an unknown message", and dropped the connection."""
	first = _FrontProcess(tmp_path)
	try:
		first.handshake(timeout=120)
	finally:
		first.close()

	front = _FrontProcess(tmp_path, child_prefix='sleep 4; ')
	try:
		front.send({'jsonrpc': '2.0', 'id': 'probe', 'method': 'server/discover', 'params': {}})
		init_at, init, _, _ = front.handshake(timeout=2)
		assert init_at < 2 and init['result']['serverInfo']['name'] == 'eye-mcp-unlimited'
		later = front.drain(12)  # the real server starts after 4 s and answers everything it was sent, the probe too
		assert not [m for m in later if m.get('id') == 'probe'], later
		# and the real server is there behind the front: a request made now is answered by it
		front.send({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})
		_, tools = front.response(3, timeout=60)
		assert any(t['name'] == 'eye_network_status' for t in tools['result']['tools'])
	finally:
		front.close()


def test_the_lists_a_client_asks_for_after_the_handshake_are_answered_from_the_cache_and_a_cancelled_one_never_is(tmp_path):
	"""Found live on a freshly booted machine: after the cached handshake Claude Code asked for prompts/list and
	resources/list, the real server answered after 32 s, the client had given up at 30 s, and the late answers ("a
	response for an unknown message ID") dropped the connection."""
	first = _FrontProcess(tmp_path)
	try:
		first.handshake(timeout=120)
		first.send({'jsonrpc': '2.0', 'id': 10, 'method': 'prompts/list'})
		first.send({'jsonrpc': '2.0', 'id': 11, 'method': 'resources/list'})
		_, prompts = first.response(10, timeout=60)
		_, resources = first.response(11, timeout=60)
	finally:
		first.close()

	front = _FrontProcess(tmp_path, child_prefix='sleep 4; ')
	try:
		front.handshake(timeout=2)
		front.send({'jsonrpc': '2.0', 'id': 10, 'method': 'prompts/list'})
		front.send({'jsonrpc': '2.0', 'id': 11, 'method': 'resources/list'})
		prompts_at, cached_prompts = front.response(10, timeout=2)
		resources_at, cached_resources = front.response(11, timeout=2)
		assert prompts_at < 2 and resources_at < 2, 'answered before the real server exists'
		assert cached_prompts['result'] == prompts['result'] and cached_resources['result'] == resources['result']
		# a request the client gives up on while the real server is still starting: its answer must never arrive
		front.send({'jsonrpc': '2.0', 'id': 12, 'method': 'resources/templates/list'})
		front.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 12, 'reason': 'timeout'}})
		later = front.drain(12)  # the real server starts after 4 s and answers all it was sent
		assert not [m for m in later if m.get('id') in (10, 11, 12)], later
	finally:
		front.close()
