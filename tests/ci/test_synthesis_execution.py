"""Running a synthesized tool against a page that did not cooperate.

Two failures that both report success. A locator the scanner happily recorded but the
resolver cannot find makes a tool that says 'could not find' forever and can never be
verified. A fill step that types into a field without clearing it appends to what is
already there, and then marks the tool verified for having done it.
"""

import json

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.synthesis import SiteToolSynthesizer
from browser_use.synthesis.store import ManifestStore

# The only text field carries data-test and nothing else: no id, no label, no placeholder,
# no name. The scanner records data-test as the testid and, having one, computes no CSS
# fallback — so the resolver's testid branch is the only way back to this element.
TESTID_PAGE = """<!DOCTYPE html>
<html><head><title>Testid Form</title></head><body>
	<form id="lookup">
		<input type="text" data-test="city">
		<button type="submit">Look up</button>
	</form>
<script>
	window.__submitted = null;
	document.getElementById('lookup').addEventListener('submit', (e) => {
		e.preventDefault();
		window.__submitted = document.querySelector('[data-test="city"]').value;
	});
</script>
</body></html>"""

# A search box that already holds something, which is the normal state of a search box a
# person has used once.
PREFILLED_PAGE = """<!DOCTYPE html>
<html><head><title>Prefilled Search</title></head><body>
	<form id="searchform">
		<input type="search" id="q" aria-label="Site search" value="old">
		<button type="submit">Search</button>
	</form>
<script>
	window.__submitted = null;
	document.getElementById('searchform').addEventListener('submit', (e) => {
		e.preventDefault();
		window.__submitted = document.getElementById('q').value;
	});
</script>
</body></html>"""


@pytest.fixture(scope='module')
def execution_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/testid').respond_with_data(TESTID_PAGE, content_type='text/html')
	server.expect_request('/prefilled').respond_with_data(PREFILLED_PAGE, content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url):
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


async def _submitted(session) -> str | None:
	out = await session.run_page_script('return window.__submitted;')
	return json.loads(out.value)


async def _field_value(session, selector: str) -> str:
	out = await session.run_page_script(f'return document.querySelector({json.dumps(selector)}).value;')
	return json.loads(out.value)


def _synth(session, tmp_path) -> SiteToolSynthesizer:
	"""A synthesizer whose persistent cache is this test's own, not the user's."""
	return SiteToolSynthesizer(session, store=ManifestStore(path=tmp_path / 'site_tools.json', enabled=True))


async def test_a_field_named_only_by_data_test_can_actually_be_driven(browser_session, execution_server, tmp_path):
	"""The scanner reads three test-id attributes; the resolver used to try two of them."""
	await _goto(browser_session, execution_server.url_for('/testid'))
	synth = _synth(browser_session, tmp_path)
	manifest = await synth.synthesize()

	tool = next((t for t in manifest.tools if any(s.action == 'fill' for s in t.steps)), None)
	assert tool is not None, f'no tool with a fill step among {[t.name for t in manifest.tools]}'
	fill = next(s for s in tool.steps if s.action == 'fill')
	assert fill.locator.testid == 'city', 'the scanner records data-test as the testid'
	assert fill.locator.css is None, 'and computes no CSS fallback once it has one'

	box = await synth._locate(fill.locator)
	assert box is not None, 'a locator the scanner recorded must resolve, or its tool never runs'

	ok, message = await synth.call(tool, {fill.param or 'value': 'Lisbon'})
	assert ok, message
	assert await _submitted(browser_session) == 'Lisbon'
	assert tool.verified is True, 'it ran end to end, so it is no longer an inference'


async def test_filling_a_prefilled_field_replaces_it_rather_than_appending(browser_session, execution_server, tmp_path):
	"""Typing is real key input, so it inserts at the caret. Without selecting what is
	already there, fill('new') over 'old' submits 'oldnew' — and still reports ok."""
	await _goto(browser_session, execution_server.url_for('/prefilled'))
	synth = _synth(browser_session, tmp_path)
	manifest = await synth.synthesize()

	tool = manifest.get('search')
	assert tool is not None, f'no search tool among {[t.name for t in manifest.tools]}'
	assert await _field_value(browser_session, '#q') == 'old', 'the box must start with something in it'

	ok, message = await synth.call(tool, {'query': 'new'})
	assert ok, message
	assert await _submitted(browser_session) == 'new', 'the old contents were kept instead of replaced'
