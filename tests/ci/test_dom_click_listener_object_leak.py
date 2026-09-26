"""Regression test for a CDP remote-object leak in JS click-listener detection.

`DomService._get_all_trees` resolves elements with JS click listeners by running
`Runtime.evaluate` (returning an array of element references), then
`Runtime.getProperties` to pull out each element's own remote object handle. Only the
array's own handle was ever released (`Runtime.releaseObject` on `result_object_id`);
every per-element handle `getProperties` resolved from it was left live in the
renderer's inspector backend for the rest of the CDP session. `Runtime.getProperties`
documents that "[the] object group of the result is inherited from the target
object" -- so tagging the array's `Runtime.evaluate` call with `objectGroup` and
releasing that whole group afterwards (instead of releasing only the array's own
objectId) frees every handle it produced, however many elements were resolved.

This is a real, unbounded leak: on any page with JS click listeners, every single
`get_dom_tree` call (i.e. every agent step) pins more remote object handles that are
never reclaimed for the life of the target's CDP session.

We prove it by capturing one of the per-element object ids `Runtime.getProperties`
actually resolves (a real CDP handle, not a fake one -- we only observe the real
call, we don't alter its behavior or result) and then trying to release it a second
time ourselves. CDP refuses to release an id that has already been released or was
never known ("Could not find object with given id"). So:
  - unfixed: the id was never released internally -> our release call succeeds.
  - fixed: the id was already released via the object group -> our call raises.
"""

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.dom.service import DomService

PAGE = """<!DOCTYPE html>
<html><head><title>Buttons with click listeners</title></head>
<body>
	<button id="btn-0">Zero</button>
	<button id="btn-1">One</button>
	<button id="btn-2">Two</button>
	<script>
		for (const btn of document.querySelectorAll('button')) {
			btn.addEventListener('click', () => {});
		}
	</script>
</body></html>"""


@pytest.fixture(scope='module')
def http_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/buttons').respond_with_data(PAGE, content_type='text/html')
	yield server
	server.stop()


async def test_click_listener_element_handles_are_released(browser_session, http_server):
	event = browser_session.event_bus.dispatch(NavigateToUrlEvent(url=http_server.url_for('/buttons')))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)

	target_id = browser_session.agent_focus_target_id
	assert target_id is not None
	cdp_session = await browser_session.get_or_create_cdp_session(target_id=target_id, focus=False)

	captured_element_object_ids: list[str] = []
	original_get_properties = cdp_session.cdp_client.send.Runtime.getProperties

	async def spying_get_properties(params, session_id=None):
		# Pass-through spy: performs the real CDP call and returns its real result
		# unmodified, only recording the per-element object ids it resolved so we can
		# probe their lifetime afterwards.
		result = await original_get_properties(params, session_id=session_id)
		for prop in result.get('result', []):
			value = prop.get('value') if isinstance(prop, dict) else None
			object_id = value.get('objectId') if isinstance(value, dict) else None
			if object_id:
				captured_element_object_ids.append(object_id)
		return result

	cdp_session.cdp_client.send.Runtime.getProperties = spying_get_properties
	try:
		dom_service = DomService(browser_session)
		trees = await dom_service._get_all_trees(target_id)
	finally:
		cdp_session.cdp_client.send.Runtime.getProperties = original_get_properties

	# Sanity check: the detection path actually ran and resolved our buttons, otherwise
	# this test would pass vacuously.
	assert len(trees.js_click_listener_backend_ids or []) == 3
	assert len(captured_element_object_ids) == 3

	leaked_ids: list[str] = []
	for object_id in captured_element_object_ids:
		try:
			await cdp_session.cdp_client.send.Runtime.releaseObject(
				params={'objectId': object_id},
				session_id=cdp_session.session_id,
			)
		except RuntimeError:
			# CDP refused the release ("Could not find object with given id") --
			# already released internally, so this handle did not leak.
			continue
		leaked_ids.append(object_id)

	assert not leaked_ids, (
		f'{len(leaked_ids)}/{len(captured_element_object_ids)} click-listener element handles were '
		'never released and are still resolvable via CDP -- they leaked in the inspector backend'
	)
