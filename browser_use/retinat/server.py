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

It never runs page scripts on the model's behalf and has no Playwright anywhere. Bot walls are
reported as walls. Run it with `python -m browser_use.retinat` (or `retinat`), add `--cdp-url`
to attach to a Chrome you started yourself (your profile, your logins, your connection).

It reuses browser-use's MCP session management and configuration, so the two servers read the
same `~/.config/browseruse` settings; they run separate browsers unless both attach to the same
Chrome with `--cdp-url`.
"""

import argparse
import asyncio
import base64
import json
import os
import sys
from typing import Any

from browser_use.mcp.server import MCP_AVAILABLE, BrowserUseServer, types
from browser_use.net import NetworkMode, NetworkRouter

TOOL_PREFIX = 'retinat_'

_DETAIL = {
	'type': 'string',
	'enum': ['glance', 'look', 'study'],
	'default': 'glance',
	'description': 'Keyframe size on the sheet: glance (cheapest), look, study.',
}


def _tools() -> list['types.Tool']:
	ro = types.ToolAnnotations(read_only_hint=True)
	return [
		types.Tool(
			name='retinat_open',
			description='Open a URL (optionally in a new tab). Says plainly if the site answered with a bot wall instead of the page.',
			input_schema={
				'type': 'object',
				'properties': {'url': {'type': 'string'}, 'new_tab': {'type': 'boolean', 'default': False}},
				'required': ['url'],
			},
		),
		types.Tool(
			name='retinat_look',
			description='What is on screen right now, as an image: a playing video briefly watched, otherwise the page as drawn (canvas/WebGL included). One frame, a few hundred tokens.',
			input_schema={'type': 'object', 'properties': {'detail': _DETAIL}},
			annotations=ro,
		),
		types.Tool(
			name='retinat_watch',
			description=(
				'Watch the playing video from its own frames and sound (muted or not) and return one sheet: keyframes '
				'covering every shot plus a spectrogram strip, with a timeline of cuts, sound (speech, music, beats, '
				'silence...), tempo, loops, on-screen caption and a transcript when the speech extra is installed. '
				'until=bored stops once nothing new is happening; event stops at the first cut or change in sound.'
			),
			input_schema={
				'type': 'object',
				'properties': {
					'seconds': {'type': 'number', 'default': 12, 'minimum': 1, 'maximum': 90},
					'until': {'type': 'string', 'enum': ['bored', 'event', 'time', 'item'], 'default': 'bored'},
					'detail': _DETAIL,
					'hold': {'type': 'boolean', 'default': True, 'description': 'Pause when done so nothing plays unseen.'},
				},
			},
		),
		types.Tool(
			name='retinat_scan',
			description='Scroll the whole page top to bottom like a reader while watching what is drawn, and return the few frames that cover everything seen (one sheet), plus where things animate on their own. Use for canvas/WebGL/scroll-driven pages where the DOM says little.',
			input_schema={
				'type': 'object',
				'properties': {
					'max_screens': {'type': 'integer', 'default': 25, 'minimum': 1, 'maximum': 80},
					'keyframes': {'type': 'integer', 'default': 6, 'minimum': 1, 'maximum': 16},
				},
			},
		),
		types.Tool(
			name='retinat_browse',
			description='Scroll a short-video feed like a person: watch each item until nothing new happens, flick to the next with a real touch swipe (confirmed by sight), repeat. One sheet, a row per item.',
			input_schema={
				'type': 'object',
				'properties': {
					'items': {'type': 'integer', 'default': 5, 'minimum': 1, 'maximum': 30},
					'max_seconds': {'type': 'number', 'default': 12, 'minimum': 2, 'maximum': 90},
					'min_seconds': {'type': 'number', 'default': 3, 'minimum': 0.5, 'maximum': 30},
					'detail': _DETAIL,
				},
			},
		),
		types.Tool(
			name='retinat_next',
			description='Move a feed to the next (or previous) item with a thumb flick; falls back to a longer flick, the wheel, then the keyboard; confirms by sight and corrects an overshoot.',
			input_schema={
				'type': 'object',
				'properties': {'direction': {'type': 'string', 'enum': ['down', 'up'], 'default': 'down'}},
			},
		),
		types.Tool(
			name='retinat_tap',
			description='Tap at viewport coordinates (CSS px, as on the look/scan images) with a real touch event.',
			input_schema={
				'type': 'object',
				'properties': {'x': {'type': 'number'}, 'y': {'type': 'number'}},
				'required': ['x', 'y'],
			},
		),
		types.Tool(
			name='retinat_click',
			description='Move the mouse along a human path to viewport coordinates and click (for desktop pages).',
			input_schema={
				'type': 'object',
				'properties': {'x': {'type': 'number'}, 'y': {'type': 'number'}},
				'required': ['x', 'y'],
			},
		),
		types.Tool(
			name='retinat_swipe',
			description='A thumb swipe across part of the viewport. up moves content up.',
			input_schema={
				'type': 'object',
				'properties': {
					'direction': {'type': 'string', 'enum': ['up', 'down', 'left', 'right'], 'default': 'up'},
					'fraction': {'type': 'number', 'default': 0.55, 'minimum': 0.05, 'maximum': 0.9},
				},
			},
		),
		types.Tool(
			name='retinat_type',
			description='Type text into whatever has focus, key by key with human timing (tap or click the field first).',
			input_schema={'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']},
		),
		types.Tool(
			name='retinat_key',
			description='Press one named key: Enter, Tab, Escape, Backspace, ArrowUp/Down/Left/Right, PageUp/Down, Home, End, Space.',
			input_schema={'type': 'object', 'properties': {'key': {'type': 'string'}}, 'required': ['key']},
		),
		types.Tool(
			name='retinat_now',
			description='One line on what is on screen and audible right now. No image; nearly free.',
			input_schema={'type': 'object', 'properties': {}},
			annotations=ro,
		),
		types.Tool(
			name='retinat_explore',
			description='Explore a whole site (its links and sitemap, robots.txt obeyed): load and scroll every page, check it (console errors, broken requests/images/links, accessibility names, phone-width overflow, speed), and return a bug report plus a sheet of every page. Never submits forms.',
			input_schema={
				'type': 'object',
				'properties': {
					'url': {'type': 'string'},
					'max_pages': {'type': 'integer', 'default': 25, 'minimum': 1, 'maximum': 100},
				},
				'required': ['url'],
			},
		),
	]


class RetinatServer(BrowserUseServer):
	"""browser-use's MCP session handling, with a vision-first tool surface."""

	def __init__(
		self, cdp_url: str | None = None, session_timeout_minutes: int = 30, network: NetworkRouter | None = None
	) -> None:
		# Agents get `auto` unless told otherwise: direct first, Tor only after a network failure or a
		# geo-block, never for a bot wall. `--network off` (or RETINAT/BROWSER_USE env) turns it off.
		super().__init__(
			session_timeout_minutes=session_timeout_minutes,
			network=network or NetworkRouter.from_env(default=NetworkMode.AUTO),
		)
		from mcp.server import Server

		from browser_use.utils import get_browser_use_version

		self.cdp_url = cdp_url
		self.server = Server('retinat', version=get_browser_use_version())
		self._setup_retinat_handlers()

	def _setup_retinat_handlers(self) -> None:
		async def list_tools(_context: Any, _params: 'types.PaginatedRequestParams') -> 'types.ListToolsResult':
			return types.ListToolsResult(tools=[*_tools(), *self._network_tool_entries('retinat')])

		async def call_tool(_context: Any, params: 'types.CallToolRequestParams') -> 'types.CallToolResult':
			try:
				result = await self._call_retinat(params.name, params.arguments or {})
				content: list[types.ContentBlock] = (
					result if isinstance(result, list) else [types.TextContent(type='text', text=result)]
				)
				return types.CallToolResult(content=content)
			except Exception as e:
				return types.CallToolResult(content=[types.TextContent(type='text', text=f'Error: {e}')], is_error=True)

		async def empty_resources(_context: Any, _params: 'types.PaginatedRequestParams') -> 'types.ListResourcesResult':
			return types.ListResourcesResult(resources=[])

		async def empty_prompts(_context: Any, _params: 'types.PaginatedRequestParams') -> 'types.ListPromptsResult':
			return types.ListPromptsResult(prompts=[])

		self.server.add_request_handler('tools/list', types.PaginatedRequestParams, list_tools)
		self.server.add_request_handler('tools/call', types.CallToolRequestParams, call_tool)
		self.server.add_request_handler('resources/list', types.PaginatedRequestParams, empty_resources)
		self.server.add_request_handler('prompts/list', types.PaginatedRequestParams, empty_prompts)

	async def _ensure_session(self) -> None:
		if not self.browser_session:
			if self.cdp_url:
				await self._init_browser_session(allowed_domains=None, cdp_url=self.cdp_url)
			else:
				await self._init_browser_session()

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

	async def _call_retinat(self, name: str, args: dict[str, Any]) -> str | list['types.ContentBlock']:
		if not name.startswith(TOOL_PREFIX):
			raise ValueError(f'Unknown tool: {name}')
		if name == 'retinat_network':
			return await self._network_set(args)
		if name == 'retinat_network_status':
			return await self.network.status()
		await self._ensure_session()
		assert self.browser_session is not None
		if name == 'retinat_open':
			note = await self._navigate_routed(args['url'], bool(args.get('new_tab')), strict=True)
			await asyncio.sleep(1.0)
			return await self._wall_note() + note
		if name == 'retinat_explore':
			from browser_use.explore import Explorer, render_markdown, render_sheet

			explorer = Explorer(self.browser_session, max_pages=int(args.get('max_pages', 25)))
			report = await explorer.run(args['url'])
			blocks: list[types.ContentBlock] = [types.TextContent(type='text', text=render_markdown(report))]
			sheet = render_sheet(explorer.looks)
			if sheet:
				blocks.append(types.ImageContent(type='image', data=base64.b64encode(sheet).decode(), mime_type='image/jpeg'))
			return blocks
		eyes = await self._eyes()
		detail = args.get('detail', 'glance')
		if name == 'retinat_look':
			return self._content(await eyes.look(detail=detail if detail != 'glance' else 'look'))
		if name == 'retinat_watch':
			return self._content(
				await eyes.watch(
					seconds=float(args.get('seconds', 12)),
					until=args.get('until', 'bored'),
					detail=detail,
					hold=bool(args.get('hold', True)),
				)
			)
		if name == 'retinat_scan':
			return self._content(
				await eyes.scan(max_screens=int(args.get('max_screens', 25)), keyframes=int(args.get('keyframes', 6)))
			)
		if name == 'retinat_browse':
			return self._content(
				await eyes.browse(
					items=int(args.get('items', 5)),
					max_seconds=float(args.get('max_seconds', 12)),
					min_seconds=float(args.get('min_seconds', 3)),
					detail=detail,
				)
			)
		if name == 'retinat_next':
			moved = await eyes.next(direction=args.get('direction', 'down'))
			head = (
				f'Moved by {moved.method} in {moved.seconds:.1f}s'
				if moved.moved
				else f'The feed did not move (tried {", ".join(moved.tries)})'
			)
			return f'{head}{"; " + moved.note if moved.note else ""}. {eyes.now_line()}'
		if name == 'retinat_tap':
			await eyes.tap(float(args['x']), float(args['y']))
			return f'Tapped ({args["x"]}, {args["y"]}). {eyes.now_line()}'
		if name == 'retinat_click':
			await eyes.hand.click(float(args['x']), float(args['y']))
			return f'Clicked ({args["x"]}, {args["y"]}).'
		if name == 'retinat_swipe':
			info = await eyes.swipe(args.get('direction', 'up'), float(args.get('fraction', 0.55)))
			return f'Swiped {args.get("direction", "up")} {info["distance_px"]:.0f}px in {info["duration_ms"]:.0f}ms. {eyes.now_line()}'
		if name == 'retinat_type':
			await eyes.hand.type_text(str(args['text']))
			return f'Typed {len(str(args["text"]))} characters.'
		if name == 'retinat_key':
			await eyes.hand.press(str(args['key']))
			return f'Pressed {args["key"]}.'
		if name == 'retinat_now':
			await eyes.retina.wait_for_data(1.0)
			return eyes.now_line()
		raise ValueError(f'Unknown tool: {name}')


async def main(cdp_url: str | None = None, network: NetworkRouter | None = None) -> None:
	if not MCP_AVAILABLE:
		print('MCP SDK is required: pip install mcp', file=sys.stderr)
		sys.exit(1)
	server = RetinatServer(cdp_url=cdp_url, network=network)
	await server.run()


def cli() -> None:
	parser = argparse.ArgumentParser(
		prog='retinat', description='Retinat MCP server: continuous image/video perception for LLMs (stdio).'
	)
	parser.add_argument(
		'--cdp-url', default=os.environ.get('RETINAT_CDP_URL'), help='attach to a Chrome you started with --remote-debugging-port'
	)
	parser.add_argument(
		'--network',
		choices=[m.value for m in NetworkMode],
		default=None,
		help='route: off (direct), auto (default: Tor only after a network/geo block), always (Tor); env BROWSER_USE_NETWORK',
	)
	parser.add_argument(
		'--exit-country', default=None, help='two-letter Tor exit country, e.g. de (env BROWSER_USE_EXIT_COUNTRY)'
	)
	args = parser.parse_args()
	network = None
	if args.network or args.exit_country:
		base = NetworkRouter.from_env(default=NetworkMode.AUTO)
		network = NetworkRouter(args.network or base.mode, args.exit_country or base.exit_country)
	asyncio.run(main(args.cdp_url, network))


if __name__ == '__main__':
	cli()
