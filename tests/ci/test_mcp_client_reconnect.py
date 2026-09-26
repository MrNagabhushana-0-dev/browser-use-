"""Regression test: MCPClient must actually stay connected after a reconnect.

`MCPClient._disconnect_event` was created once in `__init__` and never reset.
After `disconnect()` sets it, a later `connect()` starts a new
`_run_stdio_client` task whose `await self._disconnect_event.wait()` (client.py)
would return immediately because the *same*, already-set `asyncio.Event`
instance was reused -- tearing the freshly-established stdio/session context
managers down again right after `connect()` reported success. The caller sees
`connect()` return normally (and `_connected` briefly flip to `True`), but the
session is gone by the time it tries to use it, so any subsequent MCP tool
call fails with "not connected" even though nothing told the caller that.

This uses a real MCP server subprocess (tests/ci/_mcp_servers/echo_server.py)
talking real stdio JSON-RPC -- no mocking of the connection itself, only the
usual "no LLM needed" exemption (this test doesn't use an LLM at all).
"""

import sys
from pathlib import Path

import pytest

from browser_use.mcp.client import MCPClient

ECHO_SERVER = str(Path(__file__).parent / '_mcp_servers' / 'echo_server.py')


@pytest.fixture
async def client():
	c = MCPClient(server_name='echo-test', command=sys.executable, args=[ECHO_SERVER])
	yield c
	if c._connected:
		await c.disconnect()


async def test_reconnect_after_disconnect_stays_connected(client: MCPClient):
	"""connect() -> disconnect() -> connect() must yield a *live* session."""
	await client.connect()
	assert client._connected
	assert 'echo' in client._tools

	await client.disconnect()
	assert not client._connected

	await client.connect()
	assert client._connected, 'reconnect should report connected'

	# The bug: the reused, already-set _disconnect_event immediately unblocks
	# _run_stdio_client's `await self._disconnect_event.wait()`, tearing the
	# session back down asynchronously right after connect() returns. Give
	# that (buggy) teardown a chance to run before asserting the session is
	# actually still usable.
	import asyncio

	await asyncio.sleep(0.3)

	assert client._connected, 'client silently disconnected itself after reconnecting'
	assert client.session is not None, 'session must still be live after reconnecting'

	# And it must actually be usable, not just "not yet torn down".
	result = await client.session.call_tool('echo', {'text': 'hello'})
	assert result.is_error is False
	assert 'hello' in str(result.content)


async def test_reconnect_tool_call_works_end_to_end(client: MCPClient):
	"""A tool registered after a reconnect must be callable through the wrapper."""
	from browser_use import Tools

	await client.connect()
	await client.disconnect()
	await client.connect()

	tools = Tools()
	await client.register_to_tools(tools)

	result = await tools.registry.execute_action('echo', {'text': 'world'})
	assert result.success is not False
	assert result.error is None
	assert result.extracted_content is not None
	assert 'echo: world' in result.extracted_content
