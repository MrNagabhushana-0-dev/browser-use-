"""A decision model the agent can consult, and survive losing.

This is an optimization, never a dependency. Every failure path — no key, no network, a
timeout, a malformed body, a question the model declined — returns an empty result, and
every caller is written to carry on without it. An agent that stops working because a
classifier was slow is worse than one that never had a classifier.

The economy is in what gets sent. Input is the only thing billed, so the state handed
over is the page's *synthesized tool surface* — the compact typed description this library
already builds — rather than its markup. The two techniques compose: the surface makes the
page small enough to ask cheap questions about, and the decision model answers them
without generating a word.
"""

import json
import logging
import os
from typing import Any

import httpx

from browser_use.decide.views import (
	MAX_QUESTIONS,
	MAX_STATE_CHARS,
	Answer,
	Decisions,
	Question,
)

logger = logging.getLogger(__name__)

JEV_URL = 'https://api.typesafe.ai/v1/systemone'
DEFAULT_MODEL = 'jev-latest'

# A decision is on the critical path of an agent step, so it gets a short leash. The
# model's own budget is 70-500ms; anything past a couple of seconds is a network problem
# and the answer is no longer worth waiting for.
DEFAULT_TIMEOUT = 4.0


class Jev:
	"""Client for TypeSafe's Jev decision model.

	Not an LLM provider and deliberately not in `browser_use/llm/`: it does not generate
	text, does not stream, and does not take a conversation. It takes a state and a map of
	typed questions and returns typed answers, which is a different shape of thing.
	"""

	def __init__(
		self,
		api_key: str | None = None,
		model: str = DEFAULT_MODEL,
		url: str = JEV_URL,
		timeout: float = DEFAULT_TIMEOUT,
	) -> None:
		self.api_key = api_key or os.environ.get('TYPESAFE_API_KEY') or os.environ.get('JEV_API_KEY') or ''
		self.model = model
		self.url = url
		self.timeout = timeout

	@property
	def available(self) -> bool:
		"""Whether there is any point calling. Callers check this before building questions."""
		return bool(self.api_key)

	async def ask(self, state: Any, questions: dict[str, Question]) -> Decisions:
		"""Answer a map of typed questions about a state. Never raises."""
		if not questions:
			return Decisions()
		if not self.available:
			logger.debug('🎛️ No decision-model key set (TYPESAFE_API_KEY); skipping')
			return Decisions()
		if len(questions) > MAX_QUESTIONS:
			# Truncating silently would answer a different question than the caller asked.
			raise ValueError(f'at most {MAX_QUESTIONS} questions per call, got {len(questions)}')

		body = {
			'state': _clip_state(state),
			'model': self.model,
			'questions': {name: question.payload() for name, question in questions.items()},
		}
		try:
			async with httpx.AsyncClient(timeout=self.timeout) as client:
				response = await client.post(
					self.url,
					headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'},
					json=body,
				)
				response.raise_for_status()
				raw = response.json()
		except Exception as e:
			logger.debug(f'🎛️ Decision model unavailable: {type(e).__name__}: {e}')
			return Decisions()

		return parse_decisions(raw)


def _clip_state(state: Any) -> Any:
	"""Keep the billed input bounded, without silently changing its shape."""
	if isinstance(state, str):
		return state[:MAX_STATE_CHARS]
	try:
		encoded = json.dumps(state)
	except (TypeError, ValueError):
		return str(state)[:MAX_STATE_CHARS]
	# Structured state stays structured while it fits; past the cap it becomes the text
	# that would have been sent, because a truncated JSON document is not JSON.
	return state if len(encoded) <= MAX_STATE_CHARS else encoded[:MAX_STATE_CHARS]


def parse_decisions(raw: Any) -> Decisions:
	"""Turn the API's body into typed answers, dropping anything malformed.

	A dropped answer is a missing answer, and every caller treats missing as "ask someone
	else". The alternative — defaulting a classification to False — would have the agent
	act on a decision nothing actually made.
	"""
	if not isinstance(raw, dict):
		return Decisions()

	raw_usage = raw.get('usage')
	usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
	decisions = Decisions(
		model=str(raw.get('model') or ''),
		input_tokens=int(usage.get('input_tokens') or 0),
		output_tokens=int(usage.get('output_tokens') or 0),
	)

	answers = raw.get('answers')
	if not isinstance(answers, dict):
		return decisions

	for name, body in answers.items():
		if not isinstance(name, str) or not isinstance(body, dict):
			continue
		kind = body.get('type')
		raw_probabilities = body.get('probabilities')
		probabilities: dict[str, Any] = raw_probabilities if isinstance(raw_probabilities, dict) else {}
		confidence = body.get('confidence')

		value: float | str | None = None
		if kind == 'noul' and isinstance(body.get('noul'), (int, float)):
			value = float(body['noul'])
		elif kind == 'choice' and isinstance(body.get('choice'), str):
			value = str(body['choice'])
		elif kind == 'score' and isinstance(body.get('score'), (int, float)):
			value = float(body['score'])
		if value is None or kind not in ('noul', 'choice', 'score'):
			logger.debug(f'🎛️ Dropping unusable answer for {name!r}: {body}')
			continue

		decisions.answers[name] = Answer(
			name=name,
			kind=kind,
			value=value,
			probabilities={str(k): float(v) for k, v in probabilities.items() if isinstance(v, (int, float))},
			confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
		)
	return decisions
