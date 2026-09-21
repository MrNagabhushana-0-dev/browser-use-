"""Asking a decision model, and surviving it.

The happy path is the least interesting part. A decision model is an optimization the
agent has to be able to lose — no key, no network, a timeout, a body that does not parse,
an answer the model was not sure about — so most of what is asserted here is that each of
those degrades to "you decide" rather than to an exception or, worse, a confident default.

The server is a real local one speaking the documented schema, not a mock. The live
endpoint is not exercised: this environment has no TypeSafe key, so what is verified is
the request this library builds and how it handles what comes back.
"""

import json

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from browser_use.decide import Choice, Decisions, Jev, Noul, Score, choose_tool, page_state, parse_decisions, triage_page
from browser_use.decide.views import MAX_QUESTIONS, MAX_STATE_CHARS
from browser_use.webmcp.views import WebMCPPageTools, WebMCPTool


@pytest.fixture
def jev_server():
	server = HTTPServer()
	server.start()
	yield server
	server.stop()


def _answering(body: dict, capture: list | None = None):
	"""A handler that records the request and replies with a fixed body."""

	def handler(request: Request) -> Response:
		if capture is not None:
			capture.append(json.loads(request.data))
		return Response(json.dumps(body), content_type='application/json')

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


async def test_a_typed_question_goes_out_in_the_documented_shape(jev_server):
	"""The request is the part this library owns; the schema is the part it has to match."""
	sent: list = []
	jev_server.expect_request('/v1/systemone').respond_with_handler(
		_answering({'model': 'jev-1.13.0', 'answers': {'urgent': {'type': 'noul', 'noul': 0.95}}}, sent)
	)
	jev = Jev(api_key='k', url=jev_server.url_for('/v1/systemone'))

	decisions = await jev.ask(
		'payouts have been failing for three days',
		{'urgent': Noul(instructions='Does this convey urgency?', criteria={'true': 'yes', 'false': 'no'})},
	)

	assert sent[0]['model'] == 'jev-latest'
	assert sent[0]['state'] == 'payouts have been failing for three days'
	assert sent[0]['questions']['urgent']['type'] == 'noul'
	assert decisions.get('urgent').is_true is True
	assert decisions.model == 'jev-1.13.0'


async def test_every_way_of_failing_means_you_decide_it_yourself(jev_server):
	"""No key, a dead endpoint, a 500 and a body that is not JSON. None may raise, and
	none may produce an answer, because a missing answer routes to the agent while a
	defaulted one would have it act on a decision nothing made."""
	question = {'q': Noul(instructions='Is this a login wall?')}

	assert await Jev(api_key='').ask('anything', question) == Decisions()

	# Nothing listening on that path.
	dead = Jev(api_key='k', url=jev_server.url_for('/nothing-here'), timeout=2.0)
	assert not await dead.ask('anything', question)

	jev_server.expect_request('/boom').respond_with_data('upstream exploded', status=500)
	assert not await Jev(api_key='k', url=jev_server.url_for('/boom'), timeout=2.0).ask('anything', question)

	jev_server.expect_request('/garbage').respond_with_data('<html>not json</html>', content_type='text/html')
	assert not await Jev(api_key='k', url=jev_server.url_for('/garbage'), timeout=2.0).ask('anything', question)


def test_an_answer_that_does_not_parse_is_dropped_rather_than_guessed():
	"""Half a response is not a decision. Anything unusable has to go missing, loudly."""
	decisions = parse_decisions(
		{
			'model': 'jev-1.13.0',
			'answers': {
				'good': {'type': 'noul', 'noul': 0.9},
				'no_value': {'type': 'noul'},
				'wrong_type': {'type': 'telepathy', 'noul': 0.9},
				'not_a_dict': 'yes',
			},
			'usage': {'input_tokens': 296, 'output_tokens': 0},
		}
	)

	assert set(decisions.answers) == {'good'}, 'only the well-formed answer should survive'
	assert decisions.input_tokens == 296


def test_confidence_is_read_separately_from_the_winning_option():
	"""A choice can win and still be a coin flip. Acting on the winner alone acts on noise."""
	sure = parse_decisions(
		{'answers': {'t': {'type': 'choice', 'choice': 'search', 'probabilities': {'search': 0.9}, 'confidence': 0.88}}}
	).get('t')
	unsure = parse_decisions(
		{'answers': {'t': {'type': 'choice', 'choice': 'search', 'probabilities': {'search': 0.34}, 'confidence': 0.31}}}
	).get('t')

	assert sure.certain() and not unsure.certain()

	# A noul reports no confidence of its own, so distance from even is the measure.
	assert parse_decisions({'answers': {'n': {'type': 'noul', 'noul': 0.97}}}).get('n').certain()
	assert not parse_decisions({'answers': {'n': {'type': 'noul', 'noul': 0.53}}}).get('n').certain()


async def test_picking_a_tool_reads_the_surface_not_the_page(jev_server):
	"""The whole economy of this: the state billed for is the synthesized tool surface,
	which synthesis already reduced from the markup."""
	sent: list = []
	jev_server.expect_request('/v1/systemone').respond_with_handler(
		_answering(
			{'answers': {'tool': {'type': 'choice', 'choice': 'search', 'probabilities': {'search': 0.93}, 'confidence': 0.9}}},
			sent,
		)
	)
	page = _page(('search', 'search the catalogue'), ('next_page', 'go to the next page'))

	answer = await choose_tool(Jev(api_key='k', url=jev_server.url_for('/v1/systemone')), page, 'find a blue shirt')

	assert answer is not None and answer.value == 'search'
	state = sent[0]['state']
	assert state['url'] == 'https://shop.test/search'
	assert [t['name'] for t in state['tools']] == ['search()', 'next_page()']
	assert 'html' not in json.dumps(state).lower(), 'the page markup has no business being in here'


async def test_the_model_is_allowed_to_say_none_of_these(jev_server):
	"""A forced choice over wrong options still returns one of them, confidently. The
	escape hatch is what stops a confident wrong tool call."""
	sent: list = []
	jev_server.expect_request('/v1/systemone').respond_with_handler(
		_answering(
			{'answers': {'tool': {'type': 'choice', 'choice': 'none_of_these', 'confidence': 0.95}}},
			sent,
		)
	)
	page = _page(('search', 'search the catalogue'))

	answer = await choose_tool(Jev(api_key='k', url=jev_server.url_for('/v1/systemone')), page, 'delete my account')

	assert 'none_of_these' in sent[0]['questions']['tool']['criteria'], 'the option has to be offered'
	assert answer is None, 'declining must read as "you decide", not as a tool named none_of_these'


async def test_an_unsure_pick_is_handed_back_to_the_agent(jev_server):
	jev_server.expect_request('/v1/systemone').respond_with_handler(
		_answering({'answers': {'tool': {'type': 'choice', 'choice': 'search', 'confidence': 0.4}}})
	)
	page = _page(('search', 'search the catalogue'))

	assert await choose_tool(Jev(api_key='k', url=jev_server.url_for('/v1/systemone')), page, 'find a shirt') is None


async def test_triage_asks_the_four_questions_that_come_up_constantly(jev_server):
	sent: list = []
	jev_server.expect_request('/v1/systemone').respond_with_handler(
		_answering(
			{
				'answers': {
					'needs_sign_in': {'type': 'noul', 'noul': 0.97},
					'consent_wall': {'type': 'noul', 'noul': 0.02},
					'is_error': {'type': 'noul', 'noul': 0.01},
					'blocked_as_bot': {'type': 'noul', 'noul': 0.04},
				}
			},
			sent,
		)
	)

	decisions = await triage_page(Jev(api_key='k', url=jev_server.url_for('/v1/systemone')), _page(('log_in', 'sign in')))

	assert set(sent[0]['questions']) == {'needs_sign_in', 'consent_wall', 'is_error', 'blocked_as_bot'}
	assert decisions.get('needs_sign_in').is_true
	assert not decisions.get('consent_wall').is_true


def test_a_question_that_cannot_be_answered_is_rejected_before_it_costs_a_request():
	"""Input is the billed side, so a malformed question should fail locally, not remotely."""
	with pytest.raises(ValueError):
		Choice(instructions='pick', criteria={})
	with pytest.raises(ValueError):
		Score(instructions='rate', criteria=['only one level'])
	with pytest.raises(ValueError):
		Score(instructions='rate', criteria=[f'level {i}' for i in range(11)])


async def test_too_many_questions_is_an_error_not_a_silent_truncation():
	"""Answering twelve of sixteen questions answers a different question than was asked."""
	jev = Jev(api_key='k')
	with pytest.raises(ValueError, match='at most'):
		await jev.ask('state', {f'q{i}': Noul(instructions='?') for i in range(MAX_QUESTIONS + 1)})


async def test_state_is_clipped_because_input_is_the_side_that_is_billed(jev_server):
	sent: list = []
	jev_server.expect_request('/v1/systemone').respond_with_handler(_answering({'answers': {}}, sent))

	await Jev(api_key='k', url=jev_server.url_for('/v1/systemone')).ask(
		'x' * (MAX_STATE_CHARS * 2), {'q': Noul(instructions='?')}
	)

	assert len(sent[0]['state']) == MAX_STATE_CHARS


def test_the_page_state_carries_what_a_page_can_do_and_nothing_else():
	state = page_state(_page(('search', 'search the catalogue')), title='Shop')

	assert state == {
		'url': 'https://shop.test/search',
		'title': 'Shop',
		'tools': [{'name': 'search()', 'does': 'search the catalogue'}],
	}
