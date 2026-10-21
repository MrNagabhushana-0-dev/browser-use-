"""An MCP server that is stopped takes its browser with it.

Found hands-on: restarting the Retinat server (as a client does when it reconnects) left the old server's Chromium
running with parent PID 1, for good. Real server process over real stdio, a real page; the server is stopped the way
a client stops it.
"""

import asyncio
import os
import signal
import sys

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
async def test_a_stopped_retinat_server_closes_its_browser(page, tmp_path, stop):
	from mcp import ClientSession, StdioServerParameters
	from mcp.client.stdio import stdio_client

	env = {**os.environ, 'BROWSER_USE_EYES_NOW': str(tmp_path / 'now.json'), 'BROWSER_USE_CONFIG_DIR': str(tmp_path / 'config')}
	params = StdioServerParameters(command=sys.executable, args=['-m', 'browser_use.retinat', '--network', 'off'], env=env)
	browsers: list[psutil.Process] = []
	server_pid = None
	async with stdio_client(params) as (read, write):
		async with ClientSession(read, write) as client:
			await client.initialize()
			opened = await client.call_tool('retinat_open', {'url': page.url_for('/')})
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


async def test_a_restarted_retinat_tells_its_client_to_list_the_tools_again(tmp_path):
	"""Found hands-on: Retinat was restarted with a new tool (retinat_step); Claude Code reconnected in 1.7 s but kept the
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
				status = await client.call_tool('retinat_network_status', {})
				assert not status.is_error, status
			await asyncio.sleep(0.5)
			assert told == ['notifications/tools/list_changed'], told
			listed = await client.list_tools()
			assert 'retinat_step' in {t.name for t in listed.tools}


async def test_a_restarted_retinat_tells_a_listening_client_to_list_the_tools_again(tmp_path):
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
		assert 'retinat_step' in {t.name for t in listed.tools}
