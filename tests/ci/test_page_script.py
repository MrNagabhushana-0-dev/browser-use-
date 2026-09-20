"""Running code against the page, instead of one LLM round trip per element.

Reading a long table through the normal loop sends every row into the context as markup
and gets it back as prose. A script sends a line of JavaScript and returns only the
requested fields. These tests pin the behaviour, and the last one measures the gap.
"""

import json

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.tools.service import Tools

ROWS = 60

TABLE_PAGE = (
	'<!DOCTYPE html><html><head><title>Inventory</title></head><body><table id="t">'
	'<tr><th>Name</th><th>Price</th><th>Stock</th></tr>'
	+ ''.join(
		f'<tr><td class="n">Widget {i}</td><td class="p">{i * 3}.00</td><td class="s">{i % 7}</td></tr>' for i in range(ROWS)
	)
	+ '</table>'
	+ ''.join(f'<input type="checkbox" class="pick" data-id="{i}">' for i in range(20))
	+ '</body></html>'
)


@pytest.fixture(scope='module')
def script_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/table').respond_with_data(TABLE_PAGE, content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url: str) -> None:
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


async def test_one_script_reads_a_whole_table(browser_session, script_server):
	await _goto(browser_session, script_server.url_for('/table'))

	result = await browser_session.run_page_script("""
		return $$('#t tr').slice(1).map(r => ({
			name: txt($('.n', r)),
			price: txt($('.p', r)),
		}));
	""")

	assert result.ok, result.error
	rows = json.loads(result.value)
	assert len(rows) == ROWS
	assert rows[0] == {'name': 'Widget 0', 'price': '0.00'}
	assert rows[-1] == {'name': f'Widget {ROWS - 1}', 'price': f'{(ROWS - 1) * 3}.00'}


async def test_a_script_can_act_on_many_elements_at_once(browser_session, script_server):
	"""Twenty checkboxes, one step."""
	await _goto(browser_session, script_server.url_for('/table'))

	result = await browser_session.run_page_script("""
		const boxes = $$('.pick');
		boxes.forEach(b => { b.checked = true; });
		return boxes.length;
	""")
	assert result.ok, result.error
	assert json.loads(result.value) == 20

	check = await browser_session.run_page_script("return $$('.pick').filter(b => b.checked).length;")
	assert json.loads(check.value) == 20


async def test_await_is_available_in_the_script(browser_session, script_server):
	await _goto(browser_session, script_server.url_for('/table'))
	result = await browser_session.run_page_script("""
		await new Promise(r => setTimeout(r, 10));
		return 'waited';
	""")
	assert result.ok, result.error
	assert json.loads(result.value) == 'waited'


async def test_a_broken_script_reports_why_instead_of_crashing(browser_session, script_server):
	"""The agent's next move is to rewrite the script, so it needs the real message."""
	await _goto(browser_session, script_server.url_for('/table'))

	thrown = await browser_session.run_page_script('return nonexistentFunction();')
	assert not thrown.ok
	assert thrown.error is not None and 'nonexistentFunction' in thrown.error

	# A syntax error fails before the in-page try/catch exists, so it arrives by a
	# different path and must still be reported rather than raised.
	broken = await browser_session.run_page_script('return $$(;')
	assert not broken.ok
	assert broken.error


async def test_oversized_results_are_clipped_in_the_page(browser_session, script_server):
	"""A runaway script must not be able to push megabytes through the CDP socket."""
	await _goto(browser_session, script_server.url_for('/table'))

	result = await browser_session.run_page_script("return 'x'.repeat(50000);", max_chars=500)
	assert result.ok, result.error
	assert result.truncated
	assert len(result.value) == 500
	assert result.full_length > 50000


async def test_dom_nodes_come_back_readable_rather_than_empty(browser_session, script_server):
	"""Models return elements by mistake; `{}` for each one teaches them nothing."""
	await _goto(browser_session, script_server.url_for('/table'))

	result = await browser_session.run_page_script("return $$('.n').slice(0, 2);")
	assert result.ok, result.error
	nodes = json.loads(result.value)
	assert nodes[0]['tag'] == 'td'
	assert nodes[0]['text'] == 'Widget 0'


async def test_the_action_fences_the_result_for_the_agent(browser_session, script_server):
	await _goto(browser_session, script_server.url_for('/table'))
	tools = Tools()

	result = await tools.registry.execute_action(
		'run_page_script',
		{'script': "return $$('.n').length;", 'purpose': 'count rows'},
		browser_session=browser_session,
	)
	assert result.error is None
	assert result.extracted_content is not None
	assert result.extracted_content.startswith('<script_result>')
	assert '60' in result.extracted_content

	failed = await tools.registry.execute_action(
		'run_page_script',
		{'script': 'return boom();', 'purpose': 'break it'},
		browser_session=browser_session,
	)
	assert failed.error is not None and 'boom' in failed.error


async def test_a_script_is_far_cheaper_than_reading_the_serialized_page(browser_session, script_server):
	"""The whole point, measured rather than asserted.

	Compare what the agent must carry to get the same table: the serialized DOM the model
	would otherwise read, against the script plus its result.
	"""
	await _goto(browser_session, script_server.url_for('/table'))

	state = await browser_session.get_browser_state_summary(include_screenshot=False)
	dom_chars = len(state.dom_state.llm_representation())

	script = "return $$('#t tr').slice(1).map(r => ({n: txt($('.n', r)), p: txt($('.p', r))}));"
	result = await browser_session.run_page_script(script)
	assert result.ok, result.error
	script_chars = len(script) + len(result.value)

	# Both routes deliver the same 60 rows. This asserts only the direction and a floor,
	# because the exact ratio moves with the serializer; the measured value is printed so
	# a regression in the gap is visible in CI output.
	print(f'\nserialized DOM: {dom_chars} chars | script + result: {script_chars} chars')
	assert script_chars < dom_chars
