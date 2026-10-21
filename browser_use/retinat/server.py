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
import os
import sys
import time
from typing import TYPE_CHECKING, Any

from browser_use.eyes.views import CALL_BUDGET_S, Deadline
from browser_use.mcp.server import MCP_AVAILABLE, TEXT_FAILURES, BrowserUseServer, serve_until_stopped, types
from browser_use.net import NetworkMode, NetworkRouter
from browser_use.retinat.front import CALL_STARTED
from browser_use.retinat.tools import (  # noqa: F401  (the tool lists are imported from here too)
	_browser_tools,
	_control_allowed,
	_control_tools,
	_desktop_allowed,
	_desktop_tools,
	_tools,
)

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

# Tools that can run long (a count of screens, items, pages or seconds): each is held to the call's budget, and a hard
# stop BACKSTOP_GRACE_S after it if it does not stop by itself (the client's 60 s minus room for the answer)
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
		long_tools: frozenset[str] = LONG_TOOLS,
	) -> None:
		# What one tool call may spend before it answers with what it has: MCP clients drop a call at 60 s
		self.call_budget_s = float(call_budget_s or os.environ.get('BROWSER_USE_MCP_CALL_BUDGET_S') or CALL_BUDGET_S)
		assert self.call_budget_s > 0, 'a call budget is some seconds'
		# The tools held to the budget by a hard stop, and how long after the budget it comes
		self.backstop_grace_s = backstop_grace_s
		self.long_tools = long_tools
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
				# A last resort for a long tool that did not stop in time by itself: it loses what the tool had, so the
				# tools stop on their deadline first; this only keeps the server from working on after the client left
				limit = deadline.left() + self.backstop_grace_s if params.name in self.long_tools else None
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
			return self._content(
				await act(
					eyes.watch(
						seconds=float(args.get('seconds', 12)),
						until=args.get('until', 'bored'),
						detail=detail,
						hold=bool(args.get('hold', True)),
						deadline=deadline,
					)
				)
			)
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
			await act(eyes.hand.press(str(args['key'])))
			return f'Pressed {args["key"]}.'
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
		from browser_use.mcp.effects import Refused

		assert action in ('start', 'stop'), action
		assert self.browser_session is not None
		recorder = self.browser_session._recording_watchdog
		if recorder is None:
			raise Refused('this browser session has no recorder')
		if action == 'stop':
			saved = await recorder.stop_recording()
			if saved is None:
				raise Refused('no recording is running: start one with action=start')
			size = Path(saved).stat().st_size if Path(saved).exists() else 0
			return f'Saved the recording: {saved} ({size / 1e6:.1f} MB).'
		folder = Path(os.environ.get('BROWSER_USE_RECORD_VIDEO_DIR') or CONFIG.BROWSER_USE_CONFIG_DIR / 'recordings').expanduser()
		folder.mkdir(parents=True, exist_ok=True)
		try:
			out = await recorder.start_recording(folder / f'retinat-{time.strftime("%Y%m%d-%H%M%S")}.mp4', framerate=framerate)
		except RuntimeError as e:  # one already running, or no viewport yet
			raise Refused(str(e)) from e
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
			if not reflexes.installed:
				raise Refused('no reflexes are installed: install rules with retinat_reflex first')
			timeout = float(args.get('timeout', 30))
			if deadline is not None and deadline.bounded:
				# Within the call's budget: opening the browser first spends the same time the client counts
				timeout = max(0.1, min(timeout, deadline.left() - 0.5))
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

		from browser_use.eyes.clock import FRAME_MS, STEP_MAX_MS
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
		clock = await self._clock()
		assert clock is not None
		if not clock.paused:
			await act(clock.pause())
		if action == 'pause':
			return 'Paused: the page stands still until a step plays it, or action=resume.'
		# a step is played at about real speed: within the call it plays what fits and keeps time to answer
		stop_at = deadline.at - STEP_ANSWER_S if deadline is not None and deadline.bounded else None
		charge = bool(args.get('charge_thinking'))
		stepped = await act(clock.step(ms, hold=keys['hold'], tap=keys['tap'], charge_thinking=charge, stop_at=stop_at))
		eyes = await self._eyes()
		jpeg, how = await eyes._page_now()
		cut = f' of the {ms:.0f} asked (the call had no time for more)' if stepped.game_ms < ms - FRAME_MS else ''
		thought = f'First played your {stepped.charged_ms / 1000:.1f}s of thinking with the last keys held. ' if charge else ''
		held = f'holding {", ".join(stepped.held)}' if stepped.held else 'no keys held'
		text = (
			f'{thought}Played {stepped.game_ms:.0f} ms{cut} ({stepped.frames} frames) in {stepped.real_s:.1f}s ({stepped.draw_s:.1f}s '
			f'waiting for the page to draw, {stepped.clock_s:.1f}s for its clock); paused again, '
			f'{held}. Turn-based so far: {clock.played_ms / 1000:.1f}s of game time took {clock.slowdown:.0f}x as long '
			'in real time.'
		)
		if not jpeg:
			return text + ' No frame could be captured.'
		with Image.open(io.BytesIO(jpeg)) as img:
			size = img.size
		text += f' The frame it ended on ({how}), {size[0]}x{size[1]}{eyes._click_scale(size)}.'
		return [
			types.TextContent(type='text', text=text),
			types.ImageContent(type='image', data=base64.b64encode(jpeg).decode(), mime_type='image/jpeg'),
		]

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
