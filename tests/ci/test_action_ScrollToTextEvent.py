"""ScrollToTextEvent must find text that is actually on the page, quotes included.

`on_ScrollToTextEvent` splices the agent-supplied target string straight into three
XPath predicates and, on fallback, into a JS string literal. A single `"` in the
target closes the literal early, so every search path is malformed and the action
reports "Text not found" for text that is plainly in the DOM.
"""

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent, ScrollToTextEvent
from browser_use.browser.watchdogs.default_action_watchdog import _xpath_string_literal

PAGE = """<!DOCTYPE html>
<html><head><title>Quoted copy</title></head>
<body>
	<div style="height: 4000px">spacer</div>
	<p id="plain">Continue to the next step</p>
	<div style="height: 4000px">spacer</div>
	<p id="quoted">Click "Continue" to proceed</p>
	<div style="height: 4000px">spacer</div>
</body></html>"""


@pytest.fixture(scope='module')
def quoted_text_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/quoted').respond_with_data(PAGE, content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url: str) -> None:
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


async def _scroll_to_text(session, text: str) -> None:
	event = session.event_bus.dispatch(ScrollToTextEvent(text=text))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


async def test_scroll_to_quote_free_text_is_found(browser_session, quoted_text_server):
	"""Control: the same page, same mechanism, target text with no quote character."""
	await _goto(browser_session, quoted_text_server.url_for('/quoted'))
	await _scroll_to_text(browser_session, 'Continue to the next step')


async def test_scroll_to_text_containing_a_double_quote_is_found(browser_session, quoted_text_server):
	"""The target text is verbatim page copy; only the `"` in it is different."""
	await _goto(browser_session, quoted_text_server.url_for('/quoted'))

	# Sanity-check the premise: the text really is in the DOM.
	present = await browser_session.run_page_script('return document.body.innerText.includes(\'Click "Continue" to proceed\');')
	assert present.ok, present.error
	assert present.value in (True, 'true'), 'test page does not contain the target text'

	await _scroll_to_text(browser_session, 'Click "Continue" to proceed')


MIXED_QUOTE_PAGE = """<!DOCTYPE html>
<html><head><title>Mixed quotes</title></head>
<body>
	<div style="height: 4000px">spacer</div>
	<p id="mixed">it's a "quote" thing</p>
	<div style="height: 4000px">spacer</div>
</body></html>"""


@pytest.fixture(scope='module')
def mixed_quote_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/mixed').respond_with_data(MIXED_QUOTE_PAGE, content_type='text/html')
	yield server
	server.stop()


async def test_scroll_to_text_containing_both_quote_characters_is_found(browser_session, mixed_quote_server):
	"""Both `'` and `"` present: no single XPath 1.0 literal can hold this, so it needs concat()."""
	await _goto(browser_session, mixed_quote_server.url_for('/mixed'))
	await _scroll_to_text(browser_session, 'it\'s a "quote" thing')


def test_xpath_string_literal_encoding():
	"""The encoder itself: XPath 1.0 literals have no escape syntax, so quotes pick the delimiter."""
	assert _xpath_string_literal('plain') == '"plain"'
	assert _xpath_string_literal('say "hi"') == '\'say "hi"\''
	assert _xpath_string_literal("it's") == '"it\'s"'
	assert _xpath_string_literal('it\'s "x"') == 'concat("it\'s ", \'"\', "x", \'"\')'
	assert _xpath_string_literal('') == '""'
