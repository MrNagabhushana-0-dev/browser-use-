"""Two drivers, one browser, one wheel.

Co-browsing without arbitration is worse than none: the agent clicks while you are
mid-password, you scroll while it measures an element, and both of you conclude the page
is broken. These tests pin that the agent stops when you take over, that it can still
look and report while paused, and that the refusal tells it what is going on.
"""

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.tools.service import Tools

PAGE = """<!DOCTYPE html><html><head><title>Shared</title></head><body>
<button id="b" style="position:absolute;left:40px;top:60px;width:120px;height:40px;">Press</button>
<div id="count">0</div>
<script>
	let n = 0;
	document.getElementById('b').addEventListener('click', () => {
		document.getElementById('count').textContent = String(++n);
	});
</script></body></html>"""


@pytest.fixture(scope='module')
def shared_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/shared').respond_with_data(PAGE, content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url):
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


def test_the_agent_drives_by_default():
	"""Single-driver use must be unchanged by any of this."""
	from browser_use.cobrowse.control import ControlLock

	lock = ControlLock()
	assert lock.holder == 'agent'
	assert lock.agent_may_act is True


async def test_the_agent_stops_acting_when_you_take_over(browser_session, shared_server):
	await _goto(browser_session, shared_server.url_for('/shared'))
	tools = Tools()

	browser_session.control.grant_to_human('filling in my password')

	result = await tools.registry.execute_action(
		'run_page_script',
		{'script': "document.getElementById('b').click(); return 1;", 'purpose': 'sneak a click in'},
		browser_session=browser_session,
	)
	assert result.error is not None
	# The message has to say who has it and that it is temporary, or a model reads a bare
	# failure as a broken page and starts inventing workarounds.
	assert 'the person is driving' in result.error
	assert 'filling in my password' in result.error

	# And the page really was not touched.
	count = await browser_session.run_page_script("return document.getElementById('count').textContent;")
	assert count.value == '"0"'


async def test_the_agent_can_still_look_while_you_drive(browser_session, shared_server):
	"""Paused is not broken: it must still be able to read, watch and finish."""
	await _goto(browser_session, shared_server.url_for('/shared'))
	tools = Tools()
	browser_session.control.grant_to_human()

	watched = await tools.registry.execute_action(
		'watch_page', {'seconds': 1.0, 'reason': 'see what the person is doing'}, browser_session=browser_session
	)
	assert watched.error is None


async def test_control_comes_back(browser_session, shared_server):
	await _goto(browser_session, shared_server.url_for('/shared'))
	tools = Tools()

	browser_session.control.grant_to_human()
	browser_session.control.grant_to_agent('you finished typing')

	result = await tools.registry.execute_action(
		'run_page_script',
		{'script': "document.getElementById('b').click(); return 1;", 'purpose': 'press the button'},
		browser_session=browser_session,
	)
	assert result.error is None
	count = await browser_session.run_page_script("return document.getElementById('count').textContent;")
	assert count.value == '"1"'


def test_the_handover_summary_says_what_the_agent_did():
	"""What you need before taking the wheel back after being away."""
	from browser_use.cobrowse.control import ControlLock

	lock = ControlLock()
	lock.record('opened the orders page')
	lock.record('filtered to last 30 days')
	lock.grant_to_human('I want to check something')

	summary = lock.summary()
	assert 'Control is with the human' in summary
	assert 'I want to check something' in summary
	assert 'filtered to last 30 days' in summary


async def test_the_agent_logs_what_it_did_for_whoever_takes_over(browser_session, shared_server):
	"""The log existed but nothing wrote to it, which made handover guesswork."""
	await _goto(browser_session, shared_server.url_for('/shared'))
	tools = Tools()

	await tools.registry.execute_action(
		'run_page_script',
		{'script': "document.getElementById('b').click(); return 1;", 'purpose': 'press the button once'},
		browser_session=browser_session,
	)
	await tools.registry.execute_action(
		'watch_page', {'seconds': 1.0, 'reason': 'confirm the counter moved'}, browser_session=browser_session
	)

	recent = browser_session.control.recent()
	assert len(recent) >= 2, f'nothing was recorded: {recent}'
	assert any('press the button once' in line for line in recent)
	assert any('confirm the counter moved' in line for line in recent)

	summary = browser_session.control.summary()
	assert 'Last' in summary and 'press the button once' in summary


async def test_a_failed_action_says_so_in_the_log(browser_session, shared_server):
	await _goto(browser_session, shared_server.url_for('/shared'))
	tools = Tools()

	await tools.registry.execute_action(
		'run_page_script',
		{'script': 'return definitelyNotDefined();', 'purpose': 'break something'},
		browser_session=browser_session,
	)

	recent = browser_session.control.recent()
	assert any('failed' in line for line in recent), f'a failure was logged as a success: {recent}'


async def test_the_log_does_not_transcribe_what_was_typed(browser_session, shared_server):
	"""The log is shown to a person on handover; it is not a place for form contents."""
	await _goto(browser_session, shared_server.url_for('/shared'))
	tools = Tools()

	await tools.registry.execute_action(
		'run_page_script',
		{'script': "return 'ok';", 'purpose': 'a' * 400},
		browser_session=browser_session,
	)

	line = browser_session.control.recent()[-1]
	assert len(line) < 160, f'the log entry is a dump, not a summary: {len(line)} chars'
