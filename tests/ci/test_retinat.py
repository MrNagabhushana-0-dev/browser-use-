"""The Retinat MCP server, called the way an MCP client calls it. Real browser, local pages."""

import mcp.types as types
import pytest
from pytest_httpserver import HTTPServer

from browser_use.retinat import RetinatServer

PAGE = '<!doctype html><title>Hello</title><body style="background:#0b7a3b"><h1>Hello from a page</h1><input id="q"></body>'
TOAST = (
	'<!doctype html><title>Toast</title><body><h1>Settings</h1><script>setTimeout(() => {'
	"const t = document.createElement('div'); t.textContent = 'Saved #4242'; document.body.appendChild(t);"
	'setTimeout(() => t.remove(), 1200) }, 600)</script></body>'
)
WALL = '<!doctype html><title>Just a moment...</title><body>Checking your browser before accessing the site.</body>'


@pytest.fixture(scope='module')
def site():
	server = HTTPServer()
	server.start()
	server.expect_request('/').respond_with_data(PAGE, content_type='text/html')
	server.expect_request('/wall').respond_with_data(WALL, content_type='text/html')
	server.expect_request('/toast').respond_with_data(TOAST, content_type='text/html')
	yield server
	server.stop()


@pytest.fixture(scope='module')
def beeps(site, tmp_path_factory):
	from browser_use.eyes import bench

	task = bench.beeps_task(1, tmp_path_factory.mktemp('beeps'))
	site.expect_request('/beeps').respond_with_data(task.page('/beeps.webm'), content_type='text/html')
	site.expect_request('/beeps.webm').respond_with_handler(lambda r: bench.media_response(r, task.media))
	return task


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
		'retinat_search',
		'retinat_changes',
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


async def test_search_through_mcp_answers_plainly_with_an_empty_archive(retinat, site):
	await _call(retinat, 'retinat_open', {'url': site.url_for('/')})
	result = await _call(retinat, 'retinat_search', {'query': 'a green page'})
	assert not result.is_error, _text(result)
	assert 'nothing archived' in _text(result), _text(result)


async def test_changes_reports_text_that_appeared_once_then_nothing(retinat, site):
	import asyncio

	await _call(retinat, 'retinat_open', {'url': site.url_for('/toast')})
	await asyncio.sleep(3.0)  # the toast comes and goes before we ask
	first = _text(await _call(retinat, 'retinat_changes', {}))
	assert 'Saved #4242' in first, first
	second = _text(await _call(retinat, 'retinat_changes', {}))
	assert 'Saved #4242' not in second, second


async def test_the_server_hears_a_muted_video_with_no_gesture_even_when_asked_late(retinat, site, beeps):
	# Through the server's own launch profile, as an MCP client gets it: no autoplay flag passed in by the
	# test, and no click on the page. The model's first call after opening comes seconds later; beeps
	# that played in between must still be counted.
	import asyncio

	await _call(retinat, 'retinat_open', {'url': site.url_for('/beeps')})
	await asyncio.sleep(3.0)
	text = _text(await _call(retinat, 'retinat_watch', {'seconds': 11}))
	assert 'none captured' not in text, text
	assert f'{beeps.truth["count"]} onsets' in text, (beeps.truth, text)


async def test_after_a_video_page_a_text_page_is_seen_as_a_page_with_its_toast(retinat, site, beeps):
	# The second page has no video: the first page's item must not linger (it did, so look watched a ghost
	# and returned no image), and a toast that came and went before the call must still be reported.
	import asyncio

	await _call(retinat, 'retinat_open', {'url': site.url_for('/beeps')})
	await asyncio.sleep(1.5)
	await _call(retinat, 'retinat_open', {'url': site.url_for('/toast')})
	await asyncio.sleep(2.5)  # the toast shows at 0.6 s for 1.2 s: gone before the first call
	watched = await _call(retinat, 'retinat_watch', {'seconds': 2, 'until': 'time'})
	text = _text(watched)
	assert 'Saved #4242' in text, text
	assert 'video 360x640' not in text, text
	assert any(isinstance(b, types.ImageContent) for b in watched.content), 'a page with no video is shown as drawn'
	looked = await _call(retinat, 'retinat_look', {})
	assert 'no video playing' in _text(looked), _text(looked)
	assert any(isinstance(b, types.ImageContent) for b in looked.content)
	assert 'watching a video' not in _text(await _call(retinat, 'retinat_now', {}))


async def test_beeps_asked_about_long_after_the_video_ended_are_still_counted(retinat, site, beeps):
	# The question can come long after playback: the journal must hold the count (it only logged sound-class
	# changes, and beeps over silence are none), and a watch must reach back past a 30 s cap the ring outlasts.
	import asyncio

	await _call(retinat, 'retinat_open', {'url': site.url_for('/beeps')})
	await asyncio.sleep(33.0)
	changes = _text(await _call(retinat, 'retinat_changes', {}))
	assert f'{beeps.truth["count"]} distinct sounds' in changes, changes
	watched = _text(await _call(retinat, 'retinat_watch', {'seconds': 2, 'until': 'time'}))
	assert f'distinct sounds: {beeps.truth["count"]}' in watched, watched
