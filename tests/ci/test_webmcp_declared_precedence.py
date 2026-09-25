"""A real declaration always wins, even over a stale synthesized cache.

Synthesis fills in for sites that publish nothing. But an SPA can register its own
WebMCP tool *after* the page has already been scanned once — after load, or on a
same-origin navigation. When that happens, a later call to that tool name must run
the page's own declared handler, not whatever UI steps synthesis guessed the first
time around.

Real Chromium, real HTTP server; only the LLM would ever be faked here, and this test
never reaches one.
"""

import asyncio
import json

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent

# A search form synthesis will happily turn into a `search` tool, on a page that,
# about a second after load, registers its own `search` tool under the same name.
# If the form ever actually submits, the fix failed.
SPA_PAGE = """<!DOCTYPE html>
<html><head><title>Late Bloomer</title></head><body>
	<form id="searchform">
		<input type="search" id="q" aria-label="Search products">
		<button type="submit">Search</button>
	</form>
	<div id="result">nothing yet</div>
<script>
	searchform.addEventListener('submit', (e) => {
		e.preventDefault();
		window.__formUsed = true;
		document.getElementById('result').textContent = 'searched for ' + q.value;
	});
	setTimeout(() => {
		navigator.modelContext.registerTool({
			name: 'search',
			description: 'The search this SPA declared after it finished loading',
			inputSchema: {type: 'object', properties: {q: {type: 'string'}}, required: ['q']},
			execute: (args) => 'declared:' + args.q,
		});
	}, 1000);
</script>
</body></html>"""


@pytest.fixture(scope='module')
def spa_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/spa').respond_with_data(SPA_PAGE, content_type='text/html')
	yield server
	server.stop()


@pytest.fixture(scope='module')
async def browser_session(webmcp_session):
	"""WebMCP must be turned on for a page's own declarations to be visible at all."""
	return webmcp_session


async def _goto(session, url):
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


async def test_a_later_declared_tool_beats_a_stale_synthesized_one(browser_session, spa_server):
	await _goto(browser_session, spa_server.url_for('/spa'))

	# Before the page declares anything, synthesis fills in a `search` tool from the form.
	first = await browser_session.get_webmcp_tools()
	by_name = {tool.name: tool for tool in first.tools}
	assert 'search' in by_name, f'no synthesized search among {sorted(by_name)}'
	assert by_name['search'].source == 'synthesized'

	# Wait past the page's own registerTool() call, then discover again.
	await asyncio.sleep(1.5)
	second = await browser_session.get_webmcp_tools()
	by_name2 = {tool.name: tool for tool in second.tools}
	assert 'search' in by_name2, f'no declared search among {sorted(by_name2)}'
	assert by_name2['search'].source == 'js', 'a real declaration must replace the synthesized guess'

	# The synthesizer's own per-origin cache is still holding the stale, form-based
	# manifest from the first scan (discover() only re-synthesizes when a page declares
	# nothing) — this is exactly the trap the old code fell into.
	result = await browser_session.call_webmcp_tool('search', {'q': 'x'})
	assert result.ok, result.error
	assert result.content == 'declared:x'

	form_used = await browser_session.run_page_script('return window.__formUsed === true;')
	assert json.loads(form_used.value) is False, 'call_tool drove the old synthesized UI steps instead of the declared tool'
