"""The bridge's manifest deadline has to bound the whole loading pass, not just the gaps
between refs — and the bridge must never install a `window.agent` fingerprint.

Before the fix: a manifest server that never responds left the in-page `fetch()` (and the
`tools/list` RPC it can trigger) running with no abort signal, so the shared
`inFlightDiscovery` promise never settled and every `bridge.call()` for an unknown tool
name hung on it forever. Separately, `window.agent` was installed despite the bridge's
own comment saying it deliberately was not, handing anti-bot scripts a free fingerprint.
"""

import json
import time

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

# A manifest ref plus a JS-registered tool: discovery must still surface 'ping' even
# though the manifest fetch behind it never returns.
HANG_PAGE = """<!DOCTYPE html>
<html><head><title>Hang</title><link rel="model-context" href="/hang"></head>
<body>
<script>
	navigator.modelContext.registerTool({
		name: 'ping',
		description: 'Always answers',
		execute: () => 'pong',
	});
</script>
</body></html>"""


@pytest.fixture(scope='module')
def hang_server():
	# threaded=True: /hang's handler sleeps for the full 20s regardless of whether the
	# client already aborted its fetch (a plain time.sleep() has no way to notice a
	# dropped connection), so with the default single-worker dev server, one test's
	# still-draining /hang request would serialize behind the next test's own
	# navigation-time fetch to the same path — exceeding the 30s navigation watchdog
	# even though the bridge's own client-side abort fires as designed.
	server = HTTPServer(threaded=True)
	server.start()
	server.expect_request('/hang-page').respond_with_data(HANG_PAGE, content_type='text/html')

	def hang_manifest(request):
		# Longer than the bridge's manifestMs (5s) and than the "about 8s" acceptance
		# budget, so the test only passes if the fetch is actually aborted client-side
		# rather than merely raced against a timeout that happens to win.
		time.sleep(20)
		return '{}'

	server.expect_request('/hang', method='GET').respond_with_handler(hang_manifest)

	yield server
	server.stop()


@pytest.fixture(scope='module')
async def webmcp_hang_session():
	session = BrowserSession(
		browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=True, enable_webmcp=True)
	)
	await session.start()
	yield session
	await session.kill()
	await session.event_bus.stop(clear=True, timeout=5)


async def _goto(session: BrowserSession, url: str) -> None:
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


async def test_a_hanging_manifest_does_not_hang_discovery_or_unknown_tool_calls(webmcp_hang_session, hang_server):
	"""call_webmcp_tool() for a name the page never declared has to fall through discovery
	(which waits on the hanging manifest) and still come back promptly, because the
	manifest fetch is now bounded by its own deadline instead of running unbounded."""
	await _goto(webmcp_hang_session, hang_server.url_for('/hang-page'))

	start = time.monotonic()
	result = await webmcp_hang_session.call_webmcp_tool('missing_tool', {})
	elapsed = time.monotonic() - start

	assert not result.ok
	assert result.error is not None and 'no WebMCP tool named' in result.error
	assert elapsed < 8.0, f'call_webmcp_tool() took {elapsed:.1f}s, the hanging manifest was not bounded'

	# A later discovery pass (this one not racing the still-warming manifest cache) still
	# lists the JS-registered tool: the earlier timeout did not wedge the bridge.
	page_tools = await webmcp_hang_session.get_webmcp_tools()
	assert 'ping' in {tool.name for tool in page_tools.tools}


async def test_window_agent_is_never_installed(webmcp_hang_session, hang_server):
	"""navigator.modelContext is the bridge's real, intentional surface; window.agent is
	not part of any WebMCP draft and must not exist, hanging manifest or not."""
	await _goto(webmcp_hang_session, hang_server.url_for('/hang-page'))

	result = await webmcp_hang_session.run_page_script(
		"return {agent: typeof window.agent, hasModelContext: 'modelContext' in navigator};"
	)
	assert result.ok, result.error
	observed = json.loads(result.value)
	assert observed['agent'] == 'undefined'
	assert observed['hasModelContext'] is True
