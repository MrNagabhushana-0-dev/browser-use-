"""Typed questions, and the typed answers that come back.

Most of what an agent decides is not writing. "Is this a cookie wall?", "did that click
work?", "which of these eleven tools matches the goal?" are classifications, and running
them through a model that generates prose is paying for a paragraph to get a boolean —
plus the cost of putting the whole page in front of it to ask.

A decision model answers the question directly: one forward pass, a distribution over the
allowed answers, and a confidence. There is no output to bill for because there is no
generation. The shape here follows TypeSafe's Jev API, which is the one that exists.

Three primitives cover the decisions this library actually makes:

- `Noul` — a yes/no, answered as a probability rather than a bit, which matters because
  "probably a login wall, 0.55" and "definitely a login wall, 0.98" should not lead to
  the same action.
- `Choice` — pick one of up to 255 named options. This is the one that pairs with the
  synthesized tool surface: the options *are* the tools.
- `Score` — place something on an ordered scale of 2 to 10 described levels.

Everything is bounded on the way out and validated on the way in. The answers are used to
decide what to do next, so a malformed response has to fail loudly as a missing answer
rather than quietly as a default.
"""

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

# The API's own limits, enforced here so a bad question fails before it costs a request.
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10

# Questions per call. The model answers a map of them in one pass, which is the whole
# economy of it — but a caller that asks fifty things at once is not deciding, it is
# fishing.
MAX_QUESTIONS = 16

# Characters of state sent for evaluation. Input is the only thing billed, so this is the
# cost control. The synthesized tool surface for a page is around a thousand tokens, which
# fits comfortably.
MAX_STATE_CHARS = 24_000


def _validate_option_name(value: str) -> str:
	name = value.strip()
	if not name:
		raise ValueError('a choice option needs a name')
	return name


OptionName = Annotated[str, AfterValidator(_validate_option_name)]


class Noul(BaseModel):
	"""A yes/no question, answered as a probability."""

	model_config = ConfigDict(extra='forbid')

	type: Literal['noul'] = 'noul'
	instructions: str
	# What each side means. The API takes the keys 'true' and 'false'.
	criteria: dict[str, str] = Field(default_factory=dict)

	def payload(self) -> dict[str, Any]:
		body: dict[str, Any] = {'type': 'noul', 'instructions': self.instructions}
		if self.criteria:
			body['criteria'] = self.criteria
		return body


class Choice(BaseModel):
	"""Pick one of a named set. The options are the answers, not a hint toward them."""

	model_config = ConfigDict(extra='forbid')

	type: Literal['choice'] = 'choice'
	instructions: str
	# option name -> what it means, or None when the name says it.
	criteria: dict[OptionName, str | None]

	@model_validator(mode='after')
	def _bounded(self):
		if not self.criteria:
			raise ValueError('a choice question needs at least one option')
		if len(self.criteria) > MAX_CHOICE_OPTIONS:
			raise ValueError(f'a choice question takes at most {MAX_CHOICE_OPTIONS} options, got {len(self.criteria)}')
		return self

	def payload(self) -> dict[str, Any]:
		return {'type': 'choice', 'instructions': self.instructions, 'criteria': self.criteria}


class Score(BaseModel):
	"""Place something on an ordered scale of described levels."""

	model_config = ConfigDict(extra='forbid')

	type: Literal['score'] = 'score'
	instructions: str
	criteria: list[str]

	@model_validator(mode='after')
	def _bounded(self):
		if not MIN_SCORE_LEVELS <= len(self.criteria) <= MAX_SCORE_LEVELS:
			raise ValueError(
				f'a score question needs {MIN_SCORE_LEVELS}-{MAX_SCORE_LEVELS} ordered levels, got {len(self.criteria)}'
			)
		return self

	def payload(self) -> dict[str, Any]:
		return {'type': 'score', 'instructions': self.instructions, 'criteria': self.criteria}


Question = Noul | Choice | Score


class Answer(BaseModel):
	"""One decision, with how sure the model was about it.

	`confidence` is deliberately separate from the winning probability. A choice can be
	the most likely option and still be a coin flip between two of them, and a caller that
	acts on the winner without looking at how close it was will act confidently on noise.
	"""

	model_config = ConfigDict(extra='forbid')

	name: str
	kind: Literal['noul', 'choice', 'score']
	# Noul: the probability of true. Choice: the winning option. Score: the position.
	value: float | str
	probabilities: dict[str, float] = Field(default_factory=dict)
	confidence: float | None = None

	@property
	def is_true(self) -> bool:
		"""For a noul: whether it came back on the true side of even."""
		return isinstance(self.value, float) and self.value >= 0.5

	def certain(self, threshold: float = 0.7) -> bool:
		"""Whether this is worth acting on without a second opinion.

		A noul reports no confidence of its own, so its distance from even is the measure:
		0.95 is worth acting on, 0.52 is not, and both would read as 'true'.
		"""
		if self.kind == 'noul' and isinstance(self.value, float):
			return abs(self.value - 0.5) * 2 >= threshold
		return (self.confidence or 0.0) >= threshold


class Decisions(BaseModel):
	"""Everything that came back from one call."""

	model_config = ConfigDict(extra='forbid')

	model: str = ''
	answers: dict[str, Answer] = Field(default_factory=dict)
	input_tokens: int = 0
	output_tokens: int = 0

	def get(self, name: str) -> Answer | None:
		return self.answers.get(name)

	def __bool__(self) -> bool:
		return bool(self.answers)
