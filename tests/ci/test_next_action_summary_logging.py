"""Regression test: Agent._log_next_action_summary() must actually log.

The method's docstring promises "Log a comprehensive summary of the next
action(s)": it builds a per-action `name(params...)` string for every action
in the model's response, but silently threw the result away instead of
calling `self.logger.debug(...)` with it - a no-op that contradicts its own
stated contract. Operators enabling DEBUG logging to see what the agent is
about to do got nothing from this call site (distinct from `log_response()`,
which logs thinking/eval/memory/next_goal but never the action name or its
parameters).

Note on capture: `browser_use.logging_config.setup_logging()` sets
`propagate = False` on the 'browser_use' logger (see logging_config.py), so
pytest's `caplog` - which only attaches its handler to the root logger -
never sees records from 'browser_use.Agent...' child loggers. Every test
here attaches a handler directly to the 'browser_use' logger instead, which
is where those records actually surface.

Drives a real Agent.step() against a real BrowserSession + pytest-httpserver
page, with only the LLM stubbed, and asserts the summary line appears in the
captured logs with the action name and its key parameters.
"""

import logging
from contextlib import contextmanager

from browser_use.agent.service import Agent
from tests.ci.conftest import create_mock_llm


class _CollectingHandler(logging.Handler):
	def __init__(self):
		super().__init__()
		self.messages: list[str] = []

	def emit(self, record: logging.LogRecord) -> None:
		self.messages.append(record.getMessage())


@contextmanager
def capture_browser_use_logs(level: int = logging.DEBUG):
	"""Capture records from the 'browser_use' logger tree directly.

	Required because 'browser_use' has propagate=False once setup_logging()
	has run, which makes pytest's caplog (root-attached) blind to it.
	"""
	logger = logging.getLogger('browser_use')
	handler = _CollectingHandler()
	handler.setLevel(level)
	prev_level = logger.level
	logger.addHandler(handler)
	logger.setLevel(level)
	try:
		yield handler.messages
	finally:
		logger.removeHandler(handler)
		logger.setLevel(prev_level)


async def test_next_action_summary_is_logged_after_step(browser_session, httpserver):
	"""A real step() with a real page must emit the '📝 Next action(s): ...' debug line."""
	httpserver.expect_request('/summary_log_page').respond_with_data(
		'<html><head><title>Summary Log Page</title></head><body><h1>hi</h1></body></html>',
		content_type='text/html',
	)
	base_url = f'http://{httpserver.host}:{httpserver.port}/summary_log_page'

	mock_llm = create_mock_llm(actions=['{"action": [{"done": {"text": "finished the task", "success": true}}]}'])
	agent = Agent(task='Say hi', llm=mock_llm, browser_session=browser_session)

	await agent.browser_session.navigate_to(base_url)

	with capture_browser_use_logs() as messages:
		await agent.step()

	summary_records = [m for m in messages if 'Next action(s)' in m]
	assert summary_records, f'Expected a "Next action(s)" debug summary after step(), found none. All records: {messages}'
	# The chosen action ("done") and one of its key parameters must be visible in the summary,
	# not just swallowed into an unused local variable.
	assert 'done' in summary_records[0]
	assert 'success=True' in summary_records[0]


async def test_log_next_action_summary_emits_action_details_directly(browser_session):
	"""Unit-level check on the helper itself, isolated from the rest of step()."""
	mock_llm = create_mock_llm()
	agent = Agent(task='test', llm=mock_llm, browser_session=browser_session)

	parsed = agent.AgentOutput.model_validate(
		{
			'evaluation_previous_goal': 'n/a',
			'memory': 'n/a',
			'next_goal': 'n/a',
			'action': [{'done': {'text': 'a result', 'success': True}}],
		}
	)

	with capture_browser_use_logs() as messages:
		agent._log_next_action_summary(parsed)

	summary_records = [m for m in messages if 'Next action(s)' in m]
	assert summary_records, 'Building action_details without logging it is the bug under test.'
	assert 'done' in summary_records[0]
	assert 'text="a result"' in summary_records[0]
	assert 'success=True' in summary_records[0]


async def test_log_next_action_summary_is_noop_below_debug_level(browser_session):
	"""No action summary should be emitted when DEBUG logging isn't enabled (matches the early return)."""
	mock_llm = create_mock_llm()
	agent = Agent(task='test', llm=mock_llm, browser_session=browser_session)

	parsed = agent.AgentOutput.model_validate(
		{
			'evaluation_previous_goal': 'n/a',
			'memory': 'n/a',
			'next_goal': 'n/a',
			'action': [{'done': {'text': 'a result', 'success': True}}],
		}
	)

	with capture_browser_use_logs(level=logging.INFO) as messages:
		agent._log_next_action_summary(parsed)

	assert not any('Next action(s)' in m for m in messages)
