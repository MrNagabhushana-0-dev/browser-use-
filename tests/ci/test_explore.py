"""The explorer, against a small site whose every bug is planted and known.

Real browser, pytest-httpserver, nothing mocked. Each planted bug must be found on the page it
is on; the clean page must come back clean; robots.txt must be obeyed; a challenge page must be
reported as a wall rather than as content; and the session's own CDP listeners must survive.
"""

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.explore import Explorer, detect, render_markdown, render_sheet

HOME = """<!doctype html><html lang="en"><head><title>Shop | Deals | Deals</title></head><body>
<h1>Welcome</h1><h1>Also welcome</h1><h3>Skipped a level</h3>
<a href="/clean">clean</a> <a href="/phone">phone</a> <a href="/missing">gone</a> <a href="/private/x">private</a>
<a href="/challenge">challenge</a> <a href="/crash">crash</a>
<img src="/nope.png" alt="broken on purpose"> <img src="/ok.png">
<button><svg width="10" height="10"></svg></button>
<div style="width:3000px;height:10px;background:#eee"></div>
<div style="height:2400px"></div><p id="dup">a</p><p id="dup">b</p>
<script>console.error('planted console error'); setTimeout(() => { throw new Error('planted exception') }, 50)</script>
</body></html>"""

CLEAN = """<!doctype html><html lang="en"><head><title>Clean page</title><link rel="icon" href="/ok.png">
<meta name="description" content="Nothing wrong here."></head><body><main><h1>Clean</h1><p>All good.</p>
<img src="/ok.png" alt="a dot"><button>Buy</button></main></body></html>"""

PHONE = """<!doctype html><html lang="en"><head><title>Phone</title><link rel="icon" href="/ok.png">
<meta name="description" content="Too wide for a phone."><meta name="viewport" content="width=device-width">
</head><body><h1>Phone</h1><div style="width:600px;height:20px;background:#ccc">fixed width</div></body></html>"""

CRASH = '<!doctype html><html><head><title></title></head><body><h2>Application error: a client-side exception has occurred (see the browser console for more information).</h2><script>console.error("TypeError: cannot read properties of null")</script></body></html>'

CHALLENGE = '<!doctype html><html><head><title>Just a moment...</title></head><body>Checking your browser.</body></html>'

PNG = bytes.fromhex(
	'89504e470d0a1a0a0000000d4948445200000001000000010806000000'
	'1f15c4890000000d49444154789c6360000002000154a24f5f0000000049454e44ae426082'
)


@pytest.fixture(scope='module')
def site():
	server = HTTPServer()
	server.start()
	base = server.url_for('')
	server.expect_request('/').respond_with_data(HOME, content_type='text/html')
	server.expect_request('/clean').respond_with_data(CLEAN, content_type='text/html')
	server.expect_request('/phone').respond_with_data(PHONE, content_type='text/html')
	server.expect_request('/challenge').respond_with_data(CHALLENGE, content_type='text/html')
	server.expect_request('/crash').respond_with_data(CRASH, content_type='text/html')
	server.expect_request('/missing').respond_with_data('not here', status=404, content_type='text/html')
	server.expect_request('/nope.png').respond_with_data('', status=404)
	server.expect_request('/favicon.ico').respond_with_data('', status=404)
	server.expect_request('/ok.png').respond_with_data(PNG, content_type='image/png')
	server.expect_request('/robots.txt').respond_with_data('User-agent: *\nDisallow: /private\n', content_type='text/plain')
	server.expect_request('/sitemap.xml').respond_with_data(
		'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
		f'<url><loc>{base}clean</loc></url><url><loc>{base}private/y</loc></url></urlset>',
		content_type='application/xml',
	)
	yield server
	server.stop()


@pytest.fixture(scope='module')
async def explored(site):
	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=False))
	await session.start()
	registry = session.cdp_client._event_registry
	before = registry._handlers.get('Network.responseReceived')
	explorer = Explorer(session, settle_s=0.8, dom_state=True)
	report = await explorer.run(site.url_for('/'))
	after = registry._handlers.get('Network.responseReceived')
	yield report, explorer, before, after
	await session.kill()


def _on(report, path: str):
	return [f for f in report.findings if any(u.endswith(path) for u in f.pages)]


def _titles(findings) -> str:
	return '\n'.join(f'[{f.kind}] {f.title}' for f in findings)


async def test_every_planted_bug_on_the_home_page_is_found(explored):
	report = explored[0]
	home = _on(report, '/')
	kinds = {f.kind for f in home}
	text = _titles(home)
	for kind in ('broken-image', 'a11y-alt', 'a11y-name', 'layout', 'js-exception', 'console', 'head', 'seo', 'structure'):
		assert kind in kinds, f'{kind} not found on /:\n{text}'
	assert 'planted exception' in text and 'planted console error' in text
	assert 'repeats a segment' in text and '2 visible <h1>' in text and 'Heading levels skip' in text
	assert 'sideways on desktop' in text


async def test_the_clean_page_comes_back_clean(explored):
	report = explored[0]
	clean = [f for f in _on(report, '/clean') if f.severity != 'info']
	assert clean == [], _titles(clean)


async def test_a_phone_sized_viewport_catches_what_desktop_does_not(explored):
	report = explored[0]
	phone = _on(report, '/phone')
	assert any(f.kind == 'layout-mobile' for f in phone), _titles(phone)
	assert not any(f.kind == 'layout' for f in phone), 'fine on a desktop'


async def test_robots_txt_is_obeyed_and_dead_links_are_reported(explored):
	report = explored[0]
	visited = {p.url.rsplit('/', 2)[-1] for p in report.pages}
	assert not any('private' in p.url for p in report.pages), visited
	assert any('private' in s and 'robots' in s for s in report.skipped), report.skipped
	broken = [f for f in report.findings if f.kind == 'broken-link']
	assert any('/missing' in f.title and '404' in f.title for f in broken), _titles(report.findings)
	assert not any('private' in f.title for f in broken), 'a disallowed link is not fetched either'


async def test_a_crashed_page_is_one_high_finding_not_a_list_of_symptoms(explored):
	report = explored[0]
	crash = _on(report, '/crash')
	assert [f.kind for f in crash] == ['page-crash'] and crash[0].severity == 'high', _titles(crash)
	assert any('cannot read properties' in e for e in crash[0].evidence), crash[0].evidence


async def test_a_challenge_page_is_a_wall_not_content(explored):
	report = explored[0]
	page = next(p for p in report.pages if p.url.endswith('/challenge'))
	assert page.error.startswith('blocked: cloudflare-challenge'), page.error
	assert detect('https://www.google.com/sorry/index?continue=x') is not None
	assert detect('https://example.com/contact', 'Contact', 'x' * 500, ['https://www.google.com/recaptcha/api.js']) is None


async def test_the_run_is_costed_timed_and_rendered(explored):
	report, explorer, _, _ = explored
	assert report.tokens > 0 and report.seconds > 0 and len(report.pages) >= 4
	assert report.dom_state_tokens > 0, 'the DOM-dump comparison was measured'
	md = render_markdown(report)
	assert '## Findings' in md and 'tokens read' in md and '| /clean |' in md
	assert render_sheet(explorer.looks) is not None


async def test_the_sessions_own_cdp_listeners_survive_the_run(explored):
	_, _, before, after = explored
	assert after is before, 'the downloads watchdog (and anyone else) keeps its Network.responseReceived handler'
