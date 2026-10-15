"""Follow-up fixes to slice A from the design critiques. Run from the repo root once the affected-file run is done."""

import sys


def sub(path: str, old: str, new: str) -> None:
	s = open(path).read()
	assert s.count(old) == 1, (path, old[:80], s.count(old))
	open(path, 'w').write(s.replace(old, new))


# 1. The archiver keeps out of a scan's way, as it does for a watch: it pulls JPEGs through the same page thread
sub(
	'browser_use/eyes/service.py',
	"""		watcher = self._page_watcher()
		# A second for the sheet and the text after the scroll
		inner = deadline.minus(1.0) if deadline is not None else None
		result = await watcher.scan(max_screens=max_screens, keyframes=keyframes, from_screen=from_screen, deadline=inner)
""",
	"""		watcher = self._page_watcher()
		# A second for the sheet and the text after the scroll
		inner = deadline.minus(1.0) if deadline is not None else None
		# The archiver keeps out of the way, as for a watch: it pulls pictures through the same page thread
		self._watching += 1
		try:
			result = await watcher.scan(
				max_screens=max_screens, keyframes=keyframes, from_screen=from_screen, deadline=inner
			)
		finally:
			self._watching -= 1
""",
)

# 2. A picture of where the page is when no frame came from there: the stream can stop under a scan (a recording
# taking it over, the explorer stopping its own view on the same tab), and the covered range must be what was seen
sub(
	'browser_use/eyes/page.py',
	"""			if not captured:
				# A still page draws nothing new for a scan that has not moved it yet: a picture of it as it is
				shot = await self.capture()
				if shot:
					take(PageFrame(time.monotonic() - started, y, shot))
""",
	"""			if not any(abs(c.scroll_y - y) <= 2 for c in captured[-8:]):
				# Nothing drawn from here: a still page that the scan has not moved yet, or a stream that stopped
				# under the scan (a recording took it over, the explorer stopped its view on this tab). A picture of
				# it as it is, so what the scan says it saw is what it saw
				shot = await asyncio.wait_for(self.capture(), max(0.05, dl.clamp(5.0)))
				if shot:
					take(PageFrame(time.monotonic() - started, y, shot))
""",
)

# 3. Putting the page back gets half a second even when the time is up: a clamp to nothing sends the scroll and
# drops its answer
sub(
	'browser_use/eyes/page.py',
	"""			await self._eval("window.scrollTo({top: 0, behavior: 'instant'})", timeout=dl.clamp(2.0))""",
	"""			await self._eval("window.scrollTo({top: 0, behavior: 'instant'})", timeout=max(0.5, dl.clamp(2.0)))""",
)

# 4. The backstop's words do not guess the effect: effects.failure states it from what the call did
sub(
	'browser_use/retinat/server.py',
	"""						f'{params.name} ran {deadline.spent():.0f}s, past its {deadline.budget_s:.0f}s budget, and was stopped '
						'before the client would give up on it; it may have scrolled or moved the page: look before '
						'calling again'
""",
	"""						f'{params.name} ran {deadline.spent():.0f}s, past its {deadline.budget_s:.0f}s budget, and was stopped '
						'before the client would give up on it'
""",
)

# 5. A wait ends with the call's budget: after a cold browser launch a 50 s wait went past the client's 60 s
sub(
	'browser_use/retinat/server.py',
	"""			event = await reflexes.wait(timeout=float(args.get('timeout', 30)))
			stats = reflexes.stats()
			done = f'{stats["actions"]} input actions so far, decision to input p50 {stats["latency_ms_p50"]} ms'
			if event is None:
				return f'Nothing notified in {float(args.get("timeout", 30)):.0f}s. The reflexes keep running: {done}.'
""",
	"""			timeout = float(args.get('timeout', 30))
			if deadline is not None and deadline.bounded:
				# Within the call's budget: opening the browser first spends the same time the client counts
				timeout = max(0.1, min(timeout, deadline.left() - 0.5))
			event = await reflexes.wait(timeout=timeout)
			stats = reflexes.stats()
			done = f'{stats["actions"]} input actions so far, decision to input p50 {stats["latency_ms_p50"]} ms'
			if event is None:
				return f'Nothing notified in {timeout:.0f}s. The reflexes keep running: {done}.'
""",
)
print('applied', file=sys.stderr)

sub(
	'browser_use/retinat/server.py',
	"""	async def _reflex(self, name: str, args: dict[str, Any]) -> str:""",
	"""	async def _reflex(self, name: str, args: dict[str, Any], deadline: Deadline | None = None) -> str:""",
)
sub(
	'browser_use/retinat/server.py',
	"""			return await self._reflex(name, args)""",
	"""			return await self._reflex(name, args, deadline)""",
)
print('applied the deadline to _reflex', file=sys.stderr)

# 6. Tests: the wait ends with the budget by itself; the backstop is shown on a tool not yet deadline-aware
p = 'tests/ci/test_retinat.py'
s = open(p).read()
start = s.index('async def test_a_tool_that_runs_past_its_budget_is_stopped_and_says_so')
s = s[:start] + '''async def test_a_call_keeps_to_its_budget_by_itself_or_the_backstop_stops_it(tmp_path, monkeypatch, site):
	"""A call held to a 1 s budget. A wait ends with the budget by itself (after a cold browser launch a 50 s wait went
	past the client's 60 s). A tool that does not yet keep to the budget (watch, until it takes a deadline) is stopped
	half a second past it and says so, instead of the client giving up on it; the next call works."""
	import time

	monkeypatch.setenv('BROWSER_USE_EYES_NOW', str(tmp_path / 'now.json'))
	server = RetinatServer(call_budget_s=1, backstop_grace_s=0.5)
	server.config.setdefault('browser_profile', {}).update({'headless': True, 'user_data_dir': str(tmp_path / 'profile')})
	page = (
		'<!doctype html><title>Spin</title><body style="margin:0"><canvas id="c" width="400" height="300"></canvas><script>'
		"const g = document.getElementById('c').getContext('2d'); let t = 0;"
		"(function f() { t++; g.fillStyle = `hsl(${t % 360},70%,50%)`; g.fillRect(0, 0, 400, 300); requestAnimationFrame(f); })()"
		'</script></body>'
	)
	try:
		site.expect_request('/spin').respond_with_data(page, content_type='text/html')
		await _call(server, 'retinat_open', {'url': site.url_for('/spin')})
		await _call(server, 'retinat_reflex', {'rules': [{'when': 'false', 'notify': 'never'}]})
		started = time.monotonic()
		waited = await _call(server, 'retinat_wait', {'timeout': 10})
		took = time.monotonic() - started
		assert not waited.is_error and 'Nothing notified' in _text(waited), _text(waited)
		assert took < 2, f'the wait took {took:.1f}s with a 1 s budget'
		started = time.monotonic()
		stopped = await _call(server, 'retinat_watch', {'seconds': 10})
		took = time.monotonic() - started
		assert stopped.is_error and 'past its 1s budget' in _text(stopped), _text(stopped)
		assert took < 3, f'the watch took {took:.1f}s'
		after = await _call(server, 'retinat_look', {})
		assert not after.is_error, _text(after)
	finally:
		await server._close_all_sessions()
'''
open(p, 'w').write(s)
print('replaced the backstop test', file=sys.stderr)
