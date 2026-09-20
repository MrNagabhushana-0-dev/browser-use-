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
from browser_use.synthesis.store import ManifestStore, fingerprint
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

# Rows a single read tool will return. A cap is what makes it a tool rather than a dump.
MAX_ROWS_PER_READ = 100

# Resolve a stored locator back to a live element and report where it is. Mirrors the
# preference order in Locator: the handles authors keep stable come first.
RESOLVE_JS = r"""
const loc = JSON.parse(LOCATOR_JSON);
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

// Must pierce open shadow roots for the same reason the scanner does: a locator recorded
// inside a web component is unreachable through plain querySelector.
const deepQuery = (selector, root) => {
	root = root || document;
	const out = [...root.querySelectorAll(selector)];
	let budget = 400;
	const descend = (node) => {
		for (const el of node.querySelectorAll('*')) {
			if (budget <= 0) return;
			if (el.shadowRoot) { budget--; out.push(...el.shadowRoot.querySelectorAll(selector)); descend(el.shadowRoot); }
		}
	};
	descend(root);
	return out;
};

// Must match the scanner's rule exactly, or a locator recorded there will not resolve
// here. Visible text names a control, never a container: a <form> wrapping one button
// has that button's text as its own innerText, and would otherwise win the name match.
const INTERACTIVE = 'a[href], button, input, select, textarea, option, label,'
	+ ' [role="button"], [role="link"], [role="tab"], [role="checkbox"], [role="switch"], [role="menuitem"]';

const accName = (el) => {
	const aria = el.getAttribute('aria-label');
	if (aria) return clean(aria);
	if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
	const w = el.closest('label'); if (w) return clean(w.innerText);
	for (const a of ['placeholder', 'title', 'alt', 'name']) { const v = el.getAttribute(a); if (v) return clean(v); }
	const tag = el.tagName.toLowerCase();
	if (tag === 'table' || tag === 'fieldset' || ['table', 'grid'].includes(el.getAttribute('role'))) {
		const cap = el.querySelector('caption, legend');
		if (cap) return clean(cap.innerText || cap.textContent);
	}
	if (el.matches(INTERACTIVE)) return clean(el.innerText || el.value || '');
	return '';
};

const roleOf = (el) => {
	const explicit = el.getAttribute('role');
	if (explicit) return explicit.toLowerCase();
	const tag = el.tagName.toLowerCase();
	if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
	if (tag === 'button') return 'button';
	if (tag === 'select') return 'combobox';
	if (tag === 'textarea') return 'textbox';
	if (tag === 'input') {
		const ty = (el.getAttribute('type') || 'text').toLowerCase();
		if (['submit', 'button', 'reset', 'image'].includes(ty)) return 'button';
		if (ty === 'checkbox') return 'checkbox';
		if (ty === 'radio') return 'radio';
		if (ty === 'search') return 'searchbox';
		return 'textbox';
	}
	return 'generic';
};

let el = null;
if (loc.testid) el = deepQuery(`[data-testid="${CSS.escape(loc.testid)}"]`)[0]
	|| deepQuery(`[data-test-id="${CSS.escape(loc.testid)}"]`)[0];
// Not getElementById: it does not see into shadow roots.
if (!el && loc.id) el = deepQuery(`#${CSS.escape(loc.id)}`)[0] || document.getElementById(loc.id);
if (!el && loc.name) {
	const all = deepQuery('input, textarea, select, button, a[href], [role], table, form, nav');
	const named = all.filter(e => accName(e) === loc.name);
	const loose = all.filter(e => accName(e).toLowerCase() === loc.name.toLowerCase());
	// Role first: two elements can share a name, and the recorded role says which one was
	// meant — the submit button, not the form that contains it.
	el = named.find(e => roleOf(e) === loc.role)
		|| loose.find(e => roleOf(e) === loc.role)
		|| named[0] || loose[0] || null;
}
if (!el && loc.css) el = document.querySelector(loc.css);
if (!el) return {found: false};

el.scrollIntoView({block: 'center', inline: 'center'});
const r = el.getBoundingClientRect();
return {found: true, x: r.x, y: r.y, w: r.width, h: r.height,
        tag: el.tagName.toLowerCase(), visible: r.width > 1 && r.height > 1};
"""


# Pull a table's rows out as records. The row cap is the whole point of a read tool: the
# agent gets structured data with a stated limit instead of the page's markup.
READ_ROWS_JS = RESOLVE_JS.replace(
	'return {found: true, x: r.x, y: r.y, w: r.width, h: r.height,\n        tag: el.tagName.toLowerCase(), visible: r.width > 1 && r.height > 1};',
	"""
const headerCells = [...el.querySelectorAll('thead th, thead td, tr:first-child th')]
	.map(h => clean(h.innerText)).filter(Boolean);
const bodyRows = [...el.querySelectorAll('tbody tr')];
const rows = (bodyRows.length ? bodyRows : [...el.querySelectorAll('tr')].slice(1)).slice(0, ROW_LIMIT);
const out = rows.map(tr => {
	const cells = [...tr.children].map(td => clean(td.innerText));
	if (!headerCells.length) return cells;
	const record = {};
	headerCells.forEach((h, i) => { record[h] = cells[i] === undefined ? '' : cells[i]; });
	return record;
});
return {found: true, headers: headerCells, rows: out, truncated: rows.length < (bodyRows.length || 0)};
""",
)


class SiteToolSynthesizer:
	"""Induces, caches and runs a tool surface for sites that publish none."""

	def __init__(self, browser_session: 'BrowserSession', store: 'ManifestStore | None' = None) -> None:
		self.browser_session = browser_session
		self._manifests: dict[str, SiteManifest] = {}
		# Persists across sessions, so the second agent to visit a site inherits what the
		# first one worked out — including which tools have actually been run.
		self.store = store if store is not None else ManifestStore()

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

	def _tool_from_table(self, table: dict, used: set[str]) -> SynthesizedTool | None:
		"""A table becomes a read tool: structured rows, with a stated limit.

		This is the affordance that most changes what an agent costs. Without it, reading a
		sixty-row table means the whole table crossing the context window as markup; with
		it, the agent asks for the columns it wants and gets records back.
		"""
		headers = [h for h in (table.get('headers') or []) if h]
		if not headers:
			return None
		label = table.get('name') or ' '.join(headers[:2])
		name = self._unique(f'read_{to_identifier(label, fallback="table")}', used)
		return SynthesizedTool(
			name=name,
			description=f'Read rows from the {label.strip()} table ({", ".join(headers[:6])}) — synthesized',
			input_schema={
				'type': 'object',
				'properties': {'limit': {'type': 'number', 'description': f'Rows to return, up to {MAX_ROWS_PER_READ}'}},
				'required': [],
			},
			steps=[ToolStep(action='read', locator=Locator.model_validate(table.get('locator') or {}), columns=headers)],
		)

	def _tool_from_view(self, view: dict, used: set[str]) -> SynthesizedTool | None:
		label = view.get('name') or ''
		if not label:
			return None
		verb = 'switch_to' if view.get('role') == 'tab' else 'open'
		name = self._unique(f'{verb}_{to_identifier(label, fallback="view")}', used)
		return SynthesizedTool(
			name=name,
			description=f"{label.strip()} — synthesized from this page's navigation",
			input_schema={'type': 'object', 'properties': {}, 'required': []},
			steps=[ToolStep(action='click', locator=Locator.model_validate(view.get('locator') or {}))],
		)

	def _tool_from_pager(self, pager: dict, used: set[str]) -> SynthesizedTool | None:
		kind = pager.get('kind')
		if kind not in ('next', 'previous'):
			return None
		name = self._unique(f'{kind}_page', used)
		return SynthesizedTool(
			name=name,
			description=f'Go to the {kind} page of results — synthesized',
			input_schema={'type': 'object', 'properties': {}, 'required': []},
			steps=[ToolStep(action='click', locator=Locator.model_validate(pager.get('locator') or {}))],
		)

	def _tool_from_toggle(self, toggle: dict, used: set[str]) -> SynthesizedTool | None:
		label = toggle.get('name') or ''
		if not label:
			return None
		name = self._unique(f'set_{to_identifier(label, fallback="option")}', used)
		return SynthesizedTool(
			name=name,
			description=f'Turn "{label.strip()}" on or off — synthesized',
			input_schema={
				'type': 'object',
				'properties': {'on': {'type': 'boolean', 'description': 'Desired state'}},
				'required': ['on'],
			},
			steps=[ToolStep(action='set_checked', locator=Locator.model_validate(toggle.get('locator') or {}), param='on')],
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

		shape = fingerprint(affordances)
		if not refresh:
			if (in_memory := self._manifests.get(origin)) and in_memory.fingerprint == shape:
				return in_memory
			# Learned in an earlier session, and the page still looks the way it did.
			if cached := self.store.get(origin, expected_fingerprint=shape):
				self._manifests[origin] = cached
				self.logger.debug(f'🔧 Reused {len(cached.tools)} learned tool(s) for {origin}')
				return cached

		used: set[str] = set()
		tools: list[SynthesizedTool] = []

		for form in affordances.get('forms') or []:
			if len(tools) >= MAX_TOOLS_PER_SITE:
				break
			if tool := self._tool_from_form(form, used):
				tools.append(tool)

		# Read tools first among the rest: data access is what an agent needs most and what
		# costs most without it.
		for table in affordances.get('tables') or []:
			if len(tools) >= MAX_TOOLS_PER_SITE:
				break
			if tool := self._tool_from_table(table, used):
				tools.append(tool)

		for pager in affordances.get('pagers') or []:
			if len(tools) >= MAX_TOOLS_PER_SITE:
				break
			if tool := self._tool_from_pager(pager, used):
				tools.append(tool)

		for toggle in affordances.get('toggles') or []:
			if len(tools) >= MAX_TOOLS_PER_SITE:
				break
			if tool := self._tool_from_toggle(toggle, used):
				tools.append(tool)

		for button in affordances.get('buttons') or []:
			if len(tools) >= MAX_TOOLS_PER_SITE:
				break
			if tool := self._tool_from_button(button, used):
				tools.append(tool)

		for view in affordances.get('views') or []:
			if len(tools) >= MAX_TOOLS_PER_SITE:
				break
			if tool := self._tool_from_view(view, used):
				tools.append(tool)

		manifest = SiteManifest(
			origin=origin,
			url=str(affordances.get('url', ''))[:2048],
			title=str(affordances.get('title', ''))[:200],
			tools=tools,
			fingerprint=shape,
		)
		self._manifests[origin] = manifest
		self.store.put(manifest)
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
		outputs: list[str] = []

		for index, step in enumerate(tool.steps, start=1):
			# A read returns data rather than acting, and resolves the element itself.
			if step.action == 'read':
				rows = await self._read_rows(step, arguments, target_id=target_id)
				if rows is None:
					return False, f'step {index} could not read {step.locator.describe()}'
				outputs.append(rows)
				continue

			box = await self._locate(step.locator, target_id=target_id)
			if not box:
				return False, f'step {index} ({step.action}) could not find {step.locator.describe()}'

			if step.action == 'set_checked':
				desired = bool(arguments.get(step.param or 'on'))
				changed = await self._set_checked(step.locator, desired, box, target_id=target_id)
				if changed is None:
					return False, f'step {index} could not toggle {step.locator.describe()}'
				outputs.append(f'{step.locator.describe()} is {"on" if desired else "off"}')
				continue

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

		# It ran end to end, so it is no longer just a reading of the markup.
		self._mark_verified(tool)

		if outputs:
			return True, '\n'.join(outputs)
		return True, f'ran {tool.name} ({len(tool.steps)} steps)'

	def _mark_verified(self, tool: SynthesizedTool) -> None:
		"""Record that a tool has really worked, and keep that across sessions."""
		if tool.verified:
			return
		tool.verified = True
		for origin, manifest in self._manifests.items():
			if manifest.get(tool.name) is tool:
				self.store.put(manifest)
				self.logger.debug(f'🔧 {tool.name} verified on {origin}')
				return

	async def _read_rows(self, step: ToolStep, arguments: dict[str, Any], target_id=None) -> str | None:
		"""Rows from a table, as JSON records, capped."""
		try:
			requested = int(arguments.get('limit') or MAX_ROWS_PER_READ)
		except (TypeError, ValueError):
			requested = MAX_ROWS_PER_READ
		limit = max(1, min(MAX_ROWS_PER_READ, requested))

		script = READ_ROWS_JS.replace('LOCATOR_JSON', json.dumps(json.dumps(step.locator.model_dump()))).replace(
			'ROW_LIMIT', str(limit)
		)
		result = await self.browser_session.run_page_script(script, target_id=target_id, max_chars=40000)
		if not result.ok:
			return None
		try:
			payload = json.loads(result.value)
		except json.JSONDecodeError:
			return None
		if not payload.get('found'):
			return None

		rows = payload.get('rows') or []
		text = json.dumps(rows)
		if payload.get('truncated'):
			text += f'\n[showing {len(rows)} rows; ask for more with limit]'
		return text

	async def _set_checked(self, locator: Locator, desired: bool, box: dict, target_id=None) -> bool | None:
		"""Click a checkbox only if it is not already in the wanted state.

		Clicking unconditionally is the bug people ship here: calling set_x(on=True) twice
		leaves the box off, because the second call toggles it back.
		"""
		script = RESOLVE_JS.replace('LOCATOR_JSON', json.dumps(json.dumps(locator.model_dump()))).replace(
			'tag: el.tagName.toLowerCase(), visible: r.width > 1 && r.height > 1};',
			"tag: el.tagName.toLowerCase(), checked: !!(el.checked || el.getAttribute('aria-checked') === 'true')};",
		)
		result = await self.browser_session.run_page_script(script, target_id=target_id)
		if not result.ok:
			return None
		try:
			current = bool(json.loads(result.value).get('checked'))
		except json.JSONDecodeError:
			return None

		if current != desired:
			await self.browser_session.human.click_box((box['x'], box['y'], box['w'], box['h']), target_id=target_id)
		return True

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
