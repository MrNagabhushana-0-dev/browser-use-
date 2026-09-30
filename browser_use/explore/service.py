"""Explore a whole site the way a careful person would, and write down what is broken.

    explorer = Explorer(browser_session)
    report = await explorer.run('https://example.com/')
    print(render_markdown(report))

For every page it can reach (the start page's links plus the site's own sitemap, minus what
robots.txt disallows) it loads the page, scrolls to the bottom with real wheel input so lazy
content arrives, then measures instead of eyeballing: console errors, failed and 4xx/5xx
requests, broken and unlabelled images, controls without accessible names, sideways overflow on
a desktop and on a phone-sized viewport, heading order, duplicate ids, slow loads, layout
shift, heavy assets, bot walls. Same-origin links found anywhere are checked for status.

While it works, a meter in the corner of the page shows how far it is, how long it has taken,
how long the rest should take, and how many tokens the run has cost. The cost is what an agent
reading the results would pay: a short digest per page plus one small look at it (a screencast
frame, not a screenshot call), against what a DOM-dump step would cost on the same page.

It never submits a form, never logs in, and only follows links on the start URL's origin.
"""

import asyncio
import io
import json
import logging
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from browser_use.explore import walls
from browser_use.explore.views import SEVERITY_ORDER, ExploreReport, Finding, PageReport
from browser_use.eyes.percept import estimate_image_tokens
from browser_use.human.input import HumanInput
from browser_use.vision.live import LiveView
from browser_use.vision.overlay import Overlay

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

_PROBE = (Path(__file__).parent / 'probe.js').read_text()
SLOW_LOAD_MS = 4000
LOOK_WIDTH = 360
MOBILE = {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True}
# Console noise that is the environment's, not the site's.
_IGNORED_CONSOLE = ('Automatic fallback to software WebGL', 'GroupMarkerNotSet')


class Explorer:
	def __init__(
		self,
		browser_session: 'BrowserSession',
		*,
		max_pages: int = 40,
		scroll: bool = True,
		mobile: bool = True,
		look: bool = True,
		hud: bool = True,
		dom_state: bool = True,
		max_screens: int = 30,
		settle_s: float = 2.0,
	) -> None:
		assert max_pages >= 1, 'explore at least one page'
		self.session = browser_session
		self.max_pages = max_pages
		self.scroll = scroll
		self.mobile = mobile
		self.look = look
		self.hud = hud
		self.dom_state = dom_state
		self.max_screens = max_screens
		self.settle_s = settle_s
		self.hand = HumanInput(browser_session)
		self.overlay = Overlay(browser_session)
		self.looks: list[tuple[str, bytes]] = []
		self._console: list[str] = []
		self._failed: list[str] = []
		self._started = 0.0
		self._cdp: Any = None
		self._restore: list[tuple[str, Any, Any]] = []
		self._disallowed_rules: list[str] = []

	# -- plumbing ------------------------------------------------------------------------

	async def _eval(self, expression: str, timeout: float = 30.0) -> Any:
		r = await asyncio.wait_for(
			self._cdp.cdp_client.send.Runtime.evaluate(
				params={'expression': expression, 'returnByValue': True, 'awaitPromise': True}, session_id=self._cdp.session_id
			),
			timeout,
		)
		if r.get('exceptionDetails'):
			raise RuntimeError(
				(r['exceptionDetails'].get('exception') or {}).get('description') or r['exceptionDetails'].get('text')
			)
		return (r.get('result') or {}).get('value')

	async def _listen(self) -> None:
		send = self._cdp.cdp_client.send
		sid = self._cdp.session_id
		await send.Log.enable(session_id=sid)
		await send.Network.enable(session_id=sid)
		await send.Runtime.enable(session_id=sid)

		def log_entry(e: dict, s: str | None = None) -> None:
			entry = e.get('entry', {})
			if entry.get('level') in ('error', 'warning'):
				text = f'{entry.get("level")}: {(entry.get("text") or "")[:220]} {(entry.get("url") or "")[-100:]}'.strip()
				if not any(x in text for x in _IGNORED_CONSOLE):
					self._console.append(text)

		def exception(e: dict, s: str | None = None) -> None:
			d = e.get('exceptionDetails', {})
			self._console.append(f'exception: {((d.get("exception") or {}).get("description") or d.get("text") or "")[:300]}')

		def console_api(e: dict, s: str | None = None) -> None:
			if e.get('type') in ('error', 'warning', 'assert'):
				args = ' '.join(str(a.get('value', a.get('description', ''))) for a in e.get('args', []))[:220]
				text = f'{"warning" if e.get("type") == "warning" else "error"}: {args}'
				if not any(x in text for x in _IGNORED_CONSOLE):
					self._console.append(text)

		def response(e: dict, s: str | None = None) -> None:
			status = e.get('response', {}).get('status', 0)
			if status >= 400:
				self._failed.append(f'{status} {e["response"]["url"][:180]}')

		def failed(e: dict, s: str | None = None) -> None:
			if e.get('errorText') not in ('net::ERR_ABORTED',) and not e.get('canceled'):
				self._failed.append(f'{e.get("errorText")} request {e.get("requestId")}')

		for method, fn in (
			('Log.entryAdded', log_entry),
			('Runtime.exceptionThrown', exception),
			('Runtime.consoleAPICalled', console_api),
			('Network.responseReceived', response),
			('Network.loadingFailed', failed),
		):
			self._chain(method, fn)

	def _chain(self, method: str, fn: Any) -> None:
		"""Listen on a CDP event without taking the slot from whoever holds it.

		cdp-use keeps one callback per event; the downloads watchdog, for one, lives on
		Network.responseReceived. Replacing it would quietly break downloads for the session.
		"""
		registry = self.session.cdp_client._event_registry
		incumbent = registry._handlers.get(method)

		def both(event: Any, session_id: str | None = None) -> Any:
			try:
				fn(event, session_id)
			except Exception as e:
				logger.debug(f'explorer listener for {method} failed: {e}')
			return incumbent(event, session_id) if incumbent is not None else None

		self._restore.append((method, incumbent, both))
		registry.register(method, both)

	def _unlisten(self) -> None:
		registry = self.session.cdp_client._event_registry
		for method, incumbent, ours in reversed(self._restore):
			if registry._handlers.get(method) is ours:
				if incumbent is not None:
					registry.register(method, incumbent)
				else:
					registry.unregister(method)
		self._restore.clear()

	async def _meter(self, done: int, total: int, url: str, report: ExploreReport, phase: str) -> None:
		if not self.hud:
			return
		elapsed = time.monotonic() - self._started
		per_page = elapsed / done if done else None
		eta = f'{per_page * (total - done):.0f}s' if per_page is not None else '...'
		findings = sum(len(f.pages) for f in report.findings) if report.findings else 0
		lines = [
			'browser-use explorer',
			f'page {min(done + 1, total)}/{total}  {phase}',
			urlparse(url).path[:34] or '/',
			f'elapsed {elapsed:5.0f}s   left ~{eta}',
			f'tokens  ~{report.tokens:,}',
			f'DOM-dump would be ~{report.dom_state_tokens:,}' if report.dom_state_tokens else 'DOM-dump cost: measuring',
			f'issues so far {findings}',
		]
		try:
			await self.overlay.show(lines)
		except Exception:
			pass
		logger.info(' | '.join(lines[1:5]))

	# -- discovery -----------------------------------------------------------------------

	async def _fetch_text(self, url: str) -> tuple[int, str]:
		"""GET through the page itself, so cookies, proxy and TLS are exactly the browser's."""
		value = await self._eval(
			f"fetch({json.dumps(url)}, {{credentials: 'same-origin'}}).then(async r => JSON.stringify([r.status, (await r.text()).slice(0, 400000)])).catch(e => JSON.stringify([0, String(e)]))"
		)
		status, text = json.loads(value)
		return int(status), text

	async def _status(self, url: str) -> int:
		value = await self._eval(
			f"fetch({json.dumps(url)}, {{method: 'GET', credentials: 'same-origin', redirect: 'follow'}}).then(r => r.status).catch(() => 0)"
		)
		return int(value or 0)

	@staticmethod
	def _disallowed(robots: str) -> list[str]:
		rules: list[str] = []
		applies = False
		for raw in robots.splitlines():
			line = raw.split('#', 1)[0].strip()
			if ':' not in line:
				continue
			field, value = (x.strip() for x in line.split(':', 1))
			if field.lower() == 'user-agent':
				applies = value == '*'
			elif applies and field.lower() == 'disallow' and value:
				rules.append(value)
		return rules

	async def _discover(self, start: str, report: ExploreReport) -> list[str]:
		origin = f'{urlparse(start).scheme}://{urlparse(start).netloc}'
		status, robots = await self._fetch_text(origin + '/robots.txt')
		disallow = self._disallowed(robots) if status == 200 else []
		self._disallowed_rules = disallow
		urls: list[str] = [start]
		status, sitemap = await self._fetch_text(origin + '/sitemap.xml')
		if status == 200 and '<urlset' in sitemap:
			try:
				root = ET.fromstring(sitemap)
				for loc in root.iter():
					if loc.tag.endswith('loc') and loc.text:
						urls.append(loc.text.strip())
			except ET.ParseError:
				report.findings.append(
					Finding(kind='sitemap', severity='low', title='sitemap.xml is not valid XML', pages=[origin + '/sitemap.xml'])
				)
		links = await self._eval("JSON.stringify([...document.querySelectorAll('a[href]')].map(a => a.href))")
		urls += json.loads(links or '[]')
		seen: list[str] = []
		for u in urls:
			u = urljoin(start, u).split('#')[0]
			p = urlparse(u)
			if f'{p.scheme}://{p.netloc}' != origin or p.path.lower().endswith(
				('.png', '.jpg', '.jpeg', '.pdf', '.webp', '.svg', '.xml', '.txt', '.json')
			):
				continue
			if any(p.path.startswith(rule) for rule in disallow):
				report.skipped.append(f'{u} (robots.txt)')
				continue
			if u not in seen:
				seen.append(u)
		if len(seen) > self.max_pages:
			report.skipped += [f'{u} (page cap)' for u in seen[self.max_pages :]]
			seen = seen[: self.max_pages]
		return seen

	# -- one page ------------------------------------------------------------------------

	async def _scroll_through(self) -> int:
		screens = 0
		size = json.loads(await self._eval('JSON.stringify([innerWidth, innerHeight])'))
		await self.hand.move_to(size[0] * 0.55, size[1] * 0.5)
		for _ in range(self.max_screens):
			before = await self._eval('scrollY + innerHeight >= document.documentElement.scrollHeight - 4 ? -1 : scrollY')
			if before == -1:
				break
			await self.hand.wheel(size[1] * 0.85)
			await asyncio.sleep(0.35)
			after = await self._eval('scrollY')
			screens += 1
			if after == before:  # a page that scrolls something else, or not at all
				break
		await self._eval('window.scrollTo(0, 0)')
		return screens

	async def _look(self, live: LiveView | None) -> bytes | None:
		if live is None or not live.frames:
			return None
		try:
			from PIL import Image

			img = Image.open(io.BytesIO(live.frames[-1].data)).convert('RGB')
			img.thumbnail((LOOK_WIDTH, LOOK_WIDTH))
			buf = io.BytesIO()
			img.save(buf, 'JPEG', quality=70)
			return buf.getvalue()
		except Exception:
			return None

	async def _mobile_check(self, url: str) -> dict[str, Any]:
		send = self._cdp.cdp_client.send
		sid = self._cdp.session_id
		try:
			await send.Emulation.setDeviceMetricsOverride(params=MOBILE, session_id=sid)
			await send.Page.navigate(params={'url': url}, session_id=sid)
			await asyncio.sleep(self.settle_s + 1.0)
			value = await self._eval(
				"""JSON.stringify({overflow: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
				culprits: [...document.body.querySelectorAll('*')].filter(e => { const r = e.getBoundingClientRect(); return r.width && r.right > document.documentElement.clientWidth + 2 && getComputedStyle(e).position !== 'fixed' }).slice(0, 4).map(e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (typeof e.className === 'string' && e.className ? '.' + e.className.trim().split(/\\s+/)[0] : '') + ' ' + Math.round(e.getBoundingClientRect().right) + 'px'),
				tiny: [...document.querySelectorAll('p, li, span, a')].filter(e => e.innerText && e.innerText.trim().length > 20 && parseFloat(getComputedStyle(e).fontSize) < 12).length})"""
			)
			return json.loads(value)
		except Exception as e:
			return {'error': f'{type(e).__name__}: {e}'}
		finally:
			try:
				await send.Emulation.clearDeviceMetricsOverride(session_id=sid)
			except Exception:
				pass

	async def _dom_state_tokens(self) -> int | None:
		if not self.dom_state:
			return None
		try:
			state = await asyncio.wait_for(self.session.get_browser_state_summary(include_screenshot=False), 30)
			return max(1, len(state.dom_state.llm_representation()) // 4)
		except Exception as e:
			logger.debug(f'explorer: DOM state unavailable: {type(e).__name__}: {e}')
			return None

	async def _page(self, url: str, live: LiveView | None) -> PageReport:
		self._console.clear()
		self._failed.clear()
		page = PageReport(url=url)
		started = time.monotonic()
		try:
			await self.session.navigate_to(url)
		except Exception as e:
			page.error = str(e)[:400]
			page.seconds = time.monotonic() - started
			return page
		page.navigation_seconds = time.monotonic() - started
		await asyncio.sleep(self.settle_s)
		wall = walls.detect(**json.loads(await self._eval(walls.PROBE_JS)))
		if wall:
			page.error = f'blocked: {wall.kind} ({wall.evidence}); {wall.advice}'
		look = await self._look(live) if self.look else None
		page.dom_state_tokens = await self._dom_state_tokens()
		if self.scroll and not wall:
			page.screens_scrolled = await self._scroll_through()
			await asyncio.sleep(0.5)
		try:
			page.probe = json.loads(await self._eval(_PROBE, timeout=45))
		except Exception as e:
			page.error = page.error or f'probe failed: {type(e).__name__}: {e}'
		page.title = page.probe.get('title', '')
		page.status = (page.probe.get('timing') or {}).get('status')
		page.console = list(dict.fromkeys(self._console))[:20]
		page.failed_requests = list(dict.fromkeys(self._failed))[:20]
		if self.mobile and not wall:
			page.mobile = await self._mobile_check(url)
		if look:
			self.looks.append((url, look))
			w, h = _jpeg_size(look)
			page.image_tokens = estimate_image_tokens(w, h)
		page.digest_tokens = max(1, len(_digest(page)) // 4)
		page.seconds = time.monotonic() - started
		return page

	# -- the run -------------------------------------------------------------------------

	async def run(self, start_url: str) -> ExploreReport:
		self._started = time.monotonic()
		report = ExploreReport(start_url=start_url)
		self._cdp = await self.session.get_or_create_cdp_session(focus=False)
		await self._listen()
		if self.hud:
			await self.overlay.install()
		live: LiveView | None = None
		if self.look:
			live = LiveView(self.session)
			try:
				await live.start(max_width=720, quality=55)
			except Exception as e:
				logger.debug(f'explorer: no screencast: {e}')
				live = None
		try:
			await self.session.navigate_to(start_url)
			await asyncio.sleep(self.settle_s)
			report.environment = json.loads(
				await self._eval(
					"""JSON.stringify({userAgent: navigator.userAgent, cores: navigator.hardwareConcurrency, memoryGB: navigator.deviceMemory,
					gpu: (() => { try { const g = document.createElement('canvas').getContext('webgl'); const d = g && g.getExtension('WEBGL_debug_renderer_info'); return d ? g.getParameter(d.UNMASKED_RENDERER_WEBGL) : (g ? 'webgl' : 'none') } catch (e) { return 'error' } })(),
					viewport: innerWidth + 'x' + innerHeight, dpr: devicePixelRatio, reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
					siteDataset: Object.assign({}, document.documentElement.dataset)})"""
				)
			)
			urls = await self._discover(start_url, report)
			for i, url in enumerate(urls):
				await self._meter(i, len(urls), url, report, 'loading')
				page = await self._page(url, live)
				report.pages.append(page)
				_merge(report, _findings_for(page))
				await self._meter(i + 1, len(urls), url, report, 'done')
			await self._check_links(report)
		finally:
			self._unlisten()
			if live is not None:
				try:
					await live.stop()
				except Exception:
					pass
		report.findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], -len(f.pages), f.title))
		report.seconds = time.monotonic() - self._started
		await self._meter(len(report.pages), len(report.pages), start_url, report, 'finished')
		return report

	async def _check_links(self, report: ExploreReport) -> None:
		visited = {p.url for p in report.pages}
		origin = urlparse(report.start_url).netloc
		links: dict[str, list[str]] = {}
		for p in report.pages:
			for link in p.probe.get('links', []):
				if urlparse(link).netloc == origin and link not in visited:
					links.setdefault(link, []).append(p.url)
		links = {u: v for u, v in links.items() if not any(urlparse(u).path.startswith(r) for r in self._disallowed_rules)}
		for link in list(links)[:80]:
			report.link_status[link] = await self._status(link)
		broken = {u: s for u, s in report.link_status.items() if s >= 400 or s == 0}
		for u, s in broken.items():
			_merge(
				report,
				[
					Finding(
						kind='broken-link',
						severity='high',
						title=f'Link to {urlparse(u).path} returns {s or "no response"}',
						pages=links[u],
						evidence=[u],
					)
				],
			)


def _jpeg_size(jpeg: bytes) -> tuple[int, int]:
	from PIL import Image

	with Image.open(io.BytesIO(jpeg)) as img:
		return img.size


def _digest(page: PageReport) -> str:
	"""What an agent reads about one page: the facts, not the markup."""
	p = page.probe
	keep = {
		k: p.get(k)
		for k in ('title', 'h1', 'timing', 'lcp', 'cls', 'media', 'brokenImages', 'unnamedControls', 'horizontalOverflow', 'text')
	}
	return json.dumps(
		{'url': page.url, **keep, 'console': page.console[:5], 'failed': page.failed_requests[:5], 'mobile': page.mobile},
		default=str,
	)


def _merge(report: ExploreReport, found: list[Finding]) -> None:
	index = {f.key: f for f in report.findings}
	for f in found:
		if f.key in index:
			existing = index[f.key]
			existing.pages += [u for u in f.pages if u not in existing.pages]
			existing.evidence += [e for e in f.evidence if e not in existing.evidence][:8]
		else:
			report.findings.append(f)
			index[f.key] = f


def _findings_for(page: PageReport) -> list[Finding]:
	out: list[Finding] = []
	p, url = page.probe, page.url

	def add(kind: str, severity: str, title: str, evidence: list[str] | None = None, detail: str = '') -> None:
		out.append(Finding(kind=kind, severity=severity, title=title, detail=detail, pages=[url], evidence=(evidence or [])[:8]))  # type: ignore[arg-type]

	if page.error:
		blocked = page.error.startswith('blocked')
		add('blocked' if blocked else 'unreachable', 'high', page.error.split(';')[0][:120], [page.error])
		if blocked or not p:
			return out  # a challenge page's headings and meta tags are not the site's
	status = page.status
	if status and status >= 400:
		# Reached by following the site's own links: that is a broken link, and the error page's
		# headings and meta tags say nothing about the site.
		add('broken-link', 'high', f'Link to {urlparse(url).path or "/"} returns {status}', [url])
		return out
	for line in page.console:
		kind = 'js-exception' if line.startswith('exception') else 'console'
		sev = 'high' if kind == 'js-exception' else ('medium' if line.startswith('error') else 'low')
		title = line.split(' http')[0][:110]
		# The same 404'ing asset on every page is one finding, not one per page.
		add(kind, sev, title, [line])
	for f in page.failed_requests:
		code, _, target = f.partition(' ')
		name = target.split('?')[0]
		add('request', 'medium' if code.startswith(('4', '5')) else 'low', f'{code} for {name[-90:]}', [f])
	if p.get('brokenImages'):
		add('broken-image', 'medium', 'Image fails to load', p['brokenImages'])
	if p.get('imagesWithoutAlt'):
		add('a11y-alt', 'low', 'Images without alt text', p['imagesWithoutAlt'])
	if p.get('unnamedControls'):
		add('a11y-name', 'medium', 'Buttons or links with no accessible name', p['unnamedControls'])
	if not p.get('icon'):
		add('head', 'low', 'No <link rel="icon">: browsers fall back to /favicon.ico', [url])
	if not p.get('description'):
		add('seo', 'low', 'No meta description', [url])
	title = p.get('title', '')
	segments = [s.strip() for s in title.split('|')]
	if len(segments) != len(set(segments)):
		add('seo', 'low', 'Page title repeats a segment', [title])
	if len(p.get('h1') or []) != 1:
		add('structure', 'low', f'{len(p.get("h1") or [])} visible <h1> elements (expected 1)', p.get('h1') or [])
	if p.get('headingSkips'):
		add('structure', 'low', 'Heading levels skip', p['headingSkips'])
	if p.get('duplicateIds'):
		add('structure', 'low', 'Duplicate element ids', p['duplicateIds'])
	if p.get('horizontalOverflow'):
		add('layout', 'medium', f'Page scrolls sideways on desktop by {p["horizontalOverflow"]}px', p.get('overflowing'))
	mobile = page.mobile or {}
	if mobile.get('overflow'):
		add(
			'layout-mobile', 'medium', f'Page scrolls sideways on a 390px phone by {mobile["overflow"]}px', mobile.get('culprits')
		)
	if mobile.get('tiny'):
		add('layout-mobile', 'low', f'{mobile["tiny"]} text blocks below 12px on a phone', [url])
	timing = p.get('timing') or {}
	if timing.get('load', 0) > SLOW_LOAD_MS:
		add(
			'performance',
			'medium',
			f'Slow load: {timing["load"] / 1000:.1f}s to the load event',
			[f'{url} ttfb {timing.get("ttfb")}ms dcl {timing.get("dcl")}ms'],
		)
	lcp = p.get('lcp') or {}
	if lcp.get('ms', 0) > 2500:
		add(
			'performance', 'low', f'Largest contentful paint {lcp["ms"] / 1000:.1f}s (over 2.5s)', [f'{url} {lcp.get("element")}']
		)
	if (p.get('cls') or 0) > 0.1:
		add('performance', 'low', f'Layout shift {p["cls"]} (over 0.1)', [url])
	if p.get('heaviest'):
		add('performance', 'info', 'Large assets (over 300 KB)', p['heaviest'])
	if p.get('oversizedImages'):
		add('performance', 'low', 'Images much larger than they are shown', p['oversizedImages'])
	if p.get('blankTargetsWithoutRel'):
		add('security', 'info', f'{p["blankTargetsWithoutRel"]} target=_blank links without rel=noopener', [url])
	return out


def render_markdown(report: ExploreReport, title: str = 'Site exploration report') -> str:
	lines = [f'# {title}', '', f'Start: {report.start_url}']
	env = report.environment
	lines.append(
		f'Explored {len(report.pages)} pages in {report.seconds:.0f}s '
		f'(~{report.seconds / max(1, len(report.pages)):.1f}s per page), '
		f'~{report.tokens:,} tokens read (estimate)'
		+ (f'; a DOM-dump step per page would have been ~{report.dom_state_tokens:,}' if report.dom_state_tokens else '')
		+ '.'
	)
	if env:
		ds = env.get('siteDataset') or {}
		tier = ', '.join(f'{k}={v}' for k, v in ds.items() if 'effect' in k.lower() or 'motion' in k.lower())
		lines.append(
			f'Browser: {env.get("cores")} cores, {env.get("memoryGB")} GB, GPU {env.get("gpu")}, viewport {env.get("viewport")}'
			+ (f'. The site chose: {tier}' if tier else '')
		)
	lines += ['', '## Findings', '']
	if not report.findings:
		lines.append('None.')
	for f in report.findings:
		where = f'{len(f.pages)} page(s)' if len(f.pages) > 3 else ', '.join(urlparse(u).path or '/' for u in f.pages)
		lines.append(f'- **[{f.severity}] {f.title}**: {where}')
		for e in f.evidence[:3]:
			lines.append(f'    - `{e[:200]}`')
	lines += ['', '## Pages', '', '| page | status | load | LCP | screens | issues |', '|---|---|---|---|---|---|']
	for p in report.pages:
		t = p.probe.get('timing') or {}
		lcp = (p.probe.get('lcp') or {}).get('ms')
		n = sum(1 for f in report.findings if p.url in f.pages)
		lines.append(
			f'| {urlparse(p.url).path or "/"} | {p.status or ("error" if p.error else "?")} | {t.get("load", 0) / 1000:.1f}s | '
			f'{(lcp or 0) / 1000:.1f}s | {p.screens_scrolled} | {n} |'
		)
	if report.skipped:
		lines += [
			'',
			f'Skipped: {len(report.skipped)} ({", ".join(report.skipped[:6])}{"..." if len(report.skipped) > 6 else ""})',
		]
	return '\n'.join(lines) + '\n'


def render_sheet(looks: list[tuple[str, bytes]], columns: int = 5) -> bytes | None:
	"""One contact sheet of every page's first look, labelled with its path."""
	if not looks:
		return None
	from PIL import Image, ImageDraw, ImageFont

	tiles = [Image.open(io.BytesIO(j)).convert('RGB') for _, j in looks]
	w = max(t.width for t in tiles)
	h = max(t.height for t in tiles)
	rows = (len(tiles) + columns - 1) // columns
	sheet = Image.new('RGB', (columns * (w + 4), rows * (h + 22)), (12, 12, 12))
	draw = ImageDraw.Draw(sheet)
	try:
		font = ImageFont.load_default(size=13)
	except TypeError:
		font = ImageFont.load_default()
	for i, ((url, _), tile) in enumerate(zip(looks, tiles)):
		x, y = (i % columns) * (w + 4), (i // columns) * (h + 22)
		sheet.paste(tile, (x, y + 20))
		draw.text((x + 3, y + 3), (urlparse(url).path or '/')[:44], fill=(230, 230, 230), font=font)
	buf = io.BytesIO()
	sheet.save(buf, 'JPEG', quality=78)
	return buf.getvalue()
