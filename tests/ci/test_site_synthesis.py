"""Giving a site a typed tool surface it never implemented.

WebMCP is the right shape and almost nothing ships it — the standard asks the long tail of
the web to adopt a protocol, which it will not. So the tool layer gets synthesized from
what the browser already computes for screen readers, in the same shape a declaring site
would have published, and everything downstream works unchanged.

The page below declares no tools at all. That is the point.
"""

import json

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.synthesis import SiteToolSynthesizer

# An ordinary site: a search box, a standalone action, a sign-in form, a filter select.
# No modelContext, no manifest, no data-agent-anything.
ORDINARY_PAGE = """<!DOCTYPE html>
<html><head><title>Ordinary Shop</title></head><body>
	<form id="searchform">
		<input type="search" id="q" aria-label="Search products">
		<button type="submit">Search</button>
	</form>

	<button id="cart" aria-label="Add to cart">Add to cart</button>

	<form id="login">
		<label for="email">Email</label><input id="email" type="email" required>
		<label for="pw">Password</label><input id="pw" type="password" required>
		<button type="submit">Sign in</button>
	</form>

	<form id="filterform">
		<label for="size">Size</label>
		<select id="size"><option>Small</option><option>Medium</option><option>Large</option></select>
		<button type="submit">Apply filter</button>
	</form>

	<div id="result">nothing yet</div>
<script>
	const say = (t) => { document.getElementById('result').textContent = t; };
	searchform.addEventListener('submit', e => { e.preventDefault(); say('searched for ' + q.value); });
	login.addEventListener('submit', e => { e.preventDefault(); say('signed in as ' + email.value); });
	filterform.addEventListener('submit', e => { e.preventDefault(); say('filtered by ' + size.value); });
	cart.addEventListener('click', () => say('added to cart'));
</script>
</body></html>"""

# The same shop, but this one actually implements WebMCP.
DECLARING_PAGE = """<!DOCTYPE html>
<html><head><title>Modern Shop</title></head><body>
	<form><input type="search" aria-label="Search products"><button type="submit">Search</button></form>
<script>
	navigator.modelContext.registerTool({
		name: 'official_search',
		description: 'The search the site actually published',
		inputSchema: {type: 'object', properties: {q: {type: 'string'}}, required: ['q']},
		execute: (args) => 'official results for ' + args.q,
	});
</script>
</body></html>"""


@pytest.fixture(scope='module')
def shop_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/ordinary').respond_with_data(ORDINARY_PAGE, content_type='text/html')
	server.expect_request('/declaring').respond_with_data(DECLARING_PAGE, content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url):
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


async def _result_text(session) -> str:
	out = await session.run_page_script("return document.getElementById('result').textContent;")
	return json.loads(out.value)


async def test_a_site_with_no_agent_support_still_gets_typed_tools(browser_session, shop_server):
	await _goto(browser_session, shop_server.url_for('/ordinary'))

	page_tools = await browser_session.get_webmcp_tools()
	by_name = {tool.name: tool for tool in page_tools.tools}

	assert by_name, 'an ordinary page yielded no tools at all'
	assert all(tool.source == 'synthesized' for tool in page_tools.tools)

	# The commonest affordance on the web gets a predictable name and parameter.
	assert 'search' in by_name, f'no search tool among {sorted(by_name)}'
	assert 'query' in by_name['search'].input_schema['properties']

	# A standalone control becomes a no-argument verb.
	assert 'add_to_cart' in by_name, f'no add_to_cart among {sorted(by_name)}'
	assert by_name['add_to_cart'].input_schema['properties'] == {}


async def test_calling_a_synthesized_tool_really_drives_the_page(browser_session, shop_server):
	"""The whole claim: a typed call, on a site that offered no such thing."""
	await _goto(browser_session, shop_server.url_for('/ordinary'))
	await browser_session.get_webmcp_tools()

	result = await browser_session.call_webmcp_tool('search', {'query': 'wool socks'})
	assert result.ok, result.error
	assert await _result_text(browser_session) == 'searched for wool socks'

	clicked = await browser_session.call_webmcp_tool('add_to_cart', {})
	assert clicked.ok, clicked.error
	assert await _result_text(browser_session) == 'added to cart'


async def test_a_dropdown_becomes_an_enum_and_is_selectable(browser_session, shop_server):
	await _goto(browser_session, shop_server.url_for('/ordinary'))
	page_tools = await browser_session.get_webmcp_tools()

	filter_tool = next((t for t in page_tools.tools if 'filter' in t.name), None)
	assert filter_tool is not None, f'no filter tool among {[t.name for t in page_tools.tools]}'
	size = filter_tool.input_schema['properties']['size']
	assert size['enum'] == ['Small', 'Medium', 'Large'], 'a select should offer its options'

	result = await browser_session.call_webmcp_tool(filter_tool.name, {'size': 'Large'})
	assert result.ok, result.error
	assert await _result_text(browser_session) == 'filtered by Large'


async def test_a_password_never_becomes_a_parameter(browser_session, shop_server):
	"""Synthesizing sign_in(password) would invite a model to invent credentials."""
	await _goto(browser_session, shop_server.url_for('/ordinary'))
	page_tools = await browser_session.get_webmcp_tools()

	sign_in = next((t for t in page_tools.tools if 'sign_in' in t.name), None)
	assert sign_in is not None, f'no sign-in tool among {[t.name for t in page_tools.tools]}'

	properties = sign_in.input_schema['properties']
	assert 'email' in properties
	assert not any('password' in key for key in properties), f'a password leaked into {sorted(properties)}'
	assert len(properties) == 1, f'only the email should be fillable, got {sorted(properties)}'


async def test_what_the_site_declares_always_wins(browser_session, shop_server):
	"""A published tool is a contract; a synthesized one is our reading of the markup."""
	await _goto(browser_session, shop_server.url_for('/declaring'))

	page_tools = await browser_session.get_webmcp_tools()
	names = {tool.name for tool in page_tools.tools}

	assert names == {'official_search'}, f'synthesis should not run over a declaring site, got {names}'
	assert page_tools.tools[0].source == 'js'


async def test_the_prompt_says_which_tools_were_inferred(browser_session, shop_server, tmp_path):
	"""A model that cannot tell a contract from a guess will trust both equally."""
	from browser_use.agent.prompts import AgentMessagePrompt
	from browser_use.filesystem.file_system import FileSystem

	await _goto(browser_session, shop_server.url_for('/ordinary'))
	state = await browser_session.get_browser_state_summary(include_screenshot=False)

	rendered = str(
		AgentMessagePrompt(
			browser_state_summary=state,
			file_system=FileSystem(base_dir=tmp_path / 'fs', create_default_files=False),
		)
		.get_user_message(use_vision=False)
		.content
	)
	assert '<webmcp_tools>' in rendered
	assert 'not published by the site' in rendered
	assert 'check the result' in rendered


async def test_synthesis_can_be_turned_off(browser_session, shop_server):
	await _goto(browser_session, shop_server.url_for('/ordinary'))
	browser_session.browser_profile.synthesize_site_tools = False
	try:
		# Bypass the per-target cache from any earlier discovery in this session.
		if browser_session._webmcp_watchdog:
			browser_session._webmcp_watchdog.service._cache.clear()
		page_tools = await browser_session.get_webmcp_tools()
		assert page_tools.tools == []
	finally:
		browser_session.browser_profile.synthesize_site_tools = True


def test_tool_names_are_predictable_from_visible_labels():
	"""The name is what a model types back, so it must follow from what a person reads."""
	from browser_use.synthesis.views import to_identifier

	assert to_identifier('Add to cart') == 'add_to_cart'
	assert to_identifier('Sign in to your account') == 'sign_in_to_account'
	assert to_identifier('') == 'action'


async def test_a_locator_survives_the_element_moving(browser_session, shop_server):
	"""Bound by accessible name, not by index or position — indices die on re-render."""
	await _goto(browser_session, shop_server.url_for('/ordinary'))
	synthesizer = SiteToolSynthesizer(browser_session)
	manifest = await synthesizer.synthesize()
	search = manifest.get('search')
	assert search is not None

	# Shuffle the page: prepend content so everything shifts down.
	await browser_session.run_page_script(
		"document.body.insertAdjacentHTML('afterbegin', '<div style=\"height:260px\">banner</div>'); return 1;"
	)

	ok, message = await synthesizer.call(search, {'query': 'after reflow'})
	assert ok, message
	assert await _result_text(browser_session) == 'searched for after reflow'


# A dashboard: a data table, tabs, pagination, and a standalone toggle. Still no WebMCP.
DASHBOARD_PAGE = """<!DOCTYPE html>
<html><head><title>Orders</title></head><body>
	<nav>
		<a href="#overview" role="tab">Overview</a>
		<a href="#orders" role="tab">Orders</a>
	</nav>

	<table>
		<caption>Recent orders</caption>
		<thead><tr><th>Order</th><th>Customer</th><th>Total</th></tr></thead>
		<tbody>
			<tr><td>A-1</td><td>Ada</td><td>12.00</td></tr>
			<tr><td>A-2</td><td>Grace</td><td>34.50</td></tr>
			<tr><td>A-3</td><td>Alan</td><td>7.25</td></tr>
		</tbody>
	</table>

	<button id="prev">Previous</button>
	<button id="next">Next</button>

	<label for="only-open">Only open orders</label>
	<input type="checkbox" id="only-open">

	<div id="state">idle</div>
<script>
	const say = t => document.getElementById('state').textContent = t;
	next.addEventListener('click', () => say('page 2'));
	prev.addEventListener('click', () => say('page 0'));
	document.getElementById('only-open').addEventListener('change', e => say('only open: ' + e.target.checked));
</script>
</body></html>"""


@pytest.fixture(scope='module')
def dashboard_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/dashboard').respond_with_data(DASHBOARD_PAGE, content_type='text/html')
	yield server
	server.stop()


async def _state_text(session) -> str:
	out = await session.run_page_script("return document.getElementById('state').textContent;")
	return json.loads(out.value)


async def test_a_table_becomes_a_read_tool_that_returns_records(browser_session, dashboard_server):
	"""The affordance that most changes what an agent costs.

	Without it, reading the table means the whole thing crossing the context window as
	markup. With it, the agent gets records and a stated row limit.
	"""
	await _goto(browser_session, dashboard_server.url_for('/dashboard'))
	page_tools = await browser_session.get_webmcp_tools()

	read_tool = next((t for t in page_tools.tools if t.name.startswith('read_')), None)
	assert read_tool is not None, f'no read tool among {[t.name for t in page_tools.tools]}'
	assert 'Order' in read_tool.description and 'Customer' in read_tool.description

	result = await browser_session.call_webmcp_tool(read_tool.name, {})
	assert result.ok, result.error
	rows = json.loads(result.content.split('\n')[0])
	assert rows == [
		{'Order': 'A-1', 'Customer': 'Ada', 'Total': '12.00'},
		{'Order': 'A-2', 'Customer': 'Grace', 'Total': '34.50'},
		{'Order': 'A-3', 'Customer': 'Alan', 'Total': '7.25'},
	]


async def test_a_read_tool_respects_its_limit(browser_session, dashboard_server):
	await _goto(browser_session, dashboard_server.url_for('/dashboard'))
	page_tools = await browser_session.get_webmcp_tools()
	read_tool = next(t for t in page_tools.tools if t.name.startswith('read_'))

	result = await browser_session.call_webmcp_tool(read_tool.name, {'limit': 2})
	assert result.ok, result.error
	rows = json.loads(result.content.split('\n')[0])
	assert len(rows) == 2


async def test_pagination_and_tabs_become_verbs(browser_session, dashboard_server):
	await _goto(browser_session, dashboard_server.url_for('/dashboard'))
	page_tools = await browser_session.get_webmcp_tools()
	names = {t.name for t in page_tools.tools}

	assert 'next_page' in names, f'no next_page among {sorted(names)}'
	assert 'previous_page' in names, f'no previous_page among {sorted(names)}'
	assert any(n.startswith('switch_to_') for n in names), f'no tab verbs among {sorted(names)}'

	result = await browser_session.call_webmcp_tool('next_page', {})
	assert result.ok, result.error
	assert await _state_text(browser_session) == 'page 2'


async def test_a_toggle_is_set_not_flipped(browser_session, dashboard_server):
	"""Calling set_x(on=True) twice must leave it on.

	Clicking unconditionally is the bug people ship here: the second call toggles it back
	off, and the agent concludes the control does not work.
	"""
	await _goto(browser_session, dashboard_server.url_for('/dashboard'))
	page_tools = await browser_session.get_webmcp_tools()

	toggle = next((t for t in page_tools.tools if t.name.startswith('set_')), None)
	assert toggle is not None, f'no toggle among {[t.name for t in page_tools.tools]}'
	assert toggle.input_schema['properties']['on']['type'] == 'boolean'

	assert (await browser_session.call_webmcp_tool(toggle.name, {'on': True})).ok
	assert await _state_text(browser_session) == 'only open: true'

	# Idempotent: asking for the state it is already in must not flip it.
	assert (await browser_session.call_webmcp_tool(toggle.name, {'on': True})).ok
	checked = await browser_session.run_page_script("return document.getElementById('only-open').checked;")
	assert json.loads(checked.value) is True

	assert (await browser_session.call_webmcp_tool(toggle.name, {'on': False})).ok
	assert await _state_text(browser_session) == 'only open: false'


async def test_a_site_is_learned_once_and_reused_next_session(browser_session, shop_server, tmp_path):
	"""The second agent to visit a site inherits what the first one worked out."""
	from browser_use.synthesis.store import ManifestStore

	store_path = tmp_path / 'site_tools.json'
	await _goto(browser_session, shop_server.url_for('/ordinary'))

	first = SiteToolSynthesizer(browser_session, store=ManifestStore(path=store_path, enabled=True))
	learned = await first.synthesize()
	assert learned.tools and learned.fingerprint
	assert store_path.exists(), 'nothing was written to disk'

	# A brand new synthesizer, as a later session would have.
	second = SiteToolSynthesizer(browser_session, store=ManifestStore(path=store_path, enabled=True))
	reused = await second.synthesize()

	assert [t.name for t in reused.tools] == [t.name for t in learned.tools]
	assert reused.created_at == learned.created_at, 'it re-derived instead of reusing'


async def test_verification_survives_the_session_that_proved_it(browser_session, shop_server, tmp_path):
	"""Whether a tool has really run is the most valuable thing to carry forward."""
	from browser_use.synthesis.store import ManifestStore

	store_path = tmp_path / 'site_tools.json'
	await _goto(browser_session, shop_server.url_for('/ordinary'))

	first = SiteToolSynthesizer(browser_session, store=ManifestStore(path=store_path, enabled=True))
	manifest = await first.synthesize()
	search = manifest.get('search')
	assert search is not None and search.verified is False, 'a fresh tool is a guess, not a fact'

	ok, _ = await first.call(search, {'query': 'boots'})
	assert ok

	second = SiteToolSynthesizer(browser_session, store=ManifestStore(path=store_path, enabled=True))
	reloaded = (await second.synthesize()).get('search')
	assert reloaded is not None
	assert reloaded.verified is True, 'the proof that it works was lost'


async def test_a_redesigned_page_is_relearned_not_trusted(browser_session, shop_server, tmp_path):
	"""A stale surface fails in a way that reads as the agent being wrong."""
	from browser_use.synthesis.store import ManifestStore

	store_path = tmp_path / 'site_tools.json'
	await _goto(browser_session, shop_server.url_for('/ordinary'))

	store = ManifestStore(path=store_path, enabled=True)
	before = await SiteToolSynthesizer(browser_session, store=store).synthesize()
	assert 'add_to_cart' in {t.name for t in before.tools}

	# The site ships a redesign: the control is renamed.
	await browser_session.run_page_script(
		"const b = document.getElementById('cart');"
		"b.setAttribute('aria-label', 'Buy it now'); b.textContent = 'Buy it now'; return 1;"
	)

	after = await SiteToolSynthesizer(browser_session, store=ManifestStore(path=store_path, enabled=True)).synthesize()
	names = {t.name for t in after.tools}
	assert 'buy_it_now' in names, f'the rename was not picked up: {sorted(names)}'
	assert 'add_to_cart' not in names, 'the stale tool was served from cache'
	assert after.fingerprint != before.fingerprint


def test_the_fingerprint_tracks_names_not_volume():
	"""Adding a row is not a redesign; renaming a control is."""
	from browser_use.synthesis.store import fingerprint

	base = {'tables': [{'name': 'Orders', 'headers': ['A', 'B'], 'rows': 3}], 'buttons': [{'name': 'Sign in'}]}
	more_rows = {'tables': [{'name': 'Orders', 'headers': ['A', 'B'], 'rows': 900}], 'buttons': [{'name': 'Sign in'}]}
	renamed = {'tables': [{'name': 'Orders', 'headers': ['A', 'B'], 'rows': 3}], 'buttons': [{'name': 'Log in'}]}

	assert fingerprint(base) == fingerprint(more_rows), 'more data is not a new tool surface'
	assert fingerprint(base) != fingerprint(renamed), 'a renamed control breaks its locator'


def test_a_corrupt_cache_is_ignored_not_fatal(tmp_path):
	from browser_use.synthesis.store import ManifestStore

	path = tmp_path / 'site_tools.json'
	path.write_text('{ this is not json')
	assert ManifestStore(path=path, enabled=True).origins == []

	path.write_text(json.dumps({'https://x.example': {'nonsense': True}}))
	assert ManifestStore(path=path, enabled=True).origins == []


# A site built out of web components. querySelectorAll stops at the shadow boundary, so
# without piercing, every control below is invisible — this is the "UI you can't even see"
# case that makes DOM scraping fail on modern sites.
SHADOW_PAGE = """<!DOCTYPE html>
<html><head><title>Component Shop</title></head><body>
	<search-box></search-box>
	<order-table></order-table>
	<div id="out">idle</div>
<script>
	const say = t => document.getElementById('out').textContent = t;

	customElements.define('search-box', class extends HTMLElement {
		connectedCallback() {
			const root = this.attachShadow({mode: 'open'});
			root.innerHTML = `
				<form>
					<input type="search" id="needle" aria-label="Search orders">
					<button type="submit">Find</button>
				</form>`;
			root.querySelector('form').addEventListener('submit', e => {
				e.preventDefault();
				say('found ' + root.getElementById('needle').value);
			});
		}
	});

	customElements.define('order-table', class extends HTMLElement {
		connectedCallback() {
			this.attachShadow({mode: 'open'}).innerHTML = `
				<table>
					<caption>Shadow orders</caption>
					<thead><tr><th>Ref</th><th>Status</th></tr></thead>
					<tbody>
						<tr><td>S-1</td><td>shipped</td></tr>
						<tr><td>S-2</td><td>pending</td></tr>
					</tbody>
				</table>`;
		}
	});
</script>
</body></html>"""


@pytest.fixture(scope='module')
def shadow_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/shadow').respond_with_data(SHADOW_PAGE, content_type='text/html')
	yield server
	server.stop()


async def test_controls_inside_web_components_are_found(browser_session, shadow_server):
	"""querySelectorAll stops at the shadow boundary; the scanner must not."""
	await _goto(browser_session, shadow_server.url_for('/shadow'))

	# Confirm the premise rather than assuming it: a plain query really does see nothing.
	plain = await browser_session.run_page_script(
		"return {forms: document.querySelectorAll('form').length, inputs: document.querySelectorAll('input').length};"
	)
	assert json.loads(plain.value) == {'forms': 0, 'inputs': 0}, 'the fixture is not actually using shadow DOM'

	page_tools = await browser_session.get_webmcp_tools()
	names = {tool.name for tool in page_tools.tools}
	assert 'search' in names, f'the shadow search form was missed: {sorted(names)}'
	assert any(n.startswith('read_') for n in names), f'the shadow table was missed: {sorted(names)}'


async def test_a_tool_inside_a_web_component_actually_runs(browser_session, shadow_server):
	await _goto(browser_session, shadow_server.url_for('/shadow'))
	await browser_session.get_webmcp_tools()

	result = await browser_session.call_webmcp_tool('search', {'query': 'S-2'})
	assert result.ok, result.error

	out = await browser_session.run_page_script("return document.getElementById('out').textContent;")
	assert json.loads(out.value) == 'found S-2'


async def test_a_table_inside_a_web_component_reads(browser_session, shadow_server):
	await _goto(browser_session, shadow_server.url_for('/shadow'))
	page_tools = await browser_session.get_webmcp_tools()
	read_tool = next(t for t in page_tools.tools if t.name.startswith('read_'))

	result = await browser_session.call_webmcp_tool(read_tool.name, {})
	assert result.ok, result.error
	rows = json.loads(result.content.split('\n')[0])
	assert rows == [{'Ref': 'S-1', 'Status': 'shipped'}, {'Ref': 'S-2', 'Status': 'pending'}]


# A page whose real controls sit behind an open modal. Everything underneath is painted
# but unreachable, which is exactly the case where a plausible-looking tool clicks nothing.
MODAL_PAGE = """<!DOCTYPE html>
<html><head><title>Consent</title></head><body>
	<main aria-hidden="true">
		<button id="buy">Buy now</button>
		<form><input aria-label="Search catalogue"><button type="submit">Search</button></form>
	</main>
	<dialog id="consent" open aria-label="Cookie choices">
		<button id="accept">Accept all</button>
		<button id="reject">Reject all</button>
	</dialog>
	<div id="said">idle</div>
<script>
	const say = t => document.getElementById('said').textContent = t;
	accept.addEventListener('click', () => { say('accepted'); consent.close(); });
	reject.addEventListener('click', () => { say('rejected'); consent.close(); });
	buy.addEventListener('click', () => say('bought'));
</script>
</body></html>"""


@pytest.fixture(scope='module')
def modal_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/modal').respond_with_data(MODAL_PAGE, content_type='text/html')
	yield server
	server.stop()


async def test_only_the_open_modal_is_offered(browser_session, modal_server):
	"""Tools for the page behind a modal look fine and click nothing."""
	await _goto(browser_session, modal_server.url_for('/modal'))

	page_tools = await browser_session.get_webmcp_tools()
	names = {tool.name for tool in page_tools.tools}

	assert 'accept_all' in names and 'reject_all' in names, f'the dialog was missed: {sorted(names)}'
	assert 'buy_now' not in names, 'a control behind the modal was offered'
	assert 'search' not in names, 'a form behind the modal was offered'


async def test_the_page_underneath_comes_back_once_the_modal_closes(browser_session, modal_server):
	await _goto(browser_session, modal_server.url_for('/modal'))
	await browser_session.get_webmcp_tools()

	result = await browser_session.call_webmcp_tool('accept_all', {})
	assert result.ok, result.error
	said = await browser_session.run_page_script("return document.getElementById('said').textContent;")
	assert json.loads(said.value) == 'accepted'

	# The dialog closed and aria-hidden went with it, so the real page is operable again.
	await browser_session.run_page_script("document.querySelector('main').removeAttribute('aria-hidden'); return 1;")
	synthesizer = SiteToolSynthesizer(browser_session)
	names = {tool.name for tool in (await synthesizer.synthesize()).tools}
	assert 'buy_now' in names, f'the page did not come back: {sorted(names)}'


async def test_a_modal_surface_is_never_cached_as_the_site(browser_session, modal_server, tmp_path):
	"""It is correct now and wrong the moment the dialog closes."""
	from browser_use.synthesis.store import ManifestStore

	store = ManifestStore(path=tmp_path / 'site_tools.json', enabled=True)
	await _goto(browser_session, modal_server.url_for('/modal'))

	manifest = await SiteToolSynthesizer(browser_session, store=store).synthesize()
	assert manifest.modal == 'Cookie choices'
	assert store.origins == [], 'a transient dialog was written to the site cache'


async def test_the_prompt_separates_proven_tools_from_untried_ones(browser_session, shop_server, tmp_path):
	"""A tool that has run before is a different proposition from one never tried."""
	from browser_use.agent.prompts import AgentMessagePrompt
	from browser_use.filesystem.file_system import FileSystem
	from browser_use.synthesis.store import ManifestStore

	await _goto(browser_session, shop_server.url_for('/ordinary'))

	# Prove one tool, leave the rest inferred.
	store = ManifestStore(path=tmp_path / 'site_tools.json', enabled=True)
	synthesizer = SiteToolSynthesizer(browser_session, store=store)
	manifest = await synthesizer.synthesize()
	search = manifest.get('search')
	assert search is not None
	ok, _ = await synthesizer.call(search, {'query': 'boots'})
	assert ok

	# Feed that state through the same path the agent's prompt uses.
	if browser_session._webmcp_watchdog:
		service = browser_session._webmcp_watchdog.service
		service._cache.clear()
		service._synthesizer = synthesizer

	state = await browser_session.get_browser_state_summary(include_screenshot=False)
	rendered = str(
		AgentMessagePrompt(
			browser_state_summary=state,
			file_system=FileSystem(base_dir=tmp_path / 'fs', create_default_files=False),
		)
		.get_user_message(use_vision=False)
		.content
	)

	assert 'run successfully before' in rendered
	assert 'have not been run yet' in rendered
	proven_at = rendered.index('run successfully before')
	assert rendered.index('- search(', proven_at) < rendered.index('have not been run yet')


# What a real news front page looks like: a search box buried among headlines, usernames
# and timestamps, all of them anchors. Running synthesis over the live web turned pages
# like this into two dozen "tools" named after their content.
NOISY_PAGE = """<!DOCTYPE html><html><head><title>Feed</title></head><body>
	<form><input name="q"><button type="submit">Go</button></form>
	<a href="/a">Pirate face rescues LLM from a very long headline that keeps going on and on</a>
	<a href="/b">skepticalgenius</a>
	<a href="/c">3 hours ago</a>
	<a href="/d">82 comments</a>
	<a href="/e">External link</a>
	<a href="/f">External link</a>
	<a href="/g">External link</a>
	<a href="/h">External link</a>
	<button id="real">Add comment</button>
</body></html>"""


@pytest.fixture(scope='module')
def noisy_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/noisy').respond_with_data(NOISY_PAGE, content_type='text/html')
	yield server
	server.stop()


async def test_page_content_does_not_become_tools(browser_session, noisy_server):
	"""Measured against the live web: this is what filled the budget with junk."""
	await _goto(browser_session, noisy_server.url_for('/noisy'))
	page_tools = await browser_session.get_webmcp_tools()
	names = {tool.name for tool in page_tools.tools}

	# Headlines, usernames, timestamps and comment counts are content, not verbs.
	assert not any('pirate' in n for n in names), f'a headline became a tool: {sorted(names)}'
	assert not any('skepticalgenius' in n for n in names), f'a username became a tool: {sorted(names)}'
	assert not any('hours_ago' in n or 'comments' in n for n in names), f'metadata became a tool: {sorted(names)}'

	# Repeated boilerplate is not four different tools either.
	assert len([n for n in names if n.startswith('external_link')]) == 0, f'boilerplate links: {sorted(names)}'

	# What genuinely is actionable survives.
	assert 'search' in names, f'the search form was lost: {sorted(names)}'
	assert 'add_comment' in names, f'a real button was lost: {sorted(names)}'


async def test_a_bare_search_input_is_still_recognised_as_search(browser_session, noisy_server):
	"""Large sites ship <input name="q"> with no label and no obvious submit.

	Recognising search only by the words around it named this submit_form(q) — correct and
	useless, because a model looking for a way to search will not find it under that name.
	"""
	await _goto(browser_session, noisy_server.url_for('/noisy'))
	page_tools = await browser_session.get_webmcp_tools()

	search = next((t for t in page_tools.tools if t.name == 'search'), None)
	assert search is not None
	assert 'query' in search.input_schema['properties'], search.input_schema
