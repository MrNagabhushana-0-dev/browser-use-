"""Questions worth asking about a page, and the cheap thing to ask them about.

This is where the two ideas meet. Synthesis already reduces a page to a typed tool
surface — around a thousand tokens where the markup was thirty thousand. A decision model
is billed only on what it reads. So asking "which of these tools matches the goal" against
the *surface* costs a few hundred tokens and no generation, where asking the same question
of an agent step costs the whole page and a paragraph of reasoning to get one name back.

Nothing here is required. Every function returns None or an empty result when the model is
unavailable or unsure, and every caller falls back to the agent deciding for itself.
"""

import logging
from typing import TYPE_CHECKING

from browser_use.decide.service import Jev
from browser_use.decide.views import MAX_CHOICE_OPTIONS, Answer, Choice, Decisions, Noul

if TYPE_CHECKING:
	from browser_use.webmcp.views import WebMCPPageTools

logger = logging.getLogger(__name__)

# Below this, a pick is a coin flip dressed as an answer and the agent should decide.
DEFAULT_CERTAINTY = 0.7

# The reserved answer for "none of these", so the model can decline instead of being
# forced to name a tool it does not believe in. Without an escape hatch a forced choice
# over eleven wrong options still returns one of them, confidently.
NO_TOOL = 'none_of_these'


def page_state(page_tools: 'WebMCPPageTools', title: str = '') -> dict:
	"""The compact description of a page that questions get asked about.

	Deliberately the tool surface and not the markup: it is what the page can *do*, which
	is what every question below is actually about, and it is already small.
	"""
	return {
		'url': page_tools.url,
		'title': title,
		'tools': [{'name': tool.signature(), 'does': tool.description} for tool in page_tools.tools],
	}


async def choose_tool(
	jev: Jev,
	page_tools: 'WebMCPPageTools',
	goal: str,
	title: str = '',
	certainty: float = DEFAULT_CERTAINTY,
) -> Answer | None:
	"""Which tool on this page serves the goal, if the model is sure enough to say.

	Returns None when there is no key, no tools, nothing certain, or the model picked the
	escape hatch — all of which mean the same thing to the caller: decide it yourself.
	"""
	assert goal, 'choose_tool() needs a goal'
	if not jev.available or not page_tools.tools:
		return None

	options: dict[str, str | None] = {
		tool.name: (tool.description or None) for tool in page_tools.tools[: MAX_CHOICE_OPTIONS - 1]
	}
	options[NO_TOOL] = 'None of these tools does what the goal needs'

	decisions = await jev.ask(
		page_state(page_tools, title),
		{
			'tool': Choice(
				instructions=f'Which tool on this page most directly does this: {goal}',
				criteria=options,
			)
		},
	)
	answer = decisions.get('tool')
	if answer is None or answer.value == NO_TOOL:
		return None
	if not answer.certain(certainty):
		logger.debug(f'🎛️ Tool pick {answer.value!r} not certain enough ({answer.confidence}); leaving it to the agent')
		return None
	return answer


async def triage_page(jev: Jev, page_tools: 'WebMCPPageTools', title: str = '') -> Decisions:
	"""What kind of page this is, before spending an agent step finding out.

	These four come up constantly and each one has an obvious next move that does not need
	a frontier model to work out — the expensive part is noticing, not deciding.
	"""
	if not jev.available:
		return Decisions()

	return await jev.ask(
		page_state(page_tools, title),
		{
			'needs_sign_in': Noul(
				instructions='Does this page require signing in before its real content can be used?',
				criteria={'true': 'A login, paywall or account gate', 'false': 'The content is reachable'},
			),
			'consent_wall': Noul(
				instructions='Is a cookie or consent dialog blocking the page?',
				criteria={'true': 'A consent or cookie banner must be dismissed', 'false': 'Nothing is blocking'},
			),
			'is_error': Noul(
				instructions='Is this an error page rather than the content that was asked for?',
				criteria={'true': 'Not found, rate limited, blocked or broken', 'false': 'Real content'},
			),
			'blocked_as_bot': Noul(
				instructions='Is this page refusing automated access, for example a captcha or a bot check?',
				criteria={'true': 'A bot check or captcha', 'false': 'No such challenge'},
			),
		},
	)
