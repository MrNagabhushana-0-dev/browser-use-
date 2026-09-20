"""Workflow memory: reuse the route that already worked on a site.

Agent Workflow Memory (ICML 2025, arXiv:2409.07429) reports +24.6% to +51.1% relative
success on Mind2Web from replaying induced routines. These tests pin the three properties
that make that safe to ship: what gets remembered, what must never be remembered, and
what gets recalled.

Histories here are real AgentHistoryList objects built from real action models — the same
types the agent produces — not stand-ins.
"""

import json
from collections.abc import Sequence

import pytest

from browser_use.agent.views import AgentHistory, AgentHistoryList, AgentOutput
from browser_use.browser.views import BrowserStateHistory
from browser_use.dom.views import DOMInteractedElement, NodeType
from browser_use.memory import WorkflowMemory, domain_of
from browser_use.memory.views import MAX_STEPS_PER_WORKFLOW
from browser_use.tools.service import Tools


@pytest.fixture
def memory(tmp_path):
	return WorkflowMemory(path=tmp_path / 'workflows.json')


@pytest.fixture(scope='module')
def action_model():
	"""Builds the real per-action ActionModel the agent emits, from the real registry.

	create_action_model() returns a RootModel union when given several actions, which
	AgentOutput.action rejects; asked for one action it returns that action's own
	ActionModel subclass, which is what the agent actually puts in its output.
	"""
	registry = Tools().registry

	def build(payload: dict):
		name = next(iter(payload))
		return registry.create_action_model(include_actions=[name]).model_validate(payload)

	return build


def _element(**attributes) -> DOMInteractedElement:
	return DOMInteractedElement(
		node_id=1,
		backend_node_id=1,
		frame_id=None,
		node_type=NodeType.ELEMENT_NODE,
		node_value='',
		node_name='input',
		attributes=attributes,
		bounds=None,
		x_path='/html/body/input',
		element_hash=0,
	)


def _history(action_model, steps: Sequence[tuple[dict, DOMInteractedElement | None]], url: str, success: bool = True):
	"""Build a real AgentHistoryList from (action, interacted element) pairs."""
	items = []
	for action_payload, element in steps:
		action = action_model(action_payload)
		items.append(
			AgentHistory(
				model_output=AgentOutput(action=[action]),
				result=[],
				state=BrowserStateHistory(url=url, title='t', tabs=[], interacted_element=[element]),
			)
		)
	# The agent signals outcome through the final done action's result.
	from browser_use.agent.views import ActionResult

	done = action_model({'done': {'text': 'finished', 'success': success}})
	items.append(
		AgentHistory(
			model_output=AgentOutput(action=[done]),
			result=[ActionResult(is_done=True, success=success)],
			state=BrowserStateHistory(url=url, title='t', tabs=[], interacted_element=[None]),
		)
	)
	return AgentHistoryList(history=items)


def test_domain_is_the_memory_key():
	assert domain_of('https://Shop.Example.com/cart?x=1') == 'shop.example.com'
	assert domain_of('about:blank') == ''
	assert domain_of(None) == ''


def test_a_successful_run_is_remembered_as_a_route(memory, action_model):
	history = _history(
		action_model,
		[
			({'navigate': {'url': 'https://shop.example.com/catalog'}}, None),
			({'click': {'index': 7}}, _element(**{'aria-label': 'Add to cart'})),
		],
		url='https://shop.example.com/catalog',
	)

	workflow = memory.record('buy running socks', history)
	assert workflow is not None
	assert workflow.domain == 'shop.example.com'
	assert [step.action for step in workflow.steps] == ['navigate', 'click']
	# The element is remembered by its label, never by index: indices are assigned per
	# snapshot and mean nothing on the next visit.
	assert workflow.steps[1].detail == 'Add to cart'
	assert '7' not in workflow.render()


def test_typed_values_are_never_written_to_disk(memory, action_model):
	"""A trajectory through a login form must not leave credentials in a JSON file."""
	history = _history(
		action_model,
		[
			({'navigate': {'url': 'https://bank.example.com/login'}}, None),
			({'input': {'index': 2, 'text': 'hunter2-my-real-password'}}, _element(name='password', type='password')),
		],
		url='https://bank.example.com/login',
	)

	workflow = memory.record('log in', history)
	assert workflow is not None

	# The step survives — knowing a password field must be filled is useful.
	assert workflow.steps[1].action == 'input'
	assert 'password' in workflow.steps[1].detail

	# The value does not, in the object or on disk.
	assert 'hunter2' not in workflow.render()
	assert 'hunter2' not in memory.path.read_text()


def test_a_failed_run_is_not_remembered(memory, action_model):
	"""Replaying a route that did not work would actively mislead the next attempt."""
	history = _history(
		action_model,
		[({'navigate': {'url': 'https://shop.example.com/'}}, None)],
		url='https://shop.example.com/',
		success=False,
	)
	assert memory.record('buy socks', history) is None
	assert memory.workflows == []


def test_recall_is_scoped_to_the_site_and_ranked_by_task(memory, action_model):
	for task, url in [
		('buy running socks', 'https://shop.example.com/a'),
		('check my order status', 'https://shop.example.com/b'),
		('read the news', 'https://news.example.org/c'),
	]:
		memory.record(task, _history(action_model, [({'navigate': {'url': url}}, None)], url=url))

	hits = memory.recall('buy warm socks', 'https://shop.example.com/anything')
	assert hits, 'a same-site route should be recalled'
	assert hits[0].task == 'buy running socks', 'the closest task should rank first'
	assert all(h.domain == 'shop.example.com' for h in hits), 'another site is never relevant'

	assert memory.recall('buy socks', 'https://unknown.example.net/') == []


def test_the_prompt_block_is_empty_when_nothing_is_remembered(memory):
	assert memory.describe('buy socks', 'https://shop.example.com/') == ''


def test_the_prompt_block_tells_the_model_it_may_be_stale(memory, action_model):
	url = 'https://shop.example.com/catalog'
	memory.record('buy socks', _history(action_model, [({'navigate': {'url': url}}, None)], url=url))

	block = memory.describe('buy socks', url)
	assert 'done this before' in block
	assert 'shop.example.com' in block
	# A remembered route is a hint, not an instruction: sites change.
	assert 'ignore it when the page has changed' in block


def test_memory_survives_a_restart(memory, action_model, tmp_path):
	url = 'https://shop.example.com/catalog'
	memory.record('buy socks', _history(action_model, [({'navigate': {'url': url}}, None)], url=url))

	reopened = WorkflowMemory(path=tmp_path / 'workflows.json')
	assert [w.task for w in reopened.workflows] == ['buy socks']


def test_a_corrupt_store_degrades_instead_of_breaking_the_run(tmp_path):
	"""Memory is an optimization; losing it must never stop an agent."""
	path = tmp_path / 'workflows.json'
	path.write_text('{not json at all')
	assert WorkflowMemory(path=path).workflows == []

	path.write_text(json.dumps([{'garbage': True}, {'also': 'wrong'}]))
	assert WorkflowMemory(path=path).workflows == []


def test_long_runs_are_capped(memory, action_model):
	"""A 60-step trajectory must not become a 60-line prompt block."""
	url = 'https://shop.example.com/x'
	steps = [({'click': {'index': i}}, _element(**{'aria-label': f'Item {i}'})) for i in range(1, 61)]
	workflow = memory.record('click everything', _history(action_model, steps, url=url))
	assert workflow is not None
	assert len(workflow.steps) == MAX_STEPS_PER_WORKFLOW


def test_memory_can_be_turned_off(tmp_path, action_model):
	off = WorkflowMemory(path=tmp_path / 'workflows.json', enabled=False)
	url = 'https://shop.example.com/x'
	assert off.record('buy socks', _history(action_model, [({'navigate': {'url': url}}, None)], url=url)) is None
	assert off.recall('buy socks', url) == []
	assert not (tmp_path / 'workflows.json').exists()
