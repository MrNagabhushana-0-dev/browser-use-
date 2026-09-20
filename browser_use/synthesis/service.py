"""Give a site a typed tool surface it never implemented.

WebMCP is the right shape and almost nobody ships it — the standard asks the long tail of
the web to adopt a protocol, which twenty years of metadata history says it will not do.
The conclusion people keep drawing is that the browser should synthesize the layer itself
from what it already computes. This does that.

Scan the affordances a screen reader would see, induce typed tools from them, and expose
those tools in the same shape a WebMCP-aware site would have published. Everything
downstream — the agent's call_webmcp_tool action, the MCP tools that Claude Code, Codex and
Antigravity consume — then works on a site that has never heard of any of this.

Two properties keep it honest. Tools execute through real trusted input, resolved by
accessible name rather than coordinates, so nothing depends on a vision model guessing
pixels. And a tool is marked verified only once it has actually run and the page has
actually changed; until then it is labelled as an inference from markup, because that is
what it is.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from browser_use.synthesis.scanner import SCAN_JS
from browser_use.synthesis.views import (
	MAX_STEPS_PER_TOOL,
	MAX_TOOLS_PER_SITE,
	Locator,
	SiteManifest,
	SynthesizedTool,
	ToolStep,
	to_identifier,
)

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

# Labels that mean "this searches something", used to give the most common affordance on
# the web a predictable name instead of whatever the markup happened to call it.
_SEARCH_HINTS = ('search', 'find', 'query', 'lookup')

# Resolve a stored locator back to a live element and report where it is. Mirrors the
# preference order in Locator: the handles authors keep stable come first.
RESOLVE_JS = r"""
const loc = JSON.parse(LOCATOR_JSON);
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
const accName = (el) => {
	const aria = el.getAttribute('aria-label');
	if (aria) return clean(aria);
	if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
	const w = el.closest('label'); if (w) return clean(w.innerText);
	for (const a of ['placeholder', 'title', 'alt', 'name']) { const v = el.getAttribute(a); if (v) return clean(v); }
	return clean(el.innerText || el.value || '');
};

let el = null;
if (loc.testid) el = document.querySelector(`[data-testid="${CSS.escape(loc.testid)}"]`)
	|| document.querySelector(`[data-test-id="${CSS.escape(loc.testid)}"]`);
if (!el && loc.id) el = document.getElementById(loc.id);
if (!el && loc.name) {
	const all = [...document.querySelectorAll('input, textarea, select, button, a[href], [role]')];
	el = all.find(e => accName(e) === loc.name) || all.find(e => accName(e).toLowerCase() === loc.name.toLowerCase()) || null;
}
if (!el && loc.css) el = document.querySelector(loc.css);
if (!el) return {found: false};

el.scrollIntoView({block: 'center', inline: 'center'});
const r = el.getBoundingClientRect();
return {found: true, x: r.x, y: r.y, w: r.width, h: r.height,
        tag: el.tagName.toLowerCase(), visible: r.width > 1 && r.height > 1};
"""


class SiteToolSynthesizer:
	"""Induces, caches and runs a tool surface for sites that publish none."""

	def __init__(self, browser_session: 'BrowserSession') -> None:
		self.browser_session = browser_session
		self._manifests: dict[str, SiteManifest] = {}

	@property
	def logger(self):
		return self.browser_session.logger

	# -- scanning ---------------------------------------------------------------------

	async def scan(self, target_id=None) -> dict[str, Any]:
		"""The page's affordances, as one round trip."""
		result = await self.browser_session.run_page_script(SCAN_JS, target_id=target_id, max_chars=60000)
		if not result.ok:
			self.logger.debug(f'🔧 Affordance scan failed: {result.error}')
			return {}
		try:
			return json.loads(result.value)
		except json.JSONDecodeError:
			return {}

	# -- synthesis --------------------------------------------------------------------

	def _parameter(self, control: dict) -> tuple[str, dict] | None:
		"""One tool parameter from one control, or None if it must not be automated."""
		if control.get('sensitive'):
			# A password or card field never becomes a parameter. Synthesizing
			# `sign_in(password)` would invite a model to invent credentials, and put them
			# in a trace on the way.
			return None
		label = control.get('name') or control.get('role') or ''
		key = to_identifier(label, fallback='value')
		role = control.get('role')
		control_type = control.get('type')

		spec: dict[str, Any] = {'type': 'string', 'description': label[:100]}
		if control_type == 'number':
			spec['type'] = 'number'
		elif role == 'checkbox':
			spec['type'] = 'boolean'
		elif options := control.get('options'):
			spec['enum'] = options[:25]
		return key, spec

	def _tool_from_form(self, form: dict, used: set[str]) -> SynthesizedTool | None:
		controls = form.get('controls') or []
		label = form.get('name') or (form.get('submit') or {}).get('label') or 'submit form'

		properties: dict[str, Any] = {}
		required: list[str] = []
		steps: list[ToolStep] = []

		for control in controls:
			if len(steps) >= MAX_STEPS_PER_TOOL:
				break
			if control.get('role') == 'button':
				continue
			parameter = self._parameter(control)
			if parameter is None:
				continue
			key, spec = parameter
			if key in properties:
				continue
			properties[key] = spec
			if control.get('required'):
				required.append(key)
			steps.append(
				ToolStep(
					action='select' if control.get('role') == 'combobox' else 'fill',
					locator=Locator.model_validate(control.get('locator') or {}),
					param=key,
				)
			)

		if not steps:
			return None

		submit = form.get('submit')
		if submit:
			steps.append(ToolStep(action='click', locator=Locator.model_validate(submit.get('locator') or {})))
		else:
			# No submit button: Enter on the last field is how a person sends it.
			steps.append(ToolStep(action='press', locator=steps[-1].locator, key='Enter'))

		# The commonest affordance on the web deserves a predictable name.
		text = f'{label} {" ".join(properties)}'.lower()
		is_search = any(hint in text for hint in _SEARCH_HINTS)
		name = 'search' if is_search and len(properties) == 1 else to_identifier(label, fallback='submit_form')
		name = self._unique(name, used)

		if is_search and len(properties) == 1:
			# Rename the lone parameter too, so the tool reads search(query=...).
			only = next(iter(properties))
			properties = {'query': properties[only]}
			required = ['query'] if required else []
			steps[0].param = 'query'

		return SynthesizedTool(
			name=name,
			description=f"{label.strip()} — synthesized from this page's form",
			input_schema={'type': 'object', 'properties': properties, 'required': required},
			steps=steps,
		)

	def _tool_from_button(self, button: dict, used: set[str]) -> SynthesizedTool | None:
		label = button.get('name') or ''
		if not label:
			return None
		name = self._unique(to_identifier(label, fallback='press'), used)
		return SynthesizedTool(
			name=name,
			description=f'{label.strip()} — synthesized from a control on this page',
			input_schema={'type': 'object', 'properties': {}, 'required': []},
			steps=[ToolStep(action='click', locator=Locator.model_validate(button.get('locator') or {}))],
		)

	@staticmethod
	def _unique(name: str, used: set[str]) -> str:
		candidate, n = name, 2
		while candidate in used:
			candidate, n = f'{name}_{n}', n + 1
		used.add(candidate)
		return candidate

	async def synthesize(self, target_id=None, refresh: bool = False) -> SiteManifest:
		"""Induce the tool surface for the current page, cached per origin."""
		affordances = await self.scan(target_id=target_id)
		origin = affordances.get('origin') or ''
		if not origin:
			return SiteManifest(origin='')
		if not refresh and origin in self._manifests:
			return self._manifests[origin]

		used: set[str] = set()
		tools: list[SynthesizedTool] = []

		for form in affordances.get('forms') or []:
			if len(tools) >= MAX_TOOLS_PER_SITE:
				break
			if tool := self._tool_from_form(form, used):
				tools.append(tool)

		for button in affordances.get('buttons') or []:
			if len(tools) >= MAX_TOOLS_PER_SITE:
				break
			if tool := self._tool_from_button(button, used):
				tools.append(tool)

		manifest = SiteManifest(
			origin=origin,
			url=str(affordances.get('url', ''))[:2048],
			title=str(affordances.get('title', ''))[:200],
			tools=tools,
		)
		self._manifests[origin] = manifest
		if tools:
			self.logger.debug(f'🔧 Synthesized {len(tools)} tool(s) for {origin}: {", ".join(t.name for t in tools)}')
		return manifest

	def cached(self, origin: str) -> SiteManifest | None:
		return self._manifests.get(origin)

	# -- execution --------------------------------------------------------------------

	async def _locate(self, locator: Locator, target_id=None) -> dict | None:
		script = RESOLVE_JS.replace('LOCATOR_JSON', json.dumps(json.dumps(locator.model_dump())))
		result = await self.browser_session.run_page_script(script, target_id=target_id)
		if not result.ok:
			return None
		try:
			payload = json.loads(result.value)
		except json.JSONDecodeError:
			return None
		return payload if payload.get('found') else None

	async def call(self, tool: SynthesizedTool, arguments: dict[str, Any], target_id=None) -> tuple[bool, str]:
		"""Run a synthesized tool through real input. Returns (ok, message)."""
		human = self.browser_session.human

		for index, step in enumerate(tool.steps, start=1):
			box = await self._locate(step.locator, target_id=target_id)
			if not box:
				return False, f'step {index} ({step.action}) could not find {step.locator.describe()}'

			rect = (box['x'], box['y'], box['w'], box['h'])
			if step.action == 'click':
				await human.click_box(rect, target_id=target_id)
			elif step.action == 'press':
				await human.click_box(rect, target_id=target_id)
				await human.press(step.key or 'Enter', target_id=target_id)
			elif step.action in ('fill', 'select'):
				value = arguments.get(step.param or '', '')
				if value in (None, ''):
					continue
				if step.action == 'select':
					# A native <select> opens an OS-level popup that CDP input cannot drive,
					# so this is the one step that has to go through the DOM.
					picked = await self._select_option(step.locator, str(value), target_id=target_id)
					if not picked:
						return False, f'step {index} could not select {value!r} in {step.locator.describe()}'
				else:
					await human.click_box(rect, target_id=target_id)
					await human.type_text(str(value), target_id=target_id)

		return True, f'ran {tool.name} ({len(tool.steps)} steps)'

	async def _select_option(self, locator: Locator, value: str, target_id=None) -> bool:
		script = (
			RESOLVE_JS.replace('LOCATOR_JSON', json.dumps(json.dumps(locator.model_dump())))
			.replace('return {found: true,', 'const __el = el; return {found: true,')
			.replace(
				'tag: el.tagName.toLowerCase(), visible: r.width > 1 && r.height > 1};',
				'tag: el.tagName.toLowerCase(), picked: (() => {'
				'  const want = ' + json.dumps(value) + ';'
				'  const opt = [...(__el.options || [])].find(o => o.textContent.trim() === want || o.value === want);'
				'  if (!opt) return false;'
				'  __el.value = opt.value;'
				"  __el.dispatchEvent(new Event('input', {bubbles: true}));"
				"  __el.dispatchEvent(new Event('change', {bubbles: true}));"
				'  return true; })()};',
			)
		)
		result = await self.browser_session.run_page_script(script, target_id=target_id)
		if not result.ok:
			return False
		try:
			return bool(json.loads(result.value).get('picked'))
		except json.JSONDecodeError:
			return False
