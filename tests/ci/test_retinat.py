"""The Retinat MCP server, called the way an MCP client calls it. Real browser, local pages."""

import json

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
FIND = (
	'<!doctype html><title>Shop</title><body style="margin:0;font:16px sans-serif">'
	'<button id="go" style="position:absolute;left:80px;top:180px;width:140px;height:44px">Checkout</button>'
	'<p style="position:absolute;left:600px;top:400px;margin:0;font:6px sans-serif">Coupon code ZX-4417</p>'
	'<div style="position:absolute;top:3200px">Terms of service</div></body>'
)
FIND_DEEP = (
	'<!doctype html><title>Deep</title><body style="margin:0;font:16px sans-serif">'
	'<button style="position:absolute;left:300px;top:100px;width:160px;height:44px">Check<b>out</b> now</button>'
	'<ship-box style="position:absolute;left:300px;top:240px;display:block"><span>Ships from Lisbon</span></ship-box>'
	'<div style="position:absolute;left:300px;top:400px"><p>Sizes run true, so pick the larger.</p>'
	'<footer><p>Terms apply to every order.</p></footer></div>'
	"<script>customElements.define('ship-box', class extends HTMLElement { constructor() { super();"
	"this.attachShadow({mode: 'open'}).innerHTML = '<p>Shipping estimate 3 days</p><slot></slot>'; } });</script></body>"
)
CLICK = (
	'<!doctype html><title>Two buttons</title><body style="margin:0;font:16px sans-serif">'
	'<button id="save" style="position:absolute;left:100px;top:100px;width:120px;height:40px">Save</button>'
	'<button id="del" style="position:absolute;left:260px;top:100px;width:120px;height:40px">Delete</button>'
	"<script>window.hits = {save: 0, del: 0}; for (const b of document.querySelectorAll('button'))"
	' b.onclick = () => hits[b.id]++;</script></body>'
)
JWT = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZGEifQ.c2lnbmF0dXJlLW5vdC1yZWFsLTEyMw'
REQUESTS = (
	'<!doctype html><title>Shop</title><body><h1>Shop</h1><script>'
	"fetch('/api/login?access_token=qs-s3cret&lang=en', {method: 'POST', headers: {'content-type': 'application/json'},"
	" body: JSON.stringify({user: 'ada', password: 'hunter2'})})"
	".then(() => fetch('/api/broken'))"
	".then(() => { const i = new Image(); i.src = '/missing.png'; document.body.append(i); });"
	'</script></body>'
)
CONSOLE = (
	'<!doctype html><title>Console</title><body><h1>App</h1><img src="/no-such.png"><script>'
	"console.log('app started');"
	"console.warn('slow response from the cart service');"
	"console.error('payment failed: api_key=sk_live_abcdefghijklmnop1234 rejected');"
	"setTimeout(() => { throw new Error('cart is undefined at render'); }, 50);"
	'</script></body>'
)
WALL = '<!doctype html><title>Just a moment...</title><body>Checking your browser before accessing the site.</body>'


@pytest.fixture(scope='module')
def site():
	server = HTTPServer()
	server.start()
	server.expect_request('/').respond_with_data(PAGE, content_type='text/html')
	server.expect_request('/wall').respond_with_data(WALL, content_type='text/html')
	server.expect_request('/find').respond_with_data(FIND, content_type='text/html')
	server.expect_request('/find-deep').respond_with_data(FIND_DEEP, content_type='text/html')
	server.expect_request('/click').respond_with_data(CLICK, content_type='text/html')
	server.expect_request('/requests').respond_with_data(REQUESTS, content_type='text/html')
	server.expect_request('/console').respond_with_data(CONSOLE, content_type='text/html')
	server.expect_request('/no-such.png').respond_with_data('', status=404)
	server.expect_request('/api/login', method='POST').respond_with_json(
		{'user': {'name': 'Ada'}, 'session': 'sess-v4lue', 'note': JWT, 'greeting': 'hello Ada'}
	)
	server.expect_request('/api/broken').respond_with_json({'error': 'db down'}, status=500)
	server.expect_request('/missing.png').respond_with_data('', status=404)
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
		'retinat_find',
		'retinat_zoom',
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


async def test_changes_says_what_a_list_now_says_when_its_rows_are_rewritten_in_place(retinat, site):
	"""Found hands-on on Wikipedia: typing "Ada Lovelace" into the search box turned the suggestions for "A" into
	"Ada Lovelace English mathematician (1815-1852)" and the rest, in the same rows. Neither changes nor a look said
	so: only the first suggestions, for "A", were ever reported."""
	import asyncio

	rows = ''.join(f'<li><b>Alpha {n}</b> <i>first letter {n}</i></li>' for n in range(1, 8))
	site.expect_request('/suggest-rows').respond_with_data(
		f'<!doctype html><body><input value="Ada Lovelace"><ul id="s" style="position:absolute;top:40px">{rows}</ul><script>'
		"setTimeout(() => { const names = ['Ada Lovelace', 'Ada Lovelace Award', 'Ada Lovelace Day', 'Alan M. Lovelace',"
		" 'Lovelace (film)', 'Ada (programming language)', 'Byron'];"
		" document.querySelectorAll('#s li').forEach((li, i) => { li.querySelector('b').firstChild.data = names[i];"
		" li.querySelector('i').firstChild.data = 'English mathematician ' + (1815 + i); }); }, 2500);</script></body>",
		content_type='text/html',
	)
	await _call(retinat, 'retinat_open', {'url': site.url_for('/suggest-rows')})
	await _call(retinat, 'retinat_changes', {})  # what opening it did
	await asyncio.sleep(3.5)
	text = _text(await _call(retinat, 'retinat_changes', {}))
	assert 'Ada Lovelace Award' in text and 'Byron' in text, text


async def test_changes_says_when_a_request_the_page_made_failed(retinat, site):
	"""Found hands-on on Wikipedia: "Alan Turing" typed into the search box left the suggestions for "Al" on screen,
	and changes said nothing had changed. True, and no help: the suggestion request had been refused (429), which only
	retinat_requests showed. A fetch or XHR that fails is now a change; images and the like are left out."""
	import asyncio

	site.expect_request('/api/suggest').respond_with_data('slow down', status=429, content_type='text/plain')
	site.expect_request('/api/ok').respond_with_data('{"ok": true}', content_type='application/json')
	site.expect_request('/gone.png').respond_with_data('', status=404)
	site.expect_request('/typeahead').respond_with_data(
		'<!doctype html><body><input id="q" value="Alan Turing"><img src="/gone.png"><script>'
		"setTimeout(() => { fetch('/api/ok'); fetch('/api/suggest?q=Alan+Turing'); }, 2500);</script></body>",
		content_type='text/html',
	)
	await _call(retinat, 'retinat_open', {'url': site.url_for('/typeahead')})
	opened = _text(await _call(retinat, 'retinat_changes', {}))  # what opening it did, before the page fetches
	assert '/api/suggest' not in opened, opened
	await asyncio.sleep(3.0)
	text = _text(await _call(retinat, 'retinat_changes', {}))
	assert '429' in text and '/api/suggest' in text, text
	assert '/api/ok' not in text and 'gone.png' not in text, text


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


async def test_find_says_where_text_is_and_zoom_magnifies_small_print(retinat, site):
	await _call(retinat, 'retinat_open', {'url': site.url_for('/find')})

	found = await _call(retinat, 'retinat_find', {'text': 'checkout'})
	text = _text(found)
	assert any(isinstance(b, types.ImageContent) for b in found.content), 'a crop around the match comes back'
	import re

	m = re.search(r'at \((\d+), (\d+)\)', text)
	assert m, text
	x, y = int(m.group(1)), int(m.group(2))
	assert abs(x - (80 + 70)) <= 4 and abs(y - (180 + 22)) <= 4, f'the centre of the button, to click: {text}'

	below = _text(await _call(retinat, 'retinat_find', {'text': 'terms of service'}))
	assert 'below' in below and 'scroll' in below, below
	assert 'not found' in _text(await _call(retinat, 'retinat_find', {'text': 'no such words here'}))

	zoomed = await _call(retinat, 'retinat_zoom', {'x': 590, 'y': 392, 'width': 120, 'height': 24})
	images = [b for b in zoomed.content if isinstance(b, types.ImageContent)]
	assert images, _text(zoomed)
	import base64
	import io

	from PIL import Image

	with Image.open(io.BytesIO(base64.b64decode(images[0].data))) as img:
		assert img.width >= 3 * 120, f'magnified from a fresh capture, not the 640 px frame: {img.size}'


async def test_find_matches_text_split_across_elements_and_inside_shadow_roots(retinat, site):
	import re

	await _call(retinat, 'retinat_open', {'url': site.url_for('/find-deep')})
	split = _text(await _call(retinat, 'retinat_find', {'text': 'checkout now'}))
	m = re.search(r'at \((\d+), (\d+)\)', split)
	assert m and 300 <= int(m.group(1)) <= 460 and 100 <= int(m.group(2)) <= 144, f'inside the button: {split}'
	shadow = _text(await _call(retinat, 'retinat_find', {'text': 'shipping estimate'}))
	assert 'in view' in shadow and 'not found' not in shadow, shadow
	slotted = _text(await _call(retinat, 'retinat_find', {'text': 'ships from lisbon'}))
	assert slotted.count('. at (') == 1, f'slotted light-DOM text is found once, not twice: {slotted}'


async def test_find_keeps_to_one_block_in_its_match_and_its_context(retinat, site):
	await _call(retinat, 'retinat_open', {'url': site.url_for('/find-deep')})
	terms = _text(await _call(retinat, 'retinat_find', {'text': 'terms apply'}))
	assert '"Terms apply to every order."' in terms, f'the context is its own paragraph, nothing more: {terms}'
	across = _text(await _call(retinat, 'retinat_find', {'text': 'larger. terms'}))
	assert 'not found' in across or '0 match' in across, f'two paragraphs are not one phrase: {across}'


LONG_DOC = (
	'<!doctype html><style>body{margin:0;font:18px sans-serif} p{height:300px;margin:0;padding:20px}</style><body>'
	+ ''.join(f'<p>Section {n}</p>' for n in range(1, 40))
	+ '<h2 id="far">asyncio.wait_for(aw, timeout)</h2>'
	+ ''.join(f'<p>After {n}</p>' for n in range(1, 10))
	+ '</body>'
)


async def test_scroll_brings_what_find_found_into_view_and_pages_by_screens(retinat, site):
	"""Found hands-on on the Python docs: find said `wait_for` was "below the visible area: scroll down about 13.0
	screens", and nothing in Retinat scrolled a page (only the desktop tools could): 13 PageDown keys, or swipes.
	retinat_scroll turns the real wheel: by screens, or until a match is in view, then says where it is to click."""
	import re

	site.expect_request('/long').respond_with_data(LONG_DOC, content_type='text/html')
	await _call(retinat, 'retinat_open', {'url': site.url_for('/long')})
	result = await _call(retinat, 'retinat_scroll', {'to': 'wait_for'})
	text = _text(result)
	assert not result.is_error, text
	m = re.search(r'at \((\d+), (\d+)\)[^:]*in view', text)
	assert m, f'the match is said to be in view, with where to click: {text}'
	cdp = await retinat.browser_session.get_or_create_cdp_session(focus=False)
	box = await cdp.cdp_client.send.Runtime.evaluate(
		params={
			'expression': 'JSON.stringify(document.getElementById("far").getBoundingClientRect().toJSON())',
			'returnByValue': True,
		},
		session_id=cdp.session_id,
	)
	import json

	rect = json.loads(box['result']['value'])
	vh = (
		await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': 'innerHeight', 'returnByValue': True}, session_id=cdp.session_id
		)
	)['result']['value']
	assert 0 <= rect['top'] and rect['bottom'] <= vh, f'the heading is on screen: {rect}, viewport {vh}'
	assert abs(int(m.group(2)) - (rect['top'] + rect['height'] / 2)) <= 6, (text, rect)

	before = (
		await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': 'scrollY', 'returnByValue': True}, session_id=cdp.session_id
		)
	)['result']['value']
	paged = _text(await _call(retinat, 'retinat_scroll', {'screens': -2}))
	after = (
		await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': 'scrollY', 'returnByValue': True}, session_id=cdp.session_id
		)
	)['result']['value']
	assert abs((before - after) - 2 * vh) <= 0.25 * vh, (before, after, vh, paged)
	assert 'up' in paged and 'screens' in paged, paged


async def test_click_says_what_it_hit_and_refuses_when_it_is_not_what_was_expected(retinat, site):
	"""A page moves between a look and a click; saying what is under the point, and refusing a mismatch, keeps an
	agent from pressing Delete while it believes it pressed Save (after BrowserSkill's hit verification)."""
	await _call(retinat, 'retinat_open', {'url': site.url_for('/click')})
	saved = _text(await _call(retinat, 'retinat_click', {'x': 160, 'y': 120, 'expect': 'Save'}))
	assert 'Clicked' in saved and 'button "Save"' in saved, saved
	wrong = _text(await _call(retinat, 'retinat_click', {'x': 320, 'y': 120, 'expect': 'Save'}))
	assert 'Not clicked' in wrong and 'button "Delete"' in wrong, wrong
	plain = _text(await _call(retinat, 'retinat_click', {'x': 160, 'y': 120}))
	assert 'button "Save"' in plain, f'without expect it still says what it clicked: {plain}'
	assert retinat.browser_session is not None
	cdp = await retinat.browser_session.get_or_create_cdp_session(focus=False)
	hits = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': 'JSON.stringify(hits)', 'returnByValue': True}, session_id=cdp.session_id
	)
	assert json.loads(hits['result'].get('value', '{}')) == {'save': 2, 'del': 0}


async def test_requests_lists_what_the_page_fetched_with_its_secrets_masked(retinat, site):
	"""After BrowserSkill's network evidence: what the page asked for and got, failures first in mind, without handing
	the AI the tokens in it. Masked by key name and by value (a JWT under an innocent key is still a JWT)."""
	import asyncio
	import re

	await _call(retinat, 'retinat_open', {'url': site.url_for('/requests')})
	listed = ''
	for _ in range(50):
		listed = _text(await _call(retinat, 'retinat_requests', {}))
		if '/missing.png' in listed and 'fetch /api/broken' in listed:
			break
		await asyncio.sleep(0.1)
	assert 'POST 200 fetch /api/login?access_token=[redacted]&lang=en' in listed, listed
	assert 'GET 500 fetch /api/broken' in listed and '404 image /missing.png' in listed, listed
	assert 'qs-s3cret' not in listed

	failed = _text(await _call(retinat, 'retinat_requests', {'only': 'failed'}))
	assert '/api/broken' in failed and '/missing.png' in failed and '/api/login' not in failed, failed

	login = int(re.search(r'#(\d+) POST', listed).group(1))  # type: ignore[union-attr]
	body = _text(await _call(retinat, 'retinat_requests', {'body': login}))
	assert '"name":"Ada"' in body and 'hello Ada' in body, body
	assert 'sess-v4lue' not in body and JWT not in body and '[redacted]' in body, body
	broken = int(re.search(r'#(\d+) GET 500 fetch /api/broken', listed).group(1))  # type: ignore[union-attr]
	assert 'db down' in _text(await _call(retinat, 'retinat_requests', {'body': broken}))

	last = int(re.search(r'since=(\d+)', listed).group(1))  # type: ignore[union-attr]
	assert _text(await _call(retinat, 'retinat_requests', {'since': last})).startswith('0 requests after')


async def test_a_failed_call_says_whether_anything_happened(retinat, site):
	"""After BrowserSkill's effect_state. An agent retrying a failed action must know whether the first try already
	did something: none (nothing was sent, retry freely), unknown (input or navigation had started: look first),
	committed (it happened; only what followed failed)."""
	import socket

	await _call(retinat, 'retinat_open', {'url': site.url_for('/click')})
	refused = await _call(retinat, 'retinat_click', {'x': 320, 'y': 120, 'expect': 'Save'})
	assert refused.is_error and (refused.structured_content or {}).get('effect_state') == 'none', _text(refused)
	assert 'effect: none' in _text(refused)
	backwards = await _call(retinat, 'retinat_recall', {'t0': 5, 't1': 1})
	assert (backwards.structured_content or {}).get('effect_state') == 'none', 'a tool that only looks changed nothing'
	nonsense = await _call(retinat, 'retinat_fly', {})
	assert (nonsense.structured_content or {}).get('effect_state') == 'none'

	with socket.socket() as sock:
		sock.bind(('127.0.0.1', 0))
		dead = sock.getsockname()[1]
	gone = await _call(retinat, 'retinat_open', {'url': f'http://127.0.0.1:{dead}/'})
	assert gone.is_error and (gone.structured_content or {}).get('effect_state') == 'unknown', _text(gone)
	assert 'look' in _text(gone).split('effect: unknown', 1)[1], 'unknown says to look before trying again'


async def test_effect_is_committed_when_the_act_went_through_and_only_what_followed_failed():
	from browser_use.mcp import effects

	async def press() -> str:
		return 'pressed'

	token = effects.begin()
	try:
		assert await effects.act(press()) == 'pressed'
		result = effects.failure('retinat_key', RuntimeError('the reading after it failed'), read_only=False)
	finally:
		effects.end(token)
	assert result.structured_content == {
		'tool': 'retinat_key',
		'error': 'the reading after it failed',
		'effect_state': 'committed',
	}
	assert "don't repeat it" in result.content[0].text  # type: ignore[union-attr]


async def test_console_shows_what_the_page_logged_and_threw_with_secrets_masked(retinat, site):
	"""After Claude in Chrome's read_console_messages: errors, warnings, logs and uncaught exceptions, newest last,
	filtered by level or pattern, with keys and tokens masked like retinat_requests masks them."""
	import asyncio

	await _call(retinat, 'retinat_open', {'url': site.url_for('/console')})
	everything = ''
	for _ in range(50):
		everything = _text(await _call(retinat, 'retinat_console', {}))
		if 'cart is undefined' in everything:
			break
		await asyncio.sleep(0.1)
	assert 'app started' in everything and 'slow response' in everything, everything
	assert 'payment failed' in everything and 'sk_live_' not in everything and '[redacted]' in everything, everything
	assert 'cart is undefined at render' in everything, 'uncaught exceptions are there too'
	for _ in range(30):
		if 'no-such.png' in everything:
			break
		await asyncio.sleep(0.1)
		everything = _text(await _call(retinat, 'retinat_console', {}))
	assert '[network]' in everything and 'no-such.png' in everything, "the browser's own entries (a failed load) too"
	cursor = int(everything.split('since=')[1].split('.')[0])
	assert _text(await _call(retinat, 'retinat_console', {'since': cursor})).startswith('0 console entries'), 'cursor'
	errors = _text(await _call(retinat, 'retinat_console', {'level': 'error'}))
	assert 'payment failed' in errors and 'cart is undefined' in errors and 'app started' not in errors, errors
	assert 'slow response' not in errors
	cart = _text(await _call(retinat, 'retinat_console', {'pattern': 'cart'}))
	assert 'cart is undefined' in cart and 'slow response from the cart' in cart and 'payment failed' not in cart, cart


async def test_record_saves_a_video_of_what_the_browser_showed(retinat, site, tmp_path, monkeypatch):
	"""After Claude in Chrome's gif_creator: the agent records its session for the person to review. Start, act, stop:
	a finished video file whose frames show the pages changing. An MCP server keeps its browser open, so a recording
	that only closed with the browser was never playable."""
	import asyncio
	from pathlib import Path

	import imageio.v3 as iio
	import numpy as np

	monkeypatch.setenv('BROWSER_USE_RECORD_VIDEO_DIR', str(tmp_path / 'videos'))
	await _call(retinat, 'retinat_open', {'url': site.url_for('/')})
	started = _text(await _call(retinat, 'retinat_record', {'action': 'start', 'framerate': 5}))
	assert 'Recording to' in started, started
	await asyncio.sleep(1.5)
	await _call(retinat, 'retinat_open', {'url': site.url_for('/find')})
	await asyncio.sleep(1.5)
	saved = _text(await _call(retinat, 'retinat_record', {'action': 'stop'}))
	path = Path(saved.split('Saved the recording: ')[1].split(' (')[0])
	assert path.parent == tmp_path / 'videos' and path.stat().st_size > 1000, saved
	frames = list(iio.imiter(path))
	assert len(frames) >= 4, f'{len(frames)} frames'
	change = float(np.abs(frames[0].astype(int) - frames[-1].astype(int)).mean())
	assert change > 1.0, f'the first and last frames are the same page ({change:.2f})'
	again = await _call(retinat, 'retinat_record', {'action': 'stop'})
	assert again.is_error and 'no recording is running' in _text(again), _text(again)


async def test_reflexes_act_the_moment_a_button_appears_and_the_agent_sleeps_until_told(retinat, site):
	"""retinat_reflex installs rules the page runs every frame; retinat_wait sleeps until one notifies. Here a button
	shows up after a second and a rule clicks it as it appears, with real input, while the agent waits in one call."""
	page = (
		'<!doctype html><title>Late</title><body><script>window.clicks = 0;'
		"setTimeout(() => { const b = document.createElement('button'); b.id = 'go'; b.textContent = 'Claim';"
		"b.style = 'position:fixed;left:200px;top:150px;width:160px;height:50px';"
		'b.onclick = (e) => { if (e.isTrusted) clicks++; }; document.body.appendChild(b); }, 1000)</script></body>'
	)
	site.expect_request('/late').respond_with_data(page, content_type='text/html')
	await _call(retinat, 'retinat_open', {'url': site.url_for('/late')})
	rules = [
		{'when': "see.text('#go') === 'Claim'", 'click': "[see.el('#go').cx, see.el('#go').cy]", 'notify': 'claimed'},
	]
	installed = _text(await _call(retinat, 'retinat_reflex', {'rules': rules}))
	assert 'running every frame' in installed, installed
	woke = _text(await _call(retinat, 'retinat_wait', {'timeout': 10}))
	assert '"claimed"' in woke, woke
	assert retinat.browser_session is not None
	cdp = await retinat.browser_session.get_or_create_cdp_session(focus=False)
	clicks = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': 'clicks', 'returnByValue': True}, session_id=cdp.session_id
	)
	assert clicks['result']['value'] == 1, 'one real click, as the button appeared'
	bad = await _call(retinat, 'retinat_reflex', {'rules': [{'when': 'see.el(', 'notify': 'x'}]})
	assert bad.is_error and 'did not compile' in _text(bad), _text(bad)


async def test_reflexes_through_retinat_act_within_a_frame_or_two(retinat, site):
	"""Found live on the T-Rex runner: through Retinat, decision to input was ~80 ms at p50, while the library alone
	measures ~2 ms. A reflex that lands five frames late is a reflex too late for a game."""
	from tests.ci.test_reflex import FOLLOW, GAME

	site.expect_request('/follow').respond_with_data(GAME, content_type='text/html')
	await _call(retinat, 'retinat_open', {'url': site.url_for('/follow')})
	await _call(retinat, 'retinat_look', {})  # as an agent does before it writes rules: the page watcher is running too
	installed = _text(await _call(retinat, 'retinat_reflex', {'rules': FOLLOW}))
	assert 'running every frame' in installed, installed
	woke = _text(await _call(retinat, 'retinat_wait', {'timeout': 10}))
	assert '"game over"' in woke, woke
	stats = json.loads(_text(await _call(retinat, 'retinat_reflex', {'action': 'status'})))
	assert stats['actions'] >= 4, stats
	assert stats['latency_ms_p50'] is not None and stats['latency_ms_p50'] < 40, f'decision to key arriving: {stats}'


async def test_a_look_after_a_recorded_game_shows_how_it_ended(retinat, site, tmp_path, monkeypatch):
	"""Found live on the T-Rex runner, through these tools: look, record, the game played and stopped, look: the
	look showed a frame from before the end. The page sweeps a white bar while it animates: a hue drifting 3.3 degrees a
	frame moves no grid cell past changed()'s threshold, so the rule below once called the animation still 0.75 s in
	and the look, rightly, showed it mid-way (the page's own clock said so: drawn last at 2013 ms, looked at before).
	Two causes were real: the page view starved when the recorder took its stream (ecac0a8); the recorder lost the
	repaint that ends an animation (5a16efe). A third, a stream read by rules going stale, was that faulty stop rule:
	with the bar, this test passes 6 of 6 on 5a16efe too. Send the last look after retinat_wait has returned: sent
	alongside it, a look shows the page as it was when the wait began."""
	import base64
	import io

	from PIL import Image

	monkeypatch.setenv('BROWSER_USE_RECORD_VIDEO_DIR', str(tmp_path / 'videos'))
	site.expect_request('/runs').respond_with_data(
		'<!doctype html><body style="margin:0;background:#fff"><h1 id="h">Press Space to start</h1>'
		'<canvas id="c" width="600" height="150" style="display:block;margin:auto"></canvas><script>'
		'const c = document.getElementById("c"), g = c.getContext("2d"); let t0 = null;'
		'addEventListener("keydown", () => { if (t0 === null) { t0 = performance.now(); document.getElementById("h").remove();'
		' c.style.width = "100vw"; c.style.height = "50vh"; requestAnimationFrame(frame); } });'
		'function frame(now) { const t = now - t0; window.lastT = Math.round(t); g.fillStyle = t < 2000 ? `hsl(${t / 5}, 80%, 50%)` : "#0a0";'
		' g.fillRect(0, 0, 600, 150); if (t < 2000) { g.fillStyle = "#fff"; g.fillRect((t * 1.5) % 600, 0, 60, 150); }'
		' if (t < 2000) requestAnimationFrame(frame); }</script></body>',
		content_type='text/html',
	)
	await _call(retinat, 'retinat_open', {'url': site.url_for('/runs')})
	await _call(retinat, 'retinat_open', {'url': site.url_for('/runs')})
	await _call(retinat, 'retinat_look', {})
	await _call(retinat, 'retinat_record', {'action': 'start', 'framerate': 10})
	try:
		rules = [
			{'when': "see.frame > 60 && see.frame % 60 === 0 && !!document.getElementById('h')", 'key': ' '},
			{
				'when': "!document.getElementById('h') && (see.memory.q = see.changed({from: '#c'}) === 0 ? (see.memory.q || 0) + 1 : 0) > 45",
				'notify': 'stopped',
			},
		]
		await _call(retinat, 'retinat_reflex', {'rules': rules})
		woke = _text(await _call(retinat, 'retinat_wait', {'timeout': 20}))
		assert '"stopped"' in woke, woke
		look = await _call(retinat, 'retinat_look', {})
	finally:
		await _call(retinat, 'retinat_record', {'action': 'stop'})
	image = next(b for b in look.content if isinstance(b, types.ImageContent))
	with Image.open(io.BytesIO(base64.b64decode(image.data))) as img:
		middle = img.convert('RGB').getpixel((img.width // 2, img.height // 4))  # inside the canvas (top half)
	if not (middle[1] > 120 and middle[0] < 100):
		cdp = await retinat.browser_session.get_or_create_cdp_session(focus=False)
		r = await cdp.cdp_client.send.Runtime.evaluate(
			params={
				'expression': 'JSON.stringify({lastT: window.lastT, now: Math.round(performance.now() - t0)})',
				'returnByValue': True,
			},
			session_id=cdp.session_id,
		)
		page = r.get('result', {}).get('value')
		raise AssertionError(f'the look shows {middle}, not the game as it ended (green); the page: {page}; {_text(look)}')


async def test_a_look_asked_while_a_wait_is_pending_says_so(retinat, site):
	"""Found live on the T-Rex runner: looks sent alongside retinat_wait ran at once, before the game ended, and I read
	them as stale pictures of its end for an hour. A look asked while a wait is pending says so; one after it does not."""
	import asyncio

	site.expect_request('/later').respond_with_data(
		'<!doctype html><body><p id="p">before</p>'
		'<script>setTimeout(() => { document.getElementById("p").textContent = "after"; }, 4000)</script></body>',
		content_type='text/html',
	)
	await _call(retinat, 'retinat_open', {'url': site.url_for('/later')})
	await _call(retinat, 'retinat_reflex', {'rules': [{'when': "see.text('#p') === 'after'", 'notify': 'changed'}]})
	waiting = asyncio.create_task(_call(retinat, 'retinat_wait', {'timeout': 20}))
	await asyncio.sleep(0.2)
	early = _text(await _call(retinat, 'retinat_look', {}))
	woke = _text(await waiting)
	assert '"changed"' in woke, woke
	late = _text(await _call(retinat, 'retinat_look', {}))
	assert 'still waiting' in early and 'retinat_wait' in early, early
	assert 'still waiting' not in late, late
