"""Retinat: an MCP server that gives a model eyes, not a DOM.

browser-use's MCP server (`browser-use --mcp`) drives a browser through its structure: element
indices, HTML, extracted text. Retinat is its vision-first sibling. Every tool answers with what
the page *looks like* and *sounds like*, compressed for a model that takes images and text a
turn at a time:

- `retinat_watch` / `retinat_browse`: a playing video or a short-video feed, from the video's own
  frames and sound (heard even when muted), as one sheet plus a timeline and transcript.
- `retinat_look` / `retinat_scan`: any page as the compositor draws it - canvas, WebGL, CSS
  animation - one frame, or the whole page scrolled top to bottom as a few covering keyframes.
- `retinat_tap` / `retinat_swipe` / `retinat_next` / `retinat_click` / `retinat_type` /
  `retinat_key`: real touch, mouse and keyboard input, with feed moves confirmed by sight.
- `retinat_explore`: a whole site crawled and checked, with a bug report and a page sheet.
- `retinat_now`: one line on what is on screen and audible right now.
- `retinat_console`: what the page logged and threw, masked like the requests.
- `retinat_record`: a video of what the browser showed, started and stopped by the agent.
- `retinat_reflex` / `retinat_wait`: rules the page runs every frame, firing real input in milliseconds; the agent
  sleeps until one notifies.
- `retinat_requests`: what the page fetched (status, type, size, time, failures), and one response body,
  with tokens, passwords and keys masked.

It never runs page scripts on the model's behalf and has no Playwright anywhere. Bot walls are
reported as walls. Run it with `python -m browser_use.retinat` (or `retinat`), add `--cdp-url`
to attach to a Chrome you started yourself (your profile, your logins, your connection).

It reuses browser-use's MCP session management and configuration, so the two servers read the
same `~/.config/browseruse` settings; they run separate browsers unless both attach to the same
Chrome with `--cdp-url`.
"""

import asyncio
import base64
import json
import math
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from browser_use.eyes.views import CALL_BUDGET_S, Deadline
from browser_use.mcp.server import MCP_AVAILABLE, TEXT_FAILURES, BrowserUseServer, serve_until_stopped, types
from browser_use.net import NetworkMode, NetworkRouter
from browser_use.retinat.tools import (  # noqa: F401  (the tool lists are imported from here too)
	_browser_tools,
	_control_allowed,
	_control_tools,
	_desktop_allowed,
	_desktop_tools,
	_tools,
)
from browser_use.serving import CALL_STARTED

if TYPE_CHECKING:
	from browser_use.bridge import BridgeRelay
	from browser_use.desktop.service import DesktopControl
	from browser_use.eyes.clock import PageClock

TOOL_PREFIX = 'retinat_'
# Said on what a seeing tool returns when it was asked while a retinat_wait was pending: a client may send both at
# once, and the answer is then from before what the wait is for (found live: read as a stale picture for an hour)
_WAIT_PENDING_NOTE = (
	'\n⏳ asked while a retinat_wait was still waiting: this is from before what that wait is for. '
	'Ask again after the wait returns.'
)
# Tools that send input, navigate, scroll or play: refused up front while the person holds the wheel.
ACTING = frozenset(
	{
		'retinat_open',
		'retinat_explore',
		'retinat_watch',
		'retinat_scan',
		'retinat_browse',
		'retinat_next',
		'retinat_tap',
		'retinat_click',
		'retinat_swipe',
		'retinat_scroll',
		'retinat_type',
		'retinat_key',
		'retinat_reflex',
		'retinat_step',
	}
)

# Tools that can run long (a count of screens, items, pages or seconds): each keeps to the call's budget by itself.
# Every tool, these or not, is stopped BACKSTOP_GRACE_S past the budget if it has not ended (the client's 60 s minus
# room for the answer)
LONG_TOOLS = frozenset(
	{'retinat_scan', 'retinat_browse', 'retinat_watch', 'retinat_explore', 'retinat_desktop_watch', 'retinat_step'}
)
# Tools whose eyes run on the page's own timers, which stand still on a page paused by retinat_step: they would report
# a frozen page as it is, so they are refused there with the reason
NEEDS_PAGE_TIME = frozenset(
	{
		'retinat_watch',
		'retinat_browse',
		'retinat_next',
		'retinat_scan',
		'retinat_now',
		'retinat_changes',
		'retinat_reflex',
		'retinat_wait',
	}
)
_PAUSED = 'The page is paused by retinat_step: step it on, or give it real time back with retinat_step action=resume.'
_NOT_OURS = (
	"Step mode takes a tab's clock over for good (Chrome has no way to give it back), so Retinat uses it only in a "
	'browser it started itself, never in your own browser (--bridge or --cdp-url).'
)
BACKSTOP_GRACE_S = 6.0
# What a browser start may take in all, across calls: the first start after a machine boots reads Chrome off a cold
# disk, and went past the 30 s it was given. A call waits for a start only as long as its own budget allows, keeping
# START_RESERVE_S of it for its own work once the browser is up.
START_TIMEOUT_S = 120.0
# What a step keeps of its call to capture and send the frame it ended on
STEP_ANSWER_S = 2.0
# How wide (and tall, at most) a step's frame of just the game is drawn: readable without costing a full-page look's worth
STEP_GAME_WIDTH = 800
# What the eyes read a step's movement from: a picture of the region this wide, before the step and at each of its
# frames, or this many spread over it (the last included): things must not go far between two looks to be followed
MOTION_WIDTH = 400
MOTION_LOOKS = 24
MOTION_SETTLE_S = 0.15  # after a step, for the stream's last frames of it to arrive
# Game time stands still between steps, so the end of one step runs on into the next: this much of it is read with the
# next step, so a short step near an obstacle still shows what moves
MOTION_BEFORE_MS = 300.0
KEY_HOLD_MAX_MS = 5000.0  # the longest retinat_key holds keys down (a step plays at most as long)
KEY_WITH_MAX = 4  # keys held down along with the key
KEY_ANSWER_S = 1.0  # what a key hold keeps of its call to let the keys go and answer
# The largest canvas, frame or video in view (its visible part), where a game is drawn, as [x, y, width, height]
_GAME_REGION_JS = r"""(() => {
	let best = null, bestArea = 0;
	for (const el of document.querySelectorAll('canvas, iframe, video')) {
		const r = el.getBoundingClientRect();
		const x = Math.max(0, r.left), y = Math.max(0, r.top);
		const w = Math.min(innerWidth, r.right) - x, h = Math.min(innerHeight, r.bottom) - y;
		if (w < 50 || h < 50 || getComputedStyle(el).visibility === 'hidden') continue;
		if (w * h > bestArea) { bestArea = w * h; best = [x, y, w, h]; }
	}
	return best;
})()"""
START_RESERVE_S = 15.0  # a first retinat_open after its launch took 13-15 s more (eyes, navigation, the wall check)
_START_STOPPED = 'The browser start was stopped (the route changed, or the server is closing). Nothing was done; call again.'

# What a click at (x, y) lands on: the topmost element there (into open shadow roots), lifted to the control that owns
# it, described as role plus the text a person would read on it.
_HIT_JS = r"""(x, y) => {
	let el = document.elementFromPoint(x, y);
	while (el && el.shadowRoot) {
		const inner = el.shadowRoot.elementFromPoint(x, y);
		if (!inner || inner === el) break;
		el = inner;
	}
	if (!el) return null;
	const control = el.closest('a[href], button, input, select, textarea, summary, label, [role], [onclick], [tabindex]');
	const hit = control || el;
	const inputRole = { checkbox: 'checkbox', radio: 'radio', button: 'button', submit: 'button', reset: 'button', range: 'slider' };
	const tagRole = { A: 'link', BUTTON: 'button', SELECT: 'combobox', TEXTAREA: 'textbox', SUMMARY: 'button', IMG: 'img', IFRAME: 'iframe' };
	const role = hit.getAttribute('role') || (hit.tagName === 'INPUT' ? inputRole[hit.type] || 'textbox' : tagRole[hit.tagName])
		|| hit.tagName.toLowerCase();
	const text = control ? hit.innerText : el.textContent;
	const name = [hit.getAttribute('aria-label'), text, hit.value, hit.placeholder, hit.title, hit.alt]
		.find((v) => typeof v === 'string' && v.trim()) || '';
	const short = name.replace(/\s+/g, ' ').trim();
	return short ? `${role} "${short.length > 80 ? short.slice(0, 79) + '…' : short}"` : role;
}"""


def _as_number(value: Any) -> Any:
	"""A number given as text ("250") as the number; anything else as it came."""
	if isinstance(value, str):
		try:
			return float(value)
		except ValueError:
			return value
	return value


def _as_list(value: Any) -> Any:
	"""A list given as JSON text ('["ArrowRight"]') or as one name ("ArrowRight") as the list; none as []."""
	if value is None or value == '':
		return []
	if isinstance(value, str):
		try:
			parsed = json.loads(value)
		except ValueError:
			return [value]
		return parsed if isinstance(parsed, list) else [value]
	return value


class RetinatServer(BrowserUseServer):
	"""browser-use's MCP session handling, with a vision-first tool surface."""

	def __init__(
		self,
		cdp_url: str | None = None,
		session_timeout_minutes: int = 30,
		network: NetworkRouter | None = None,
		bridge: 'BridgeRelay | None' = None,
		call_budget_s: float | None = None,
		backstop_grace_s: float = BACKSTOP_GRACE_S,
	) -> None:
		# What one tool call may spend before it answers with what it has: MCP clients drop a call at 60 s
		self.call_budget_s = float(call_budget_s or os.environ.get('BROWSER_USE_MCP_CALL_BUDGET_S') or CALL_BUDGET_S)
		assert self.call_budget_s > 0, 'a call budget is some seconds'
		# The tools held to the budget by a hard stop, and how long after the budget it comes
		self.backstop_grace_s = backstop_grace_s
		# Agents get `auto` unless told otherwise: direct first, Tor only after a network failure or a
		# geo-block, never for a bot wall. `--network off` (or RETINAT/BROWSER_USE env) turns it off.
		super().__init__(
			session_timeout_minutes=session_timeout_minutes,
			network=network or NetworkRouter.from_env(default=NetworkMode.AUTO),
		)
		from mcp.server import Server

		from browser_use.utils import get_browser_use_version

		self.bridge = bridge
		self.cdp_url = bridge.cdp_url if bridge else cdp_url
		self._control: 'DesktopControl | None' = None  # computer use, made on first use
		self._waits = 0  # retinat_wait calls in progress
		self._starting: asyncio.Task[None] | None = None  # the browser start in flight, which calls join
		self._starting_since = 0.0
		self._clocks: dict[str, 'PageClock'] = {}  # per tab, once retinat_step has paused it
		# the last step's looks for the next one to run on from: (its clock, the clock's moved_ms then, area, looks)
		self._motion_before: tuple[Any, float, tuple[float, ...], list[tuple[float, Any]]] | None = None
		from browser_use.config import CONFIG
		from browser_use.eyes.narration import NarrationLog

		# live narration: each watch result owed a narration before the next watch (see eyes/narration.py)
		self._narration = NarrationLog(CONFIG.BROWSER_USE_CONFIG_DIR / 'narration' / f'{time.strftime("%Y%m%d-%H%M%S")}.jsonl')
		self.server = Server('retinat', version=get_browser_use_version())
		self._setup_retinat_handlers()

	def _setup_retinat_handlers(self) -> None:
		async def list_tools(_context: Any, _params: 'types.PaginatedRequestParams') -> 'types.ListToolsResult':
			return types.ListToolsResult(tools=[*_tools(), *self._network_tool_entries('retinat')])

		async def call_tool(_context: Any, params: 'types.CallToolRequestParams') -> 'types.CallToolResult':
			from browser_use.mcp import effects

			# The call's clock starts here: a browser launch or opening the eyes below spends the same time the client is
			# counting
			# From when the front took the call, if it waited for this server to load
			started = CALL_STARTED.get()
			deadline = (
				Deadline.after(self.call_budget_s)
				if started is None
				else Deadline(started=started, at=started + self.call_budget_s)
			)
			backstop: asyncio.Timeout | None = None
			token = effects.begin()
			listed = {t.name: t for t in [*_tools(), *self._network_tool_entries('retinat')]}
			looking = {name for name, t in listed.items() if t.annotations and t.annotations.read_only_hint}
			waiting = params.name == 'retinat_wait'
			before_a_wait = self._waits > 0 and params.name in looking and not waiting
			self._waits += waiting
			try:
				if params.name in listed:
					effects.check_arguments(listed[params.name].input_schema, params.arguments or {})
				# A last resort for a tool that did not stop in time by itself: it loses what the tool had, so the long
				# tools stop on their deadline first; this keeps every call answering while the client still listens (a
				# look on a page that hung ran until the client gave up, with no answer at all)
				limit = deadline.left() + self.backstop_grace_s if deadline.bounded else None
				async with asyncio.timeout(limit) as backstop:
					result = await self._call_retinat(params.name, params.arguments or {}, deadline)
				if isinstance(result, str) and result.startswith(TEXT_FAILURES):  # shared handlers report checks as text
					raise effects.refused(result.removeprefix('Error: '))
				content: list[types.ContentBlock] = (
					result if isinstance(result, list) else [types.TextContent(type='text', text=result)]
				)
				if before_a_wait and content and isinstance(content[0], types.TextContent):
					content[0] = types.TextContent(type='text', text=content[0].text + _WAIT_PENDING_NOTE)
				return types.CallToolResult(content=content)
			except TimeoutError as e:
				if backstop is not None and backstop.expired():
					e = RuntimeError(
						f'{params.name} ran {deadline.spent():.0f}s, past its {deadline.budget_s:.0f}s budget, and was stopped '
						'before the client would give up on it'
					)
				return effects.failure(params.name, e, read_only=params.name in looking)
			except Exception as e:
				# Every input and navigation below goes through effects.act, so a failure says if anything happened.
				return effects.failure(params.name, e, read_only=params.name in looking)
			finally:
				self._waits -= waiting
				effects.end(token)
				if self.browser_session is not None:  # a long call ends in use too
					self._update_session_activity(self.browser_session.id)

		async def empty_resources(_context: Any, _params: 'types.PaginatedRequestParams') -> 'types.ListResourcesResult':
			return types.ListResourcesResult(resources=[])

		async def empty_prompts(_context: Any, _params: 'types.PaginatedRequestParams') -> 'types.ListPromptsResult':
			return types.ListPromptsResult(prompts=[])

		self.server.add_request_handler('tools/list', types.PaginatedRequestParams, list_tools)
		self.server.add_request_handler('tools/call', types.CallToolRequestParams, call_tool)
		self.server.add_request_handler('resources/list', types.PaginatedRequestParams, empty_resources)
		self.server.add_request_handler('prompts/list', types.PaginatedRequestParams, empty_prompts)

	async def _ensure_session(self, deadline: Deadline | None = None) -> None:
		"""A browser for this call. Its start runs on past a call that cannot wait for it, and later calls join that same
		start: a slow start was stopped part way at 30 s, and the next call began another from nothing."""
		if self._starting is None:
			if self.browser_session:
				return
			self._starting = asyncio.create_task(self._start_session())
			self._starting.add_done_callback(self._start_ended)
			self._starting_since = time.monotonic()
		starting = self._starting
		wait = deadline.clamp(START_TIMEOUT_S, reserve=START_RESERVE_S) if deadline is not None and deadline.bounded else None
		try:
			await asyncio.wait_for(asyncio.shield(starting), wait)
		except TimeoutError:
			if not starting.done():
				raise RuntimeError(
					f'The browser is still starting ({time.monotonic() - self._starting_since:.0f}s so far: Chrome read off '
					'a cold disk, as after the machine boots, can take a minute). Nothing was done; call again.'
				) from None
			if starting.cancelled():
				raise RuntimeError(_START_STOPPED) from None
			starting.result()  # it ended as the wait did: up (returns), or its own error (raises)
		except asyncio.CancelledError:
			task = asyncio.current_task()
			if not starting.cancelled() or (task is not None and task.cancelling()):
				raise  # this call was cancelled, not the start
			raise RuntimeError(_START_STOPPED) from None
		finally:
			if starting.done() and self._starting is starting:
				self._starting = None  # failed and told: the next call starts afresh

	def _start_ended(self, starting: 'asyncio.Task[None]') -> None:
		if starting.cancelled():
			return
		# A failure stays for the next call to be told; a browser that is up needs no start, and one dropped since (a
		# route change) is started afresh by the next call rather than joined to this finished start
		if starting.exception() is None and self._starting is starting:
			self._starting = None

	async def _stop_starting(self) -> None:
		"""Stop a browser start in flight. Cancelling it ends the Chrome it waits on (BrowserSession.start), so this does
		not wait for the launch to finish, and nothing is left holding the profile."""
		starting, self._starting = self._starting, None
		if starting is not None and not starting.done():
			starting.cancel()
			await asyncio.gather(starting, return_exceptions=True)

	async def _drop_browser_session(self) -> None:
		await self._stop_starting()  # a start in flight is for the old route
		await self._close_clocks()
		await super()._drop_browser_session()

	async def _start_session(self) -> None:
		if not self.browser_session:
			if self.bridge:
				try:
					await self.bridge.wait_for_extension(timeout=5)
				except TimeoutError:
					from browser_use.bridge.doctor import checks, summary

					raise RuntimeError(
						"The person's browser is not connected; tell them what to do. "
						+ summary(checks(await self.bridge.status()))
					) from None
				await self._init_browser_session(allowed_domains=None, start_timeout=START_TIMEOUT_S)
			elif self.cdp_url:
				await self._init_browser_session(allowed_domains=None, cdp_url=self.cdp_url, start_timeout=START_TIMEOUT_S)
			else:
				await self._init_browser_session(start_timeout=START_TIMEOUT_S)

	@staticmethod
	def _content(percept) -> list['types.ContentBlock']:
		blocks: list[types.ContentBlock] = [types.TextContent(type='text', text=percept.text)]
		if percept.image:
			blocks.append(types.ImageContent(type='image', data=base64.b64encode(percept.image).decode(), mime_type='image/jpeg'))
		return blocks

	async def _wall_note(self) -> str:
		from browser_use.explore import walls

		assert self.browser_session is not None
		cdp = await self.browser_session.get_or_create_cdp_session(focus=False)
		r = await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': walls.PROBE_JS, 'returnByValue': True}, session_id=cdp.session_id
		)
		info = json.loads((r.get('result') or {}).get('value') or '{}')
		wall = walls.detect(**info) if info else None
		if wall:
			through_tor = ' Tor exits are widely challenged, so this is reported, not bypassed.' if self.network.uses_tor else ''
			return f'BLOCKED: {wall.kind} ({wall.evidence}). {wall.advice}.{through_tor}'
		return f'Opened "{info.get("title", "")}" at {info.get("url", "")}.'

	async def _secret_field_focused(self) -> bool:
		"""Whether the focused element is a password, card or one-time-code field (top document only)."""
		assert self.browser_session is not None
		cdp = await self.browser_session.get_or_create_cdp_session(focus=False)
		r = await cdp.cdp_client.send.Runtime.evaluate(
			params={
				'expression': "(() => { const e = document.activeElement; if (!e || e.tagName !== 'INPUT') return false;"
				" return e.type === 'password' || /password|cc-number|cc-csc|one-time-code/.test(e.autocomplete || ''); })()",
				'returnByValue': True,
			},
			session_id=cdp.session_id,
		)
		return bool((r.get('result') or {}).get('value'))

	async def _what_is_at(self, x: float, y: float, strict: bool = False) -> str | None:
		"""What a click at viewport (x, y) would land on, as role and text (`button "Save"`), or None if unknown.
		strict: let a failure to ask (such as the bridge refusing while the person holds the wheel) propagate."""
		assert self.browser_session is not None
		cdp = await self.browser_session.get_or_create_cdp_session(focus=False)
		try:
			r = await cdp.cdp_client.send.Runtime.evaluate(
				params={'expression': f'({_HIT_JS})({x}, {y})', 'returnByValue': True}, session_id=cdp.session_id
			)
		except Exception:
			if strict:
				raise
			return None  # a page that won't answer (or a bridge refusing script) leaves the click unchecked
		value = (r.get('result') or {}).get('value')
		return value if isinstance(value, str) and value else None

	async def _call_retinat(
		self, name: str, args: dict[str, Any], deadline: Deadline | None = None
	) -> str | list['types.ContentBlock']:
		from browser_use.mcp.effects import Refused, act

		if not name.startswith(TOOL_PREFIX):
			raise Refused(f'Unknown tool: {name}')
		if name == 'retinat_network':
			return await act(self._network_set(args))
		if name == 'retinat_network_status':
			return await self.network.status()
		if name in ('retinat_desktop_look', 'retinat_desktop_watch'):
			from browser_use.eyes.desktop import DesktopEyes

			if not _desktop_allowed():
				raise Refused('Desktop eyes are off on this server (start it with BROWSER_USE_DESKTOP_EYES=1).')
			# With computer use on, the eyes cover the windows of apps the person did not grant, like the actions do.
			desktop = DesktopEyes(enabled=True, mask=self._desktop_control().hide_ungranted if _control_allowed() else None)
			if name == 'retinat_desktop_look':
				seen = await desktop.look()
				if _control_allowed() and seen.image_size:
					desk = self._desktop_control()
					desk.scale = desk.x.width / seen.image_size[0]  # actions take this image's pixels
				return self._content(seen)
			return self._content(await desktop.watch(seconds=float(args.get('seconds', 8))))
		if name.startswith('retinat_desktop_'):
			return await self._call_control(name, args)
		if name == 'retinat_step' and (self.bridge is not None or self.cdp_url):
			raise Refused(_NOT_OURS)
		await self._ensure_session(deadline)
		assert self.browser_session is not None
		# in use: the idle-session cleanup shared with browser-use counted only its own calls, and closed a browser
		# used through Retinat 30 minutes after it started, mid-game
		self._update_session_activity(self.browser_session.id)
		if name in ('retinat_open', 'retinat_browse', 'retinat_next', 'retinat_explore'):
			self._motion_before = None  # another page: the last step does not run on into the next
		if self.bridge is not None and name in ACTING:
			self._check_wheel()
		if name in NEEDS_PAGE_TIME:
			clock = await self._clock(create=False)
			if clock is not None and clock.paused:
				raise Refused(_PAUSED)
		if name == 'retinat_open':
			resumed = await self._resume_paused_tab()
			if not args.get('new_tab'):
				await self._eyes()  # watch from the page's first moment: what appears right after load counts
			note = await act(self._navigate_routed(args['url'], bool(args.get('new_tab')), strict=True))
			await asyncio.sleep(1.0)
			return await self._wall_note() + resumed + note
		if name == 'retinat_explore':
			from browser_use.explore import Explorer, render_markdown, render_sheet

			resumed = await self._resume_paused_tab()
			explorer = Explorer(self.browser_session, max_pages=int(args.get('max_pages', 25)))
			report = await act(explorer.run(args['url']))
			blocks: list[types.ContentBlock] = [types.TextContent(type='text', text=render_markdown(report) + resumed)]
			sheet = render_sheet(explorer.looks)
			if sheet:
				blocks.append(types.ImageContent(type='image', data=base64.b64encode(sheet).decode(), mime_type='image/jpeg'))
			return blocks
		if name == 'retinat_record':
			return await self._record(str(args['action']), int(args.get('framerate', 8)))
		if name == 'retinat_narrate':
			text = str(args.get('text') or '').strip()
			if not text:
				raise Refused('Nothing to log: pass what the last watch result showed as text.')
			n = self._narration.take(text)
			return f'Narration of watch result {n} logged.' if n else 'Narration logged (no watch result was waiting for one).'

		if name in ('retinat_reflex', 'retinat_wait'):
			return await self._reflex(name, args, deadline)
		if name == 'retinat_step':
			return await self._step(args, deadline)
		eyes = await self._eyes()
		if name == 'retinat_console':
			from browser_use.eyes.console import render as render_console

			log = eyes.console
			since = int(args.get('since', 0))
			picked = log.entries(
				since=since, level=str(args.get('level', 'all')), pattern=str(args.get('pattern') or ''), limit=0
			)
			limit = max(1, int(args.get('limit', 30)))
			return render_console(picked[-limit:], since, log.last_seq, matched=len(picked))
		if name == 'retinat_requests':
			from browser_use.eyes.requests import render

			log = eyes.requests
			if args.get('body') is not None:
				return await log.body(int(args['body']), max_chars=int(args.get('max_chars', 3000)))
			since = int(args.get('since', 0))
			picked = log.entries(since=since, only=str(args.get('only', 'all')), pattern=str(args.get('pattern') or ''), limit=0)
			limit = max(1, int(args.get('limit', 30)))
			url = await self.browser_session.get_current_page_url()
			return render(picked[-limit:], since, log.last_seq, url, matched=len(picked))
		detail = args.get('detail', 'glance')
		if name == 'retinat_look':
			return self._content(await eyes.look(detail=detail if detail != 'glance' else 'look'))
		if name == 'retinat_watch':
			narration = str(args.get('narration') or '').strip()
			if args.get('narrate'):
				self._narration.start()
			if narration:
				self._narration.take(narration)
			elif self._narration.owes():
				raise Refused(self._narration.refusal())
			percept = await act(
				eyes.watch(
					seconds=float(args.get('seconds', 12)),
					until=args.get('until', 'bored'),
					detail=detail,
					hold=bool(args.get('hold', True)),
					deadline=deadline,
				)
			)
			covering = re.search(r'covering (\S+-\S+)', percept.text)
			# how long before the answer the last sample was taken: the percept's build time, for narration latency
			last = max((f.wall for item in percept.items for f in item.frames), default=None)
			lag = round(percept.ended_at - last, 3) if last is not None else None
			self._narration.result(covering[1] if covering else 'nothing played', lag)
			return self._content(percept)
		if name == 'retinat_scan':
			return self._content(
				await act(
					eyes.scan(
						max_screens=int(args.get('max_screens', 25)),
						keyframes=int(args.get('keyframes', 6)),
						from_screen=float(args.get('from_screen') or 0),
						deadline=deadline,
					)
				)
			)
		if name == 'retinat_browse':
			return self._content(
				await act(
					eyes.browse(
						items=int(args.get('items', 5)),
						max_seconds=float(args.get('max_seconds', 12)),
						min_seconds=float(args.get('min_seconds', 3)),
						detail=detail,
						deadline=deadline,
					)
				)
			)
		if name == 'retinat_next':
			moved = await act(eyes.next(direction=args.get('direction', 'down')))
			head = (
				f'Moved by {moved.method} in {moved.seconds:.1f}s'
				if moved.moved
				else f'The feed did not move (tried {", ".join(moved.tries)})'
			)
			return f'{head}{"; " + moved.note if moved.note else ""}. {eyes.now_line()}'
		if name == 'retinat_find':
			return self._content(await eyes.find(str(args['text']), limit=int(args.get('limit', 5))))
		if name == 'retinat_scroll':
			screens, to = args.get('screens'), str(args.get('to') or '').strip()
			if (screens is None) == (not to):
				raise Refused(
					'Give either screens (how far, negative is up) or to (a text to bring into view), not both or neither.'
				)
			return self._content(
				await act(
					eyes.scroll(
						screens=float(screens) if screens is not None else None, to=to or None, nth=int(args.get('nth', 1))
					)
				)
			)
		if name == 'retinat_zoom':
			return self._content(await eyes.zoom(float(args['x']), float(args['y']), float(args['width']), float(args['height'])))
		if name == 'retinat_tap':
			await act(eyes.tap(float(args['x']), float(args['y'])))
			return f'Tapped ({args["x"]}, {args["y"]}). {eyes.now_line()}'
		if name == 'retinat_click':
			x, y, expect = float(args['x']), float(args['y']), str(args.get('expect') or '').strip()
			there = await self._what_is_at(x, y, strict=bool(expect))
			if expect and (there is None or expect.casefold() not in there.casefold()):
				raise Refused(
					f'Not clicked: at ({args["x"]}, {args["y"]}) there is {there or "nothing the page will name"}, not '
					f'"{expect}". Look again (retinat_look, or retinat_find "{expect}") and click where it is now.'
				)
			await act(eyes.hand.click(x, y))
			return f'Clicked ({args["x"]}, {args["y"]}){" on " + there if there else ""}.'
		if name == 'retinat_swipe':
			info = await act(eyes.swipe(args.get('direction', 'up'), float(args.get('fraction', 0.55))))
			return f'Swiped {args.get("direction", "up")} {info["distance_px"]:.0f}px in {info["duration_ms"]:.0f}ms. {eyes.now_line()}'
		if name == 'retinat_type':
			if self.bridge and await self._secret_field_focused():
				raise Refused(
					"Refusing to type into a password, card or one-time-code field in the person's own browser: "
					'they enter those themselves. Ask them to fill it in, then carry on.'
				)
			if self.network.uses_tor and await self._secret_field_focused():
				raise Refused(
					'Refusing to type into a password or payment field while routed through Tor: the exit relay is '
					'on the path. Set the route to off (retinat_network), or ask the person to enter it themselves.'
				)
			await act(eyes.hand.type_text(str(args['text'])))
			return f'Typed {len(str(args["text"]))} characters.'
		if name == 'retinat_key':
			# a client with a schema from before hold_ms and with existed sends them as text
			key, hold_ms, also = str(args['key']), _as_number(args.get('hold_ms')), _as_list(args.get('with'))
			if hold_ms is None and not also:
				await act(eyes.hand.press(key))
				return f'Pressed {key}.'
			if not isinstance(also, list) or len(also) > KEY_WITH_MAX or not all(isinstance(k, str) and k for k in also):
				raise Refused(f'with is a list of up to {KEY_WITH_MAX} key names held down along with key')
			hold_ms = 100.0 if hold_ms is None else hold_ms
			if not isinstance(hold_ms, int | float):
				raise Refused(f'hold_ms is a number of milliseconds, not {hold_ms!r}')
			fits = KEY_HOLD_MAX_MS
			if deadline is not None and deadline.bounded:
				fits = min(fits, (deadline.left() - KEY_ANSWER_S) * 1000)
			if not 0 < hold_ms <= fits:
				raise Refused(f'hold_ms is how long to hold the keys down, more than 0 and at most {max(0.0, fits):.0f} ms')
			await act(eyes.hand.hold(key, hold_ms / 1000, also=also))
			return f'Held {" + ".join([*also, key])} down for {hold_ms:.0f} ms, then let go.'
		if name == 'retinat_recall':
			t0, t1 = float(args['t0']), float(args['t1'])
			if t1 < t0:
				raise Refused('t1 must be at or after t0')
			item = args.get('item')
			return self._content(
				await eyes.recall(t0, t1, frames=int(args.get('frames', 4)), item=int(item) if item is not None else None)
			)
		if name == 'retinat_search':
			item = args.get('item')
			return self._content(
				await eyes.search(
					str(args['query']), frames=int(args.get('frames', 4)), item=int(item) if item is not None else None
				)
			)
		if name == 'retinat_changes':
			from browser_use.eyes import hook

			await eyes.retina.wait_for_data(1.2)  # let the latest batch land in the journal
			eyes.note_sounds()  # the count of discrete sounds, even if no pause was seen to trigger it
			eyes.note_requests()  # a fetch refused after the page settled: no batch came along to journal it
			assert eyes.now_path is not None, 'the eyes keep no journal (now_path=False)'
			lines = hook.new_entries(eyes.now_path, limit=int(args.get('limit', 20)), reader='retinat')
			return 'Changes since last asked:\n' + '\n'.join(lines) if lines else 'No changes since last asked.'
		if name == 'retinat_now':
			await eyes.retina.wait_for_data(1.0)
			return eyes.now_line()
		raise Refused(f'Unknown tool: {name}')

	async def _record(self, action: str, framerate: int) -> str:
		"""Start or finish a screencast video of the browser. The session's own recording only closes its file when the
		browser stops, which an MCP server's browser never does while the agent works."""
		import time
		from pathlib import Path

		from browser_use.config import CONFIG
		from browser_use.mcp.effects import Refused, act

		assert action in ('start', 'stop'), action
		assert self.browser_session is not None
		recorder = self.browser_session._recording_watchdog
		if recorder is None:
			raise Refused('this browser session has no recorder')
		if action == 'stop':
			saved = await act(recorder.stop_recording())
			if saved is None:
				raise Refused('no recording is running: start one with action=start')
			size = Path(saved).stat().st_size if Path(saved).exists() else 0
			self._narration.recording('stop', str(saved))
			return f'Saved the recording: {saved} ({size / 1e6:.1f} MB).'
		folder = Path(os.environ.get('BROWSER_USE_RECORD_VIDEO_DIR') or CONFIG.BROWSER_USE_CONFIG_DIR / 'recordings').expanduser()
		folder.mkdir(parents=True, exist_ok=True)
		try:
			out = await act(
				recorder.start_recording(folder / f'retinat-{time.strftime("%Y%m%d-%H%M%S")}.mp4', framerate=framerate)
			)
		except RuntimeError as e:  # one already running, or no viewport yet
			raise Refused(str(e)) from e
		self._narration.recording('start', str(out))
		return f'Recording to {out} at {framerate} fps. Call retinat_record with action=stop to finish the file.'

	async def _reflex(self, name: str, args: dict[str, Any], deadline: Deadline | None = None) -> str:
		"""Install, clear or report the tab's reflexes, or wait for one to notify."""
		from browser_use.eyes.reflex import Reflexes
		from browser_use.mcp.effects import Refused

		assert self.browser_session is not None
		reflexes = getattr(self, '_reflexes', None)
		if reflexes is None or reflexes.session is not self.browser_session:
			reflexes = self._reflexes = Reflexes(self.browser_session)
		if name == 'retinat_wait':
			timeout = float(args.get('timeout', 30))
			if deadline is not None and deadline.bounded:
				# Within the call's budget: opening the browser first spends the same time the client counts
				timeout = max(0.1, min(timeout, deadline.left() - 0.5))
			if not reflexes.installed:
				# A plain wait, as for a loader to finish (agents reached for this and were refused)
				await asyncio.sleep(timeout)
				return f'Waited {timeout:.1f}s; nothing was watched (no reflex rules are installed: retinat_reflex adds some).'
			event = await reflexes.wait(timeout=timeout)
			stats = reflexes.stats()
			done = f'{stats["actions"]} input actions so far, decision to input p50 {stats["latency_ms_p50"]} ms'
			if event is None:
				return f'Nothing notified in {timeout:.0f}s. The reflexes keep running: {done}.'
			at = f', video at {event.media_t:.2f}s' if event.media_t is not None else ''
			return f'"{event.label}" (rule {event.rule}) fired at page time {event.page_ms / 1000:.2f}s{at}. {done}.'
		action = str(args.get('action') or 'install')
		if action == 'clear':
			await reflexes.clear()
			return 'Reflexes stopped; held keys released.'
		if action == 'status':
			return json.dumps(reflexes.stats())
		rules = args.get('rules') or []
		if not rules:
			raise Refused('give rules to install (or action=clear / status)')
		try:
			count = await reflexes.install(rules, screen_width=int(args.get('screen_width') or 240))
		except (ValueError, AssertionError) as e:
			raise Refused(str(e)) from e
		return f'{count} reflex rule(s) running every frame on this tab. retinat_wait sleeps until one notifies.'

	async def _step(self, args: dict[str, Any], deadline: Deadline | None = None) -> str | list['types.ContentBlock']:
		"""Pause, step or resume the focused tab's clock: the model plays a real-time page turn by turn."""
		import io

		from PIL import Image

		from browser_use.eyes.clock import FPS_MIN, FRAME_MS, STEP_MAX_MS
		from browser_use.mcp.effects import Refused, act

		action = str(args.get('action') or 'step')
		if action == 'resume':
			clock = await self._clock(create=False)
			if clock is None or not clock.paused:
				return 'The page already runs on real time.'
			await clock.resume()
			return 'The page runs on real time again (Retinat moves its clock with the real one from now on).'
		if action not in ('step', 'pause'):
			raise Refused(f'action is step, pause or resume, not {action!r}')
		# all checked before the page is paused: a pause cannot be taken back
		ms = float(args.get('ms', 250))
		if not FRAME_MS <= ms <= STEP_MAX_MS:
			raise Refused(f'ms is game time to play, {FRAME_MS:.0f}-{STEP_MAX_MS:.0f}')
		keys = {'hold': args.get('hold') or [], 'tap': args.get('tap') or []}
		for field, value in keys.items():
			if not isinstance(value, list) or not all(isinstance(k, str) and k for k in value):
				raise Refused(f'{field} is a list of key names, like ["ArrowLeft"]')
		fps = float(args.get('fps') or 60)
		if not FPS_MIN <= fps <= 60:
			raise Refused(f'fps is frames drawn per second of game time, {FPS_MIN:.0f}-60')
		frame = args.get('frame') or 'page'
		if not (
			frame in ('page', 'game')
			or (isinstance(frame, list) and len(frame) == 4 and all(isinstance(v, int | float) for v in frame))
		):
			raise Refused(
				'frame is "page", "game" (the largest canvas, frame or video in view), or [x, y, width, height] in CSS px'
			)
		motion = args.get('motion', True)
		if not isinstance(motion, bool):
			raise Refused('motion is true (the eyes say what moved during the step) or false')
		clock = await self._clock()
		assert clock is not None
		if not clock.paused:
			await act(clock.pause())
		if action == 'pause':
			return 'Paused: the page stands still until a step plays it, or action=resume.'
		# a step is played at about real speed: within the call it plays what fits and keeps time to answer
		stop_at = deadline.at - STEP_ANSWER_S if deadline is not None and deadline.bounded else None
		charge = bool(args.get('charge_thinking'))
		region = await self._game_region() if frame == 'game' else (frame if isinstance(frame, list) else None)
		eyes = await self._eyes()
		looks: list[tuple[float, Any]] = []  # (game ms into the step, grey picture): what the eyes saw during it
		seen: Callable[[int, float], Awaitable[None]] | None = None
		live = None
		drawn: list[tuple[float, float]] = []  # the stream's clock as each of the step's frames was drawn, its game ms
		began = 0.0
		if motion:
			view = await self._viewport_area()
			area = region or view
			try:  # the eyes watch the compositor's stream of what the page draws, as they watch any video
				watcher = eyes._page_watcher()
				# wide enough that the region comes through at least MOTION_WIDTH px wide (a 600 px game was 200 px of
				# a 640 px stream, and its motion read wrong)
				await watcher.start(min_width=min(round(view[2]), math.ceil(view[2] * MOTION_WIDTH / max(1.0, area[2]))))
				live = watcher.live
			except Exception as e:
				assert self.browser_session is not None
				self.browser_session.logger.debug(f'retinat_step: no stream to watch the step on: {type(e).__name__}: {e}')
			if live is not None:
				began = live.clock()
				stream = live

				async def note(i: int, game_ms: float) -> None:
					drawn.append((stream.clock(), game_ms))

				seen = note
			else:
				total = max(1, round(ms / (1000 / fps)))
				picks = {round(k * (total - 1) / (MOTION_LOOKS - 1)) for k in range(MOTION_LOOKS)} if total > 1 else {0}

				async def look_at(i: int, game_ms: float) -> None:
					if i in picks:
						looks.append((game_ms, await self._grey_look(area)))

				seen = look_at
				looks.append((0.0, await self._grey_look(area)))  # as it stands before the step
		moved = clock.moved_ms
		stepped = await act(
			clock.step(ms, hold=keys['hold'], tap=keys['tap'], charge_thinking=charge, stop_at=stop_at, fps=fps, seen=seen)
		)
		if motion and live is not None:
			await asyncio.sleep(MOTION_SETTLE_S)  # the stream's last frames of the step arrive
			looks = self._stream_looks(live, began, drawn, area, view)
		elif motion and looks[-1][0] < stepped.game_ms:  # the step ended early: the eyes see where it ended too
			looks.append((stepped.game_ms, await self._grey_look(area)))
		if motion:
			looks = self._run_on(clock, moved, tuple(area), looks, stepped.charged_ms)
		cut = f' of the {ms:.0f} asked (the call had no time for more)' if stepped.game_ms < ms - FRAME_MS else ''
		thought = f'First played your {stepped.charged_ms / 1000:.1f}s of thinking with the last keys held. ' if charge else ''
		held = f'holding {", ".join(stepped.held)}' if stepped.held else 'no keys held'
		text = (
			f'{thought}Played {stepped.game_ms:.0f} ms{cut} ({stepped.frames} frames) in {stepped.real_s:.1f}s ({stepped.draw_s:.1f}s '
			f'waiting for the page to draw, {stepped.clock_s:.1f}s for its clock); paused again, '
			f'{held}. Turn-based so far: {clock.played_ms / 1000:.1f}s of game time took {clock.slowdown:.0f}x as long '
			'in real time.'
		)
		if region is not None:
			x, y, w, h = (float(v) for v in region)
			zoomed = await eyes.zoom(x, y, w, h, max_width=STEP_GAME_WIDTH, max_height=STEP_GAME_WIDTH)
			jpeg = zoomed.image
			if not jpeg:
				return text + ' No frame could be captured.'
			size = zoomed.image_size or (0, 0)
			if motion:
				jpeg, said = self._motion_on(jpeg, looks, size[0])
				text += ' ' + said
			where = "the game's frame" if frame == 'game' else 'the region asked'
			text += (
				f' The frame it ended on, {where} only: ({x:.0f}, {y:.0f}) {w:.0f}x{h:.0f} CSS px, drawn afresh at '
				f'{size[0]}x{size[1]}: to click, multiply its pixels by {w / max(1, size[0]):.2f} and add ({x:.0f}, {y:.0f}).'
			)
		else:
			if frame == 'game':
				text += ' (No canvas, frame or video was in view: the whole page is shown.)'
			jpeg, how = await eyes._page_now()
			if not jpeg:
				return text + ' No frame could be captured.'
			with Image.open(io.BytesIO(jpeg)) as img:
				size = img.size
			text += f' The frame it ended on ({how}), {size[0]}x{size[1]}{eyes._click_scale(size)}.'
			if motion:
				jpeg, said = self._motion_on(jpeg, looks, size[0])
				text += ' ' + said
		return [
			types.TextContent(type='text', text=text),
			types.ImageContent(type='image', data=base64.b64encode(jpeg).decode(), mime_type='image/jpeg'),
		]

	async def _viewport_area(self) -> list[float]:
		"""The whole viewport, in CSS px, as a region."""
		assert self.browser_session is not None
		cdp = await self.browser_session.get_or_create_cdp_session(focus=False)
		metrics = await cdp.cdp_client.send.Page.getLayoutMetrics(session_id=cdp.session_id)
		view = metrics['cssLayoutViewport']
		return [0.0, 0.0, float(view['clientWidth']), float(view['clientHeight'])]

	def _stream_looks(
		self, live: Any, began: float, drawn: list[tuple[float, float]], area: list[float], view: list[float]
	) -> list[tuple[float, Any]]:
		"""The step as the compositor's stream showed it: the newest frame shown before it (game time 0), then those
		shown during it, each at the game time of the last of the step's frames drawn before Chrome showed it (the
		stream runs a frame or two behind the drawing, so a frame's time is good to a frame or so; the first and last
		are exact, the page standing still before and after a step), the latest frame for each time kept, each cut to
		`area` and made grey, MOTION_WIDTH px wide. Only frames of the stream's own size (the most common) are read: a
		capture taken meanwhile shows up in the stream too, of the part of the page it took and at its own size."""
		import io
		from collections import Counter

		import numpy as np
		from PIL import Image

		def size(data: bytes) -> tuple[int, int]:
			with Image.open(io.BytesIO(data)) as img:  # reads the header only
				return img.size

		frames = [(f, size(f.data)) for f in live.frames]
		own = Counter(s for _, s in frames).most_common(1)[0][0] if frames else (0, 0)
		shown = sorted(
			((f.swapped_at if f.swapped_at is not None else f.at, f) for f, s in frames if s == own), key=lambda pair: pair[0]
		)
		before = [f for at, f in shown if at < began]
		first_ms = drawn[0][1] if drawn else 0.0
		by_time: dict[float, Any] = {0.0: before[-1]} if before else {}
		for at, f in shown:
			if at >= began:
				by_time[max([first_ms] + [g for drawn_at, g in drawn if drawn_at <= at])] = f
		x, y, w, h = area
		looks = []
		for game_ms in sorted(by_time)[-MOTION_LOOKS - 1 :]:
			with Image.open(io.BytesIO(by_time[game_ms].data)) as img:
				scale = img.width / max(1.0, view[2])
				grey = img.convert('L').crop((round(x * scale), round(y * scale), round((x + w) * scale), round((y + h) * scale)))
				grey = grey.resize((MOTION_WIDTH, max(1, round(h * MOTION_WIDTH / max(1.0, w)))))
				looks.append((game_ms, np.asarray(grey)))
		return looks

	async def _grey_look(self, area: list[float]) -> Any:
		"""`area` (viewport CSS px) as the compositor draws it now, grey, MOTION_WIDTH px wide: what the eyes read
		movement from."""
		import io

		import numpy as np
		from PIL import Image

		from browser_use.browser.capture import capture_screenshot

		assert self.browser_session is not None
		x, y, w, h = area
		cdp = await self.browser_session.get_or_create_cdp_session(focus=False)
		clip = {'x': x, 'y': y, 'width': w, 'height': h, 'scale': min(1.0, MOTION_WIDTH / max(1.0, w))}
		shot = await capture_screenshot(cdp.cdp_client, {'format': 'jpeg', 'quality': 85, 'clip': clip}, cdp.session_id)
		with Image.open(io.BytesIO(base64.b64decode(shot['data']))) as img:
			grey = img.convert('L')
			if grey.width != MOTION_WIDTH:  # the same size every time, whatever the capture came back at
				grey = grey.resize((MOTION_WIDTH, max(1, round(grey.height * MOTION_WIDTH / grey.width))))
			return np.asarray(grey)

	def _run_on(
		self, clock: Any, moved: float, area: tuple[float, ...], looks: list[tuple[float, Any]], charged_ms: float
	) -> list[tuple[float, Any]]:
		"""The step's looks with the last MOTION_BEFORE_MS of the step before in front (at game times before 0), when
		game time has not moved between the two on the same clock and region; kept for the next step. Thinking played
		at the start of this step (`charged_ms`) is a gap between them, unseen."""
		before, self._motion_before = self._motion_before, None
		if looks:
			self._motion_before = (clock, clock.moved_ms, area, [(t, p) for t, p in looks if t >= 0])
		if before is None or before[0] is not clock or before[1] != moved or before[2] != area or not looks:
			return looks
		earlier = before[3]
		if not earlier or earlier[0][1].shape != looks[0][1].shape:
			return looks
		end = earlier[-1][0] + charged_ms
		# the step before's last look is this step's first, the page standing still between
		ran = [(t - end, p) for t, p in earlier[:-1] if t - end >= -MOTION_BEFORE_MS]
		return ran + looks

	def _motion_on(self, jpeg: bytes, looks: list[tuple[float, Any]], picture_width: int) -> tuple[bytes, str]:
		"""What moved during the step, read by the eyes from `looks`, drawn on the picture (`jpeg`) and said in words."""
		import io

		from PIL import Image

		from browser_use.eyes import moving

		movement = moving.read(looks)
		with Image.open(io.BytesIO(jpeg)) as img:
			picture = img.convert('RGB')
		moving.draw(picture, movement, picture_width / MOTION_WIDTH)
		out = io.BytesIO()
		picture.save(out, format='JPEG', quality=85)
		before_ms = max(0.0, -looks[0][0]) if looks else 0.0
		return out.getvalue(), moving.describe(movement, picture_width / MOTION_WIDTH, before_ms=before_ms)

	async def _game_region(self) -> list[float] | None:
		"""The largest canvas, frame or video in view, in viewport CSS px: where a game is drawn."""
		assert self.browser_session is not None
		cdp = await self.browser_session.get_or_create_cdp_session(focus=False)
		r = await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': _GAME_REGION_JS, 'returnByValue': True}, session_id=cdp.session_id
		)
		value = (r.get('result') or {}).get('value')
		return [float(v) for v in value] if isinstance(value, list) and len(value) == 4 else None

	async def _resume_paused_tab(self) -> str:
		"""Give a tab paused by retinat_step its clock back before it is navigated (a paused page commits no new one),
		and a note saying so."""
		clock = await self._clock(create=False)
		if clock is None or not clock.virtual:
			return ''
		was_paused = clock.paused
		await clock.resume()  # also starts the pump again if it stopped
		return ' (the page was paused by retinat_step: it runs on real time again)' if was_paused else ''

	async def _clock(self, create: bool = True) -> 'PageClock | None':
		"""The focused tab's clock, made on first use."""
		from browser_use.eyes.clock import PageClock

		assert self.browser_session is not None
		cdp = await self.browser_session.get_or_create_cdp_session(focus=False)
		clock = self._clocks.get(cdp.target_id)
		if clock is not None and clock.session is not self.browser_session:
			await clock.close()
			clock = None
		if clock is None and create:
			clock = self._clocks[cdp.target_id] = PageClock(self.browser_session)
		return clock

	async def _close_clocks(self) -> None:
		clocks, self._clocks = list(self._clocks.values()), {}
		for clock in clocks:
			await clock.close()

	async def _close_all_sessions(self) -> str:
		try:
			await self._stop_starting()
			await self._close_clocks()
		finally:
			if self._control is not None:
				self._control.close()
				self._control = None
		return await super()._close_all_sessions()

	def _desktop_control(self) -> 'DesktopControl':
		if self._control is None:
			from browser_use.desktop.service import DesktopControl

			self._control = DesktopControl(enabled=True)
		return self._control

	async def _call_control(self, name: str, args: dict[str, Any]) -> str | list['types.ContentBlock']:
		"""Computer use on the X desktop (browser_use/desktop): only apps the person granted, never over their hands."""
		from browser_use.mcp.effects import Refused, act

		if not _control_allowed():
			raise Refused('Desktop control is off on this server (start it with BROWSER_USE_DESKTOP_CONTROL=1).')
		desk = self._desktop_control()
		if name == 'retinat_desktop_status':
			return desk.status()
		if name == 'retinat_desktop_request_access':
			from browser_use.desktop import consent
			from browser_use.desktop.service import parse_grants

			wanted = parse_grants(str(args['apps']))
			allowed = await act(consent.ask(desk.display, wanted, str(args.get('reason') or '')))
			desk.grant(allowed)
			if allowed:
				desk.resume()  # an approval is the person's go-ahead, also after an Escape
			return consent.describe(wanted, allowed)
		if name == 'retinat_desktop_zoom':
			png = await desk.zoom(float(args['x']), float(args['y']), float(args['width']), float(args['height']))
			return [types.ImageContent(type='image', data=base64.b64encode(png).decode(), mime_type='image/png')]
		if name == 'retinat_desktop_click':
			return await act(
				desk.click(
					float(args['x']),
					float(args['y']),
					str(args.get('button', 'left')),
					int(args.get('count', 1)),
					str(args.get('expect') or '').strip(),
				)
			)
		if name == 'retinat_desktop_move':
			return await act(desk.move(float(args['x']), float(args['y'])))
		expect = str(args.get('expect') or '').strip()
		if name == 'retinat_desktop_type':
			return await act(desk.type_text(str(args['text']), expect=expect))
		if name == 'retinat_desktop_key':
			return await act(desk.key(str(args['keys']), expect=expect))
		if name == 'retinat_desktop_scroll':
			return await act(
				desk.scroll(float(args['x']), float(args['y']), str(args.get('direction', 'down')), int(args.get('amount', 3)))
			)
		if name == 'retinat_desktop_drag':
			return await act(desk.drag(float(args['x1']), float(args['y1']), float(args['x2']), float(args['y2'])))
		raise Refused(f'Unknown tool: {name}')

	def _check_wheel(self) -> None:
		"""In the person's browser, refuse before sending anything while they hold the wheel or have stopped the AI,
		so the failure is known to have done nothing (the relay would refuse the first event anyway)."""
		from browser_use.bridge.policy import HOLDING, STOPPED
		from browser_use.mcp.effects import Refused

		assert self.bridge is not None
		if self.bridge.stopped:
			raise Refused(f'Not done: {STOPPED}.')
		if self.bridge.human_driving:
			raise Refused(f'Not done: {HOLDING}.')


async def main(cdp_url: str | None = None, network: NetworkRouter | None = None, bridge_port: int | None = None) -> None:
	if not MCP_AVAILABLE:
		print('MCP SDK is required: pip install mcp', file=sys.stderr)
		sys.exit(1)
	bridge = None
	if bridge_port is not None:
		from browser_use.bridge import EXTENSION_DIR, BridgeRelay

		bridge = await BridgeRelay(port=bridge_port).start()
		print(f'Retinat bridge on {bridge.cdp_url}; load the extension from {EXTENSION_DIR} and share a tab.', file=sys.stderr)
	server = RetinatServer(cdp_url=cdp_url, network=network, bridge=bridge)
	await serve_until_stopped(server)  # closes the bridge and Tor with the browsers


def cli() -> None:
	"""The process starts from the front, which answers the handshake while this module loads."""
	from browser_use.retinat.front import cli as front_cli

	front_cli()


if __name__ == '__main__':
	cli()
