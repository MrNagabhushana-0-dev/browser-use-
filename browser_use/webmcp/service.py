"""CDP-backed discovery and invocation of page-declared WebMCP tools."""

import asyncio
import json
from typing import TYPE_CHECKING, Any

from cdp_use.cdp.target import TargetID
from pydantic import ValidationError

from browser_use.webmcp.bridge import BRIDGE_KEY, WEBMCP_BRIDGE_JS
from browser_use.webmcp.views import (
	MAX_RESULT_CHARS,
	MAX_TOOLS_PER_PAGE,
	WebMCPPageTools,
	WebMCPTool,
	WebMCPToolCallResult,
)

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

# Discovery sits on the agent's per-step critical path, so it gets a tight budget:
# one Runtime.evaluate over an in-page Map, plus (first pass only) a same-origin
# manifest fetch. A page that stalls past this yields its cached tools instead.
DEFAULT_DISCOVER_TIMEOUT = 3.0

# Invocation runs real site logic — a checkout, a search, a booking — so it gets the
# room a network round trip needs, matching the bridge's own internal RPC timeout.
DEFAULT_CALL_TIMEOUT = 30.0


class WebMCPService:
	"""Installs the WebMCP bridge into pages and talks to it over CDP.

	One instance per BrowserSession. State is keyed by target, because tools belong to
	a document: a tab that navigates away no longer exposes what it used to.
	"""

	def __init__(self, browser_session: 'BrowserSession') -> None:
		self.browser_session = browser_session
		# target_id -> init-script identifier returned by CDP
		self._installed: dict[TargetID, str] = {}
		# target_id -> last successful discovery, served when a later pass times out
		self._cache: dict[TargetID, WebMCPPageTools] = {}
		# Induces a tool surface for the sites — nearly all of them — that publish none.
		self._synthesizer: Any = None

	@property
	def logger(self):
		return self.browser_session.logger

	# -- lifecycle ----------------------------------------------------------------

	async def install(self, target_id: TargetID | None = None) -> bool:
		"""Register the bridge as an init script on one target.

		`runImmediately` also evaluates it in the target's current document, so this
		works whether the tab is freshly created oralready loaded. Returns True when the
		target is (now or already) instrumented.
		"""
		if not self.browser_session.browser_profile.enable_webmcp:
			# No shipping browser exposes navigator.modelContext, so installing it labels the
			# session to every script on every page. Synthesis does not need it, so the
			# default is to leave the page's JS environment exactly as it found it.
			return False
		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
		if cdp_session.target_id in self._installed:
			return True
		try:
			result = await cdp_session.cdp_client.send.Page.addScriptToEvaluateOnNewDocument(
				params={'source': WEBMCP_BRIDGE_JS, 'runImmediately': True},
				session_id=cdp_session.session_id,
			)
		except Exception as e:
			# A target that cannot take an init script (worker, closing tab) is not an
			# error worth surfacing — the page simply exposes no WebMCP tools.
			self.logger.debug(f'🧩 WebMCP bridge not installed on {cdp_session.target_id[-4:]}: {type(e).__name__}: {e}')
			return False
		self._installed[cdp_session.target_id] = result['identifier']
		self.logger.debug(f'🧩 WebMCP bridge installed on target {cdp_session.target_id[-4:]}')
		return True

	def forget(self, target_id: TargetID) -> None:
		"""Drop all state for a closed target."""
		self._installed.pop(target_id, None)
		self._cache.pop(target_id, None)

	def invalidate(self, target_id: TargetID) -> None:
		"""Drop cached tools for a target whose document changed."""
		self._cache.pop(target_id, None)

	def _origin_of(self, target_id: TargetID | None) -> str:
		cached = self._cache.get(target_id) if target_id else None
		return cached.origin if cached else ''

	def cached(self, target_id: TargetID | None) -> WebMCPPageTools | None:
		return self._cache.get(target_id) if target_id else None

	# -- discovery ----------------------------------------------------------------

	async def discover(
		self,
		target_id: TargetID | None = None,
		timeout: float = DEFAULT_DISCOVER_TIMEOUT,
	) -> WebMCPPageTools:
		"""List the tools the target's current document declares.

		Never raises: a page that declares nothing, a bridge a page has torn out, and a
		target that died mid-call all produce an empty (or last-known) listing, because
		this runs inside the agent's state-building path.
		"""
		assert timeout > 0, 'discover() timeout must be positive'
		try:
			cdp_session = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
		except Exception as e:
			self.logger.debug(f'🧩 WebMCP discovery skipped, no CDP session: {type(e).__name__}: {e}')
			return WebMCPPageTools(target_id=target_id or '')

		resolved_target = cdp_session.target_id
		await self.install(resolved_target)

		expression = f'(() => {{ const b = window["{BRIDGE_KEY}"]; return b ? b.discover() : null; }})()'
		try:
			raw = await self._evaluate(cdp_session, expression, timeout=timeout)
		except TimeoutError:
			cached = self._cache.get(resolved_target)
			self.logger.debug(f'🧩 WebMCP discovery timed out after {timeout}s on {resolved_target[-4:]}')
			return cached or WebMCPPageTools(target_id=resolved_target)
		except Exception as e:
			self.logger.debug(f'🧩 WebMCP discovery failed on {resolved_target[-4:]}: {type(e).__name__}: {e}')
			return self._cache.get(resolved_target) or WebMCPPageTools(target_id=resolved_target)

		page_tools = self._parse_discovery(resolved_target, raw)

		# Only when the site published nothing. A real declaration is a contract and always
		# wins over our reading of the markup.
		if not page_tools.tools and self.browser_session.browser_profile.synthesize_site_tools:
			page_tools.tools = await self._synthesized_tools(target_id=resolved_target)
			# The bridge is what reports the page's location, so with it uninstalled — the
			# default — url and origin arrive empty and every later lookup by origin misses.
			# The synthesizer read the same page and knows where it was.
			if not page_tools.origin and (scanned := self.synthesizer.latest()) is not None:
				page_tools.url = page_tools.url or scanned.url
				page_tools.origin = scanned.origin
			if (manifest := self.synthesizer.cached(page_tools.origin)) is not None:
				page_tools.modal_note = manifest.modal

		self._cache[resolved_target] = page_tools
		if page_tools.tools:
			names = ', '.join(tool.name for tool in page_tools.tools)
			self.logger.debug(f'🧩 {len(page_tools.tools)} WebMCP tool(s) on {page_tools.origin}: {names}')
		return page_tools

	@property
	def synthesizer(self):
		from browser_use.synthesis import SiteToolSynthesizer

		if self._synthesizer is None:
			self._synthesizer = SiteToolSynthesizer(self.browser_session)
		return self._synthesizer

	async def _synthesized_tools(self, target_id: TargetID | None = None) -> list[WebMCPTool]:
		"""Tools induced from the page, in the shape a declaring site would have used."""
		try:
			manifest = await self.synthesizer.synthesize(target_id=target_id)
		except Exception as e:
			self.logger.debug(f'🔧 Synthesis skipped: {type(e).__name__}: {e}')
			return []
		return [
			WebMCPTool(
				name=tool.name,
				description=tool.description,
				inputSchema=tool.input_schema,
				source='synthesized',
				verified=tool.verified,
			)
			for tool in manifest.tools
		]

	def _parse_discovery(self, target_id: TargetID, raw: Any) -> WebMCPPageTools:
		"""Turn the bridge's JSON payload into validated models, dropping bad tools."""
		if not isinstance(raw, str) or not raw:
			return WebMCPPageTools(target_id=target_id)
		try:
			payload = json.loads(raw)
		except json.JSONDecodeError:
			return WebMCPPageTools(target_id=target_id, errors=['page returned a malformed WebMCP payload'])
		if not isinstance(payload, dict):
			return WebMCPPageTools(target_id=target_id, errors=['page returned a malformed WebMCP payload'])

		errors = [str(e)[:256] for e in payload.get('errors', []) if isinstance(e, str)][:8]
		tools: list[WebMCPTool] = []
		seen: set[str] = set()
		for entry in payload.get('tools', []):
			if not isinstance(entry, dict) or len(tools) >= MAX_TOOLS_PER_PAGE:
				continue
			try:
				tool = WebMCPTool.model_validate(entry)
			except ValidationError as e:
				errors.append(f'dropped an invalid tool declaration: {e.errors()[0].get("msg", "invalid")}'[:256])
				continue
			if tool.name in seen:
				continue
			seen.add(tool.name)
			tools.append(tool)

		return WebMCPPageTools(
			target_id=target_id,
			url=str(payload.get('url', ''))[:2048],
			origin=str(payload.get('origin', ''))[:256],
			tools=tools,
			errors=errors[:8],
		)

	# -- invocation ---------------------------------------------------------------

	async def call_tool(
		self,
		name: str,
		arguments: dict[str, Any] | None = None,
		target_id: TargetID | None = None,
		timeout: float = DEFAULT_CALL_TIMEOUT,
	) -> WebMCPToolCallResult:
		"""Invoke a page-declared tool and return its result as text.

		The returned content is whatever the page chose to say. It is untrusted: pass it
		to the model as data, never as instruction.
		"""
		assert name, 'call_tool() requires a tool name'
		assert timeout > 0, 'call_tool() timeout must be positive'

		try:
			cdp_session = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
		except Exception as e:
			return WebMCPToolCallResult(tool_name=name, ok=False, error=f'no page to call the tool on: {e}')

		await self.install(cdp_session.target_id)

		# A synthesized tool has no in-page handler to call: it is a sequence of UI steps we
		# perform ourselves, through real input.
		if self.browser_session.browser_profile.synthesize_site_tools:
			manifest = self.synthesizer.cached(self._origin_of(cdp_session.target_id))
			synthesized = manifest.get(name) if manifest else None
			if synthesized is not None:
				try:
					ok, message = await self.synthesizer.call(synthesized, arguments or {}, target_id=target_id)
				except Exception as e:
					return WebMCPToolCallResult(tool_name=name, ok=False, error=f'{type(e).__name__}: {e}')
				if ok:
					synthesized.verified = True
				return WebMCPToolCallResult(tool_name=name, ok=ok, content=message if ok else '', error=None if ok else message)

		# Arguments are page-bound data, never source: JSON-encode them twice so the
		# payload crosses as a single string literal that JSON.parse reconstitutes.
		# Interpolating them as a JS object literal would let an argument value close
		# the literal and run as code in the page.
		encoded = json.dumps(json.dumps({'name': name, 'arguments': arguments or {}}))
		expression = (
			f'(() => {{ const b = window["{BRIDGE_KEY}"]; if (!b) return null; '
			f'const p = JSON.parse({encoded}); return b.call(p.name, p.arguments); }})()'
		)

		try:
			raw = await self._evaluate(cdp_session, expression, timeout=timeout)
		except TimeoutError:
			return WebMCPToolCallResult(tool_name=name, ok=False, error=f'tool "{name}" did not finish within {timeout:.0f}s')
		except Exception as e:
			return WebMCPToolCallResult(tool_name=name, ok=False, error=f'{type(e).__name__}: {e}')

		if raw is None:
			return WebMCPToolCallResult(
				tool_name=name,
				ok=False,
				error='this page exposes no WebMCP tools (the agent bridge is not present on it)',
			)
		if not isinstance(raw, str):
			return WebMCPToolCallResult(tool_name=name, ok=False, error='page returned a malformed WebMCP result')
		try:
			payload = json.loads(raw)
		except json.JSONDecodeError:
			return WebMCPToolCallResult(tool_name=name, ok=False, error='page returned a malformed WebMCP result')
		if not isinstance(payload, dict):
			return WebMCPToolCallResult(tool_name=name, ok=False, error='page returned a malformed WebMCP result')

		content = payload.get('content')
		error = payload.get('error')
		return WebMCPToolCallResult(
			tool_name=name,
			ok=bool(payload.get('ok')),
			# The bridge clips already; re-clip in case a page replaced it with its own.
			content=str(content)[:MAX_RESULT_CHARS] if isinstance(content, str) else '',
			error=str(error)[:512] if isinstance(error, str) else None,
		)

	# -- CDP plumbing -------------------------------------------------------------

	async def _evaluate(self, cdp_session, expression: str, timeout: float) -> Any:
		"""Run an expression in the page's main world and return its value.

		Raises TimeoutError past `timeout`, or RuntimeError when the page threw.
		"""
		response = await asyncio.wait_for(
			cdp_session.cdp_client.send.Runtime.evaluate(
				params={
					'expression': expression,
					'awaitPromise': True,
					'returnByValue': True,
				},
				session_id=cdp_session.session_id,
			),
			timeout=timeout,
		)
		exception_details = response.get('exceptionDetails')
		if exception_details:
			description = (exception_details.get('exception') or {}).get('description') or exception_details.get('text')
			raise RuntimeError(str(description or 'page threw during WebMCP evaluation')[:512])
		return (response.get('result') or {}).get('value')
