"""The token-efficient capabilities must reach MCP clients, not just the Python API.

Claude Code, Codex and Antigravity drive browser-use over MCP, where there is no
`<browser_state>` block to advertise anything — so a capability that exists only in the
Python agent loop is invisible to them. These tests exercise the three additions through
the real `tools/call` handler against a real page.
"""

import json

import mcp.types as types
import pytest
from pytest_httpserver import HTTPServer

from browser_use.mcp.server import BrowserUseServer

TABLE_PAGE = (
	'<!DOCTYPE html><html><head><title>Rows</title></head><body><table>'
	+ ''.join(f'<tr><td class="n">Item {i}</td><td class="p">{i * 2}</td></tr>' for i in range(25))
	+ '</table></body></html>'
)

WEBMCP_PAGE = """<!DOCTYPE html><html><head><title>Booking</title></head><body>
<script>
	navigator.modelContext.registerTool({
		name: 'book_seat',
		description: 'Reserve a seat by number',
		inputSchema: {type: 'object', properties: {seat: {type: 'string'}}, required: ['seat']},
		execute: (args) => 'reserved ' + args.seat,
	});
</script></body></html>"""


@pytest.fixture(scope='module')
def mcp_server_pages():
	server = HTTPServer()
	server.start()
	server.expect_request('/rows').respond_with_data(TABLE_PAGE, content_type='text/html')
	server.expect_request('/booking').respond_with_data(WEBMCP_PAGE, content_type='text/html')
	yield server
	server.stop()


@pytest.fixture
async def server(tmp_path):
	mcp_server = BrowserUseServer()
	# The server's shipped defaults are headful with a persistent profile directory, which
	# is right for a desktop client and impossible on a CI box with no display. These are
	# the two knobs a headless deployment changes; everything else stays as shipped.
	mcp_server.config.setdefault('browser_profile', {}).update({'headless': True, 'user_data_dir': str(tmp_path / 'profile')})
	yield mcp_server
	await _call(mcp_server, 'browser_close_all', {})


async def _call(server: BrowserUseServer, name: str, arguments: dict) -> str:
	"""Invoke a tool the way an MCP client does, and return its text content."""
	handler = server.server.get_request_handler('tools/call')
	assert handler is not None, 'tools/call handler is not registered'
	result = await handler.handler(
		None,  # type: ignore[arg-type]
		types.CallToolRequestParams(name=name, arguments=arguments),
	)
	assert isinstance(result, types.CallToolResult)
	texts = [block.text for block in result.content if isinstance(block, types.TextContent)]
	return '\n'.join(texts)


async def test_browser_run_script_returns_bulk_data_in_one_call(server, mcp_server_pages):
	await _call(server, 'browser_navigate', {'url': mcp_server_pages.url_for('/rows')})

	out = await _call(server, 'browser_run_script', {'script': "return $$('.n').map(txt);"})
	names = json.loads(out)
	assert len(names) == 25
	assert names[0] == 'Item 0'
	assert names[-1] == 'Item 24'


async def test_browser_run_script_reports_a_bad_script_verbatim(server, mcp_server_pages):
	await _call(server, 'browser_navigate', {'url': mcp_server_pages.url_for('/rows')})

	out = await _call(server, 'browser_run_script', {'script': 'return totallyUndefined();'})
	assert out.startswith('Script failed:')
	assert 'totallyUndefined' in out


async def test_page_declared_tools_are_discoverable_and_callable_over_mcp(server, mcp_server_pages):
	await _call(server, 'browser_navigate', {'url': mcp_server_pages.url_for('/booking')})

	listed = json.loads(await _call(server, 'browser_list_page_tools', {}))
	assert [tool['name'] for tool in listed] == ['book_seat']
	assert listed[0]['input_schema']['required'] == ['seat']

	out = await _call(server, 'browser_call_page_tool', {'name': 'book_seat', 'arguments': '{"seat": "14C"}'})
	assert out == 'reserved 14C'


async def test_a_page_without_declared_tools_says_so_plainly(server, mcp_server_pages):
	"""The client needs to know to fall back to the UI, not just get an empty list."""
	await _call(server, 'browser_navigate', {'url': mcp_server_pages.url_for('/rows')})

	out = await _call(server, 'browser_list_page_tools', {})
	assert 'no WebMCP tools' in out
	assert 'UI' in out


async def test_malformed_tool_arguments_are_rejected_before_reaching_the_page(server, mcp_server_pages):
	await _call(server, 'browser_navigate', {'url': mcp_server_pages.url_for('/booking')})

	assert 'must be a JSON object' in await _call(
		server, 'browser_call_page_tool', {'name': 'book_seat', 'arguments': 'not json'}
	)
	assert 'must be a JSON object' in await _call(server, 'browser_call_page_tool', {'name': 'book_seat', 'arguments': '[1, 2]'})


# A site with a form and nothing agent-specific: the ordinary case.
PLAIN_SHOP = """<!DOCTYPE html><html><head><title>Shop</title></head><body>
	<form><input type="search" aria-label="Search stock"><button type="submit">Search</button></form>
	<div id="out">idle</div>
<script>
	document.querySelector('form').addEventListener('submit', e => {
		e.preventDefault();
		document.getElementById('out').textContent = 'found ' + document.querySelector('input').value;
	});
</script></body></html>"""


async def test_the_sites_tools_appear_in_the_mcp_tool_list(server, mcp_server_pages):
	"""An MCP client should not have to know to ask what a page offers.

	Requiring browser_list_page_tools first means most clients never will, and the point of
	the synthesis layer is that site_search is simply there once you are on a site that can
	search.
	"""
	mcp_server_pages.expect_request('/plainshop').respond_with_data(PLAIN_SHOP, content_type='text/html')

	before = {tool.name for tool in await _list_tools(server)}
	assert not any(name.startswith('site_') for name in before), 'site tools before navigating anywhere'

	await _call(server, 'browser_navigate', {'url': mcp_server_pages.url_for('/plainshop')})

	after = {tool.name for tool in await _list_tools(server)}
	new_tools = after - before
	assert 'site_search' in new_tools, f'the page tools were not advertised: {sorted(new_tools)}'


async def test_a_site_tool_can_be_called_directly(server, mcp_server_pages):
	mcp_server_pages.expect_request('/plainshop').respond_with_data(PLAIN_SHOP, content_type='text/html')
	await _call(server, 'browser_navigate', {'url': mcp_server_pages.url_for('/plainshop')})

	out = await _call(server, 'site_search', {'query': 'widgets'})
	assert 'failed' not in out.lower(), out

	shown = await _call(server, 'browser_run_script', {'script': "return document.getElementById('out').textContent;"})
	assert json.loads(shown) == 'found widgets'


async def test_a_site_tool_that_is_not_on_this_page_says_what_is(server, mcp_server_pages):
	await _call(server, 'browser_navigate', {'url': mcp_server_pages.url_for('/rows')})

	out = await _call(server, 'site_definitely_not_here', {})
	assert 'not a tool on the current page' in out


async def _list_tools(server):
	import mcp.types as types

	handler = server.server.get_request_handler('tools/list')
	assert handler is not None
	result = await handler.handler(None, types.PaginatedRequestParams())  # type: ignore[arg-type]
	return result.tools
