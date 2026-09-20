"""What a page can tell about the browser driving it.

None of this is about hiding that an agent is at the controls — a site that asks is
entitled to an honest answer, and `navigator.webdriver` stays exactly as Chromium sets
it. It is about not emitting signals that are artefacts of *how the browser was started*
rather than facts about it, because those break ordinary browsing for the person whose
profile this is. A browser reporting a window smaller than its own viewport is not
disclosing anything; it is reporting something impossible.
"""

import json

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

PAGE = '<html><head><title>fingerprint</title></head><body>hello</body></html>'


@pytest.fixture(scope='module')
def fingerprint_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/probe').respond_with_data(PAGE, content_type='text/html')
	yield server
	server.stop()


async def _probe(session, server) -> dict:
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=server.url_for('/probe')))
	await event
	await event.event_result(raise_if_any=False, raise_if_none=False)
	result = await session.run_page_script(
		"""
		return {
			ua: navigator.userAgent,
			outer: [window.outerWidth, window.outerHeight],
			inner: [window.innerWidth, window.innerHeight],
			has_model_context: 'modelContext' in navigator,
		};
		""",
		max_chars=4000,
	)
	assert result.ok, result.error
	return json.loads(result.value)


async def test_a_headless_browser_does_not_announce_itself_in_the_user_agent(fingerprint_server):
	"""Chromium writes "HeadlessChrome" into the UA purely because of how it was launched.

	It is the same binary rendering the same pages, and that string is the single loudest
	signal a site reads — enough on its own to get a person's own session challenged.
	"""
	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=True))
	await session.start()
	try:
		observed = await _probe(session, fingerprint_server)
		assert 'HeadlessChrome' not in observed['ua'], observed['ua']
		assert 'Chrome/' in observed['ua'], observed['ua']
	finally:
		await session.kill()


async def test_the_window_is_not_smaller_than_its_own_viewport(fingerprint_server):
	"""Headless left the OS window at Chrome's 780x580 default while the viewport was
	overridden, so outerWidth came back *below* innerWidth. No real browser can be in that
	state, and it costs one flag to fix."""
	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=True))
	await session.start()
	try:
		observed = await _probe(session, fingerprint_server)
		outer_w, outer_h = observed['outer']
		inner_w, inner_h = observed['inner']
		assert outer_w >= inner_w, f'outerWidth {outer_w} < innerWidth {inner_w}'
		assert outer_h >= inner_h, f'outerHeight {outer_h} < innerHeight {inner_h}'
	finally:
		await session.kill()


async def test_no_page_sees_an_api_that_no_browser_ships(fingerprint_server):
	"""navigator.modelContext exists in no shipping browser, so its presence identifies
	the session to every script on every page. Since almost no site declares WebMCP tools,
	the default pays that for nothing — and synthesis needs no injection at all."""
	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=True))
	await session.start()
	try:
		observed = await _probe(session, fingerprint_server)
		assert observed['has_model_context'] is False
	finally:
		await session.kill()
