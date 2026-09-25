"""reset() must clear every session-scoped cache it owns, including auto-dismissed dialogs.

PopupsWatchdog appends to `BrowserSession._closed_popup_messages` (session.py) whenever it
auto-accepts a JavaScript dialog, and DOMWatchdog copies the whole list into every
`BrowserStateSummary` it builds — so it is shown to the LLM as "Auto-closed JavaScript
dialogs" on every single step for the rest of the session. `reset()` clears every other
per-session cache it owns (`_cached_selector_map`, `_cached_selector_indices`,
`_downloaded_files`) so a session that stops and starts again does not resurface state from
whatever ran before it. `_closed_popup_messages` was the one cache reset() forgot: a dialog
auto-dismissed before a stop()/start() cycle kept being reported as freshly closed in every
browser state of the next browsing session on the same BrowserSession object.
"""

import asyncio

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

ALERT_PAGE = """
<html>
<head><title>alert page</title></head>
<body>
<script>alert('leftover dialog message');</script>
<p>after alert</p>
</body>
</html>
"""


@pytest.fixture(scope='module')
def alert_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/alert').respond_with_data(ALERT_PAGE, content_type='text/html')
	yield server
	server.stop()


async def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> None:
	loop = asyncio.get_event_loop()
	deadline = loop.time() + timeout
	while loop.time() < deadline:
		if predicate():
			return
		await asyncio.sleep(interval)
	raise TimeoutError('condition not met before timeout')


async def test_reset_clears_closed_popup_messages(alert_server: HTTPServer):
	"""A dialog auto-dismissed before reset() must not resurface after it.

	`_closed_popup_messages` is what DOMWatchdog copies verbatim into every future
	BrowserStateSummary as "Auto-closed JavaScript dialogs" - a per-session log, not a
	permanent transcript. reset() is the function BrowserSession documents as the place
	that clears its caches for reuse (see the `_cached_selector_map` / `_downloaded_files`
	clears right next to it), so a stale dialog message surviving it is a real leak, not a
	documentation gap elsewhere.
	"""
	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None))
	await session.start()
	try:
		await session.navigate_to(alert_server.url_for('/alert'))

		# PopupsWatchdog auto-accepts the dialog and records the message asynchronously.
		await _wait_until(lambda: len(session._closed_popup_messages) > 0, timeout=10.0)
		assert all(msg == '[alert] leftover dialog message' for msg in session._closed_popup_messages)

		await session.reset()

		assert session._closed_popup_messages == [], (
			'reset() left a stale popup message behind - it would be reported as a freshly '
			'auto-closed dialog in every BrowserStateSummary of the next browsing session on '
			'this BrowserSession object, even though nothing was actually closed in it'
		)
	finally:
		await session.kill()


async def test_stop_then_restart_does_not_resurface_old_dialog_in_state(alert_server: HTTPServer):
	"""End-to-end: stop() a session that saw a dialog, start it again, and confirm the next
	BrowserStateSummary is clean - reproducing exactly what DOMWatchdog copies into the LLM prompt."""
	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None))
	await session.start()
	try:
		await session.navigate_to(alert_server.url_for('/alert'))
		await _wait_until(lambda: len(session._closed_popup_messages) > 0, timeout=10.0)

		await session.stop()
		await session.start()

		state = await session.get_browser_state_summary(include_screenshot=False)
		assert state.closed_popup_messages == [], (
			f'stale dialog message resurfaced after stop()/start(): {state.closed_popup_messages}'
		)
	finally:
		await session.kill()
