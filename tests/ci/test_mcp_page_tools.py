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
