"""Regression test: evaluate() must not corrupt valid JavaScript that contains
a properly backslash-escaped quote inside a double-quoted string literal.

`Tools._validate_and_fix_javascript()` runs on every `evaluate()` call and its
"Pattern 1" step blindly replaces every `\"` occurrence in the submitted code
with a bare `"`, regardless of whether that escaped quote is a genuine,
correctly-escaped quote inside a string (e.g. `"She said \"hi\""`). Stripping
the backslash there turns valid JS into a syntax error, so code the agent
wrote correctly fails for a reason it cannot diagnose or fix by retrying.

See browser_use/tools/service.py `_validate_and_fix_javascript()`.
"""

import pytest
from pytest_httpserver import HTTPServer

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools


@pytest.fixture(scope='session')
def http_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/blank').respond_with_data(
		'<!DOCTYPE html><html><body><p>hello</p></body></html>',
		content_type='text/html',
	)
	yield server
	server.stop()


@pytest.fixture(scope='session')
def base_url(http_server):
	return f'http://{http_server.host}:{http_server.port}'


@pytest.fixture(scope='module')
async def browser_session(base_url):
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,
			keep_alive=True,
		)
	)
	await session.start()
	yield session
	await session.kill()


@pytest.fixture(scope='function')
def tools():
	return Tools()


class TestEvaluateDoesNotCorruptEscapedQuotes:
	async def test_string_with_escaped_inner_quotes_is_returned_unmangled(self, tools, browser_session, base_url):
		"""A JS string literal that itself contains an escaped double-quote is
		perfectly valid JavaScript and must evaluate to the literal text,
		including the inner quote characters -- not be turned into a syntax
		error by the "helpful" quote-fixing heuristic.
		"""
		await tools.navigate(url=f'{base_url}/blank', new_tab=False, browser_session=browser_session)

		code = '(function(){ return "She said \\"hello\\" to me"; })()'

		result = await tools.evaluate(code=code, browser_session=browser_session)

		assert isinstance(result, ActionResult)
		assert result.error is None, f'evaluate() should succeed on valid JS, got error: {result.error}'
		assert result.extracted_content == 'She said "hello" to me'

	async def test_selector_with_escaped_quote_still_finds_the_element(self, tools, browser_session, base_url):
		"""A querySelector call whose CSS string argument needs an escaped quote
		(e.g. matching an attribute value that itself contains a quote) must
		keep working.
		"""
		await tools.navigate(url=f'{base_url}/blank', new_tab=False, browser_session=browser_session)

		code = (
			'(function(){ '
			'var d = document.createElement("div"); '
			'd.setAttribute("data-note", \'she said "hi"\'); '
			'document.body.appendChild(d); '
			'return "She said \\"hi\\" again"; '
			'})()'
		)

		result = await tools.evaluate(code=code, browser_session=browser_session)

		assert isinstance(result, ActionResult)
		assert result.error is None, f'evaluate() should succeed on valid JS, got error: {result.error}'
		assert result.extracted_content == 'She said "hi" again'
