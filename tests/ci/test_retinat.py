"""The Retinat MCP server, called the way an MCP client calls it. Real browser, local pages."""

import mcp.types as types
import pytest
from pytest_httpserver import HTTPServer

from browser_use.retinat import RetinatServer

PAGE = '<!doctype html><title>Hello</title><body style="background:#0b7a3b"><h1>Hello from a page</h1><input id="q"></body>'
WALL = '<!doctype html><title>Just a moment...</title><body>Checking your browser before accessing the site.</body>'


@pytest.fixture(scope='module')
def site():
	server = HTTPServer()
	server.start()
	server.expect_request('/').respond_with_data(PAGE, content_type='text/html')
	server.expect_request('/wall').respond_with_data(WALL, content_type='text/html')
	yield server
	server.stop()


@pytest.fixture
async def retinat(tmp_path, monkeypatch):
	monkeypatch.setenv('BROWSER_USE_EYES_NOW', str(tmp_path / 'now.json'))
	server = RetinatServer()
	server.config.setdefault('browser_profile', {}).update({'headless': True, 'user_data_dir': str(tmp_path / 'profile')})
	yield server
	await server._close_all_sessions()


async def _call(server, name: str, arguments: dict):
	handler = server.server.get_request_handler('tools/call')
	assert handler is not None
	result = await handler.handler(None, types.CallToolRequestParams(name=name, arguments=arguments))  # type: ignore[arg-type]
	assert isinstance(result, types.CallToolResult)
	return result


def _text(result) -> str:
	return '\n'.join(b.text for b in result.content if isinstance(b, types.TextContent))


async def test_it_is_its_own_server_with_only_vision_first_tools():
	server = RetinatServer()
	assert server.server.name == 'retinat'
	handler = server.server.get_request_handler('tools/list')
	assert handler is not None
	listed = await handler.handler(None, types.PaginatedRequestParams())  # type: ignore[arg-type]
	assert isinstance(listed, types.ListToolsResult)
	names = {t.name for t in listed.tools}
	assert names >= {
		'retinat_open',
		'retinat_look',
		'retinat_watch',
		'retinat_scan',
		'retinat_browse',
		'retinat_next',
		'retinat_explore',
		'retinat_recall',
	}
	assert all(n.startswith('retinat_') for n in names), 'no DOM tools here: that is the browser-use server'


async def test_open_look_type_and_key_through_mcp(retinat, site):
	opened = await _call(retinat, 'retinat_open', {'url': site.url_for('/')})
	assert _text(opened).startswith('Opened "Hello"'), _text(opened)
	look = await _call(retinat, 'retinat_look', {})
	assert any(isinstance(b, types.ImageContent) and b.mime_type == 'image/jpeg' for b in look.content), _text(look)
	await _call(retinat, 'retinat_click', {'x': 60, 'y': 75})
	cdp = await retinat.browser_session.get_or_create_cdp_session(focus=False)
	await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': 'document.getElementById("q").focus()'}, session_id=cdp.session_id
	)
	await _call(retinat, 'retinat_type', {'text': 'hi there'})
	value = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': 'document.getElementById("q").value', 'returnByValue': True}, session_id=cdp.session_id
	)
	assert value['result']['value'] == 'hi there'


async def test_a_bot_wall_is_reported_as_blocked(retinat, site):
	opened = await _call(retinat, 'retinat_open', {'url': site.url_for('/wall')})
	assert _text(opened).startswith('BLOCKED: cloudflare-challenge'), _text(opened)


async def test_unknown_tools_are_errors_not_crashes(retinat):
	result = await _call(retinat, 'browser_click', {'index': 1})
	assert result.is_error and 'Unknown tool' in _text(result)


async def test_recall_through_mcp_answers_plainly_when_nothing_is_held(retinat, site):
	await _call(retinat, 'retinat_open', {'url': site.url_for('/')})
	result = await _call(retinat, 'retinat_recall', {'t0': 0, 't1': 5})
	assert not result.is_error and 'nothing held' in _text(result), _text(result)
	bad = await _call(retinat, 'retinat_recall', {'t0': 5, 't1': 1})
	assert bad.is_error and 't1 must be' in _text(bad)
