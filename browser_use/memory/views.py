"""Models for workflow memory.

A workflow is what the agent learned by succeeding once: the route it actually took
through a site, compacted to the decisions that mattered. Replaying that knowledge on the
next task for the same site is the cheapest accuracy there is — Agent Workflow Memory
(ICML 2025, arXiv:2409.07429) reports +24.6% to +51.1% relative success on Mind2Web from
exactly this, because the agent stops re-deriving navigation it has already solved.

What is deliberately *not* stored: anything the agent typed. A trajectory that includes
`input_text` on a password field would otherwise put credentials on disk in plain text,
in a file nobody thinks of as a secret store. Steps keep the action and its target, never
the value.
"""

import re
from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from uuid_extensions import uuid7str

MAX_STEPS_PER_WORKFLOW = 24
MAX_DETAIL_LEN = 120
MAX_TASK_LEN = 300

# Actions that say nothing about how a site works. Keeping them makes a replayed workflow
# longer without making it more useful.
UNINFORMATIVE_ACTIONS = frozenset({'wait', 'screenshot', 'done', 'think', 'scroll'})

# Actions whose parameters carry user input. The action is worth remembering; the value
# never is.
VALUE_BEARING_ACTIONS = frozenset(
	{
		# Registry names.
		'input',
		'send_keys',
		'select_dropdown',
		# Names used by custom actions people register themselves; harmless if unused,
		# and the cost of missing one is a password on disk.
		'input_text',
		'type',
		'type_text',
		'fill',
		'select_dropdown_option',
	}
)

_WHITESPACE_RE = re.compile(r'\s+')
_WORD_RE = re.compile(r'[a-z0-9]+')


def _clean(value: str) -> str:
	return _WHITESPACE_RE.sub(' ', value).strip()


def _clip_detail(value: str) -> str:
	cleaned = _clean(value)
	return cleaned if len(cleaned) <= MAX_DETAIL_LEN else cleaned[: MAX_DETAIL_LEN - 1] + '…'


def _clip_task(value: str) -> str:
	cleaned = _clean(value)
	return cleaned if len(cleaned) <= MAX_TASK_LEN else cleaned[: MAX_TASK_LEN - 1] + '…'


def tokenize(text: str) -> set[str]:
	"""Content words of a task, for overlap scoring."""
	return {word for word in _WORD_RE.findall(text.lower()) if len(word) > 2}


class WorkflowStep(BaseModel):
	"""One remembered action: what was done, and to what."""

	model_config = ConfigDict(extra='forbid')

	action: str
	detail: Annotated[str, AfterValidator(_clip_detail)] = ''

	def render(self) -> str:
		return f'{self.action}({self.detail})' if self.detail else f'{self.action}()'


class Workflow(BaseModel):
	"""A route through one site that worked."""

	model_config = ConfigDict(extra='forbid')

	id: str = Field(default_factory=uuid7str)
	domain: str
	task: Annotated[str, AfterValidator(_clip_task)]
	steps: list[WorkflowStep]
	created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
	last_used_at: datetime | None = None
	uses: int = 0

	def render(self) -> str:
		lines = [f'On {self.domain}, to "{self.task}":']
		lines.extend(f'  {i}. {step.render()}' for i, step in enumerate(self.steps, start=1))
		return '\n'.join(lines)

	def score(self, task: str, domain: str) -> float:
		"""How relevant this workflow is to the task at hand.

		Same-site is the strong signal — a route through one site says nothing about
		another — so a domain mismatch is disqualifying rather than merely down-weighted.
		Within a site, rank by wording overlap with the remembered task.
		"""
		if domain != self.domain:
			return 0.0
		wanted = tokenize(task)
		remembered = tokenize(self.task)
		if not wanted or not remembered:
			# Same site and nothing to compare on: still worth more than nothing.
			return 0.1
		overlap = len(wanted & remembered) / len(wanted | remembered)
		# Floor keeps same-site workflows eligible even when the wording is entirely new,
		# since the navigation knowledge usually still transfers.
		return max(overlap, 0.1)


def render_workflow_memory(workflows: list[Workflow]) -> str:
	"""Render the prompt block. Empty string when nothing is remembered."""
	if not workflows:
		return ''
	header = (
		'You have done this before on this site. These routes worked; follow one when it fits, '
		'and ignore it when the page has changed or the task differs.'
	)
	return '\n'.join([header, *(workflow.render() for workflow in workflows)])
