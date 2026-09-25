"""What the decision model does when the body it gets back is wrong in a 200 OK.

`Jev.ask()` is documented as never raising, and every caller is written on that promise:
a missing answer means "the agent decides", while an exception means the agent stops. The
network failures are covered in `test_decide.py`; the ones here live inside a successful
response — a usage counter that is not a number, a confidence that is not comparable, and
an answer naming a tool that was never on the menu.

The server is a real local one replying with hand-written JSON, because the point is the
literal bytes a well-behaved client still has to survive.
"""

import json

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from browser_use.decide import Decisions, Jev, Noul, choose_tool, parse_decisions
from browser_use.webmcp.views import WebMCPPageTools, WebMCPTool


@pytest.fixture
def jev_server():
	server = HTTPServer()
	server.start()
	yield server
	server.stop()


def _replying(raw_body: str, capture: list | None = None):
	"""A handler that replies with a literal JSON document, well-formed or otherwise."""

	def handler(request: Request) -> Response:
		if capture is not None:
			capture.append(json.loads(request.data))
		return Response(raw_body, content_type='application/json')

	return handler


def _page(*tools: tuple[str, str]) -> WebMCPPageTools:
	return WebMCPPageTools(
		target_id='T1',
		url='https://shop.test/search',
		origin='https://shop.test',
		tools=[
			WebMCPTool(name=name, description=description, source='synthesized', inputSchema={'type': 'object'})
			for name, description in tools
		],
	)


async def test_a_nonsense_usage_block_costs_the_telemetry_not_the_answer(jev_server):
	"""`int('n/a')` raises, and so does `int()` on the infinity that JSON's 1e400 decodes
	to. A billing counter is no reason for an optimization to take down the step it was
	optimizing."""
	jev_server.expect_request('/v1/systemone').respond_with_handler(
		_replying(
			'{"model": "jev-1.13.0",'
			' "answers": {"urgent": {"type": "noul", "noul": 0.91}},'
			' "usage": {"input_tokens": "n/a", "output_tokens": 1e400}}'
		)
	)

	decisions = await Jev(api_key='k', url=jev_server.url_for('/v1/systemone')).ask(
		'payouts are failing', {'urgent': Noul(instructions='Does this convey urgency?')}
	)

	assert decisions.get('urgent').is_true is True, 'the answer was fine; only the counters were not'
	assert (decisions.input_tokens, decisions.output_tokens) == (0, 0)


def test_every_unusable_token_count_reads_as_zero():
	"""A string, a list, a negative, a bool and a non-finite float each raise or lie when
	handed to `int()`. Usage is telemetry, so the floor is zero."""
	for usage in (
		{'input_tokens': 'n/a', 'output_tokens': [1]},
		{'input_tokens': float('inf'), 'output_tokens': float('nan')},
		{'input_tokens': -5, 'output_tokens': True},
		{'input_tokens': None, 'output_tokens': {'n': 1}},
	):
		decisions = parse_decisions({'answers': {}, 'usage': usage})
		assert (decisions.input_tokens, decisions.output_tokens) == (0, 0), usage

	counted = parse_decisions({'usage': {'input_tokens': 296, 'output_tokens': 4.0}})
	assert (counted.input_tokens, counted.output_tokens) == (296, 4)


async def test_a_parse_error_nobody_anticipated_is_still_an_empty_result(jev_server):
	"""The backstop. A JSON integer too large to become a float blows up the probability
	conversion, which is exactly the class of surprise the never-raises contract is for."""
	huge = '1' + '0' * 400
	jev_server.expect_request('/v1/systemone').respond_with_handler(
		_replying('{"answers": {"t": {"type": "choice", "choice": "search", "probabilities": {"search": %s}}}}' % huge)
	)

	decisions = await Jev(api_key='k', url=jev_server.url_for('/v1/systemone')).ask('state', {'t': Noul(instructions='?')})

	assert decisions == Decisions(), 'an unparseable body is a missing answer, not an exception'


def test_a_confidence_that_is_not_a_real_number_is_dropped():
	"""inf clears every threshold and nan clears none, so either one turns `certain()`
	into a verdict the model never gave."""
	for bogus in (float('inf'), float('-inf'), float('nan'), 'high', True, None):
		answer = parse_decisions({'answers': {'t': {'type': 'choice', 'choice': 'search', 'confidence': bogus}}}).get('t')
		assert answer is not None and answer.confidence is None, bogus
		assert not answer.certain(), f'{bogus!r} must not read as certain'

	assert parse_decisions({'answers': {'t': {'type': 'choice', 'choice': 'search', 'confidence': 0.9}}}).get('t').certain()


async def test_a_tool_that_was_never_offered_is_not_a_pick(jev_server):
	"""A choice only means something among the options it was offered. A confident answer
	naming anything else is a tool this page does not have, and it has to read as "you
	decide", exactly like the escape hatch does."""
	sent: list = []
	jev_server.expect_request('/v1/systemone').respond_with_handler(
		_replying('{"answers": {"tool": {"type": "choice", "choice": "not_offered", "confidence": 0.99}}}', sent)
	)
	page = _page(('search', 'search the catalogue'), ('next_page', 'go to the next page'))

	answer = await choose_tool(Jev(api_key='k', url=jev_server.url_for('/v1/systemone')), page, 'find a blue shirt')

	assert set(sent[0]['questions']['tool']['criteria']) == {'search', 'next_page', 'none_of_these'}
	assert answer is None, 'a name that was never on the menu is not an answer, however confident'


async def test_an_offered_tool_still_comes_back(jev_server):
	"""The membership check must not eat the picks it was meant to let through."""
	jev_server.expect_request('/v1/systemone').respond_with_handler(
		_replying('{"answers": {"tool": {"type": "choice", "choice": "next_page", "confidence": 0.93}}}')
	)
	page = _page(('search', 'search the catalogue'), ('next_page', 'go to the next page'))

	answer = await choose_tool(Jev(api_key='k', url=jev_server.url_for('/v1/systemone')), page, 'see more results')

	assert answer is not None and answer.value == 'next_page'
