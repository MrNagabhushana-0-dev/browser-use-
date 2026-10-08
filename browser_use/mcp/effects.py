"""Whether a failed tool call changed anything (after BrowserSkill's `effect_state`).

An agent that retries a failed click must know if the first one already landed: a second "Pay" is worse than the
error. Every failed call says one of three things:

- `none`: nothing was sent to the page. Fix the cause and try again freely.
- `unknown`: input or navigation had started when it failed. Look before trying again.
- `committed`: it went through; only what came after it failed. Don't repeat it.

It fails closed. A tool that only looks reports `none`. An acting tool reports `none` only when it failed before
`act()` began sending, or raised `Refused`; once sending began, `unknown` until it is known to have finished.
"""

import json
from collections.abc import Awaitable
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Literal, TypeVar

if TYPE_CHECKING:
	import mcp.types as types

EffectState = Literal['none', 'committed', 'unknown']
T = TypeVar('T')

HINTS: dict[str, str] = {
	'none': 'nothing was done, so it is safe to try again once the cause is fixed',
	'unknown': 'it had started and may have partly or fully happened; look at the page before trying again',
	'committed': "it was done and only what followed failed; don't repeat it",
}

_state: ContextVar[EffectState | None] = ContextVar('effect_state', default=None)


class Refused(ValueError):
	"""Refused before anything was sent: the effect is none, whatever the tool."""


def begin() -> Token:
	return _state.set('none')


def end(token: Token) -> None:
	_state.reset(token)


async def act(step: Awaitable[T]) -> T:
	"""Run one step that sends input or navigates: unknown while it runs, committed once it returned."""
	_state.set('unknown')
	result = await step
	_state.set('committed')
	return result


def state_of(error: BaseException, read_only: bool, instrumented: bool = True) -> EffectState:
	current = _state.get()
	if read_only or (isinstance(error, Refused) and current in (None, 'none')):
		return 'none'
	if not instrumented:  # a tool whose sending isn't marked: assume it may have acted
		return 'committed' if current == 'committed' else 'unknown'
	return current or 'none'


def failure(tool: str, error: BaseException, read_only: bool, instrumented: bool = True) -> 'types.CallToolResult':
	"""The error result for a failed call, with its effect in words and as structured content."""
	import mcp.types as types  # here, so Refused can be raised where the MCP SDK isn't installed

	state = state_of(error, read_only, instrumented)
	data = {'tool': tool, 'error': str(error), 'effect_state': state}
	assert state in HINTS, state
	return types.CallToolResult(
		# One block: the spec asks for structured content to be in text too, and some clients show only the first.
		content=[types.TextContent(type='text', text=f'Error: {error}\neffect: {state}: {HINTS[state]}.\n{json.dumps(data)}')],
		structured_content=data,
		is_error=True,
	)
