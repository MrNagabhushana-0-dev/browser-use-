import asyncio

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession


class MessageHandlerClient:
	def __init__(self, task: asyncio.Task) -> None:
		self._message_handler_task = task


async def test_ws_drop_during_reconnect_triggers_follow_up_attempt(monkeypatch) -> None:
	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, cdp_url='ws://127.0.0.1:9222'))

	reconnect_started = asyncio.Event()
	allow_reconnect = asyncio.Event()
	reconnect_attempts = 0

	async def reconnect(self: BrowserSession) -> None:
		nonlocal reconnect_attempts
		reconnect_attempts += 1
		if reconnect_attempts == 1:
			reconnect_started.set()
			await allow_reconnect.wait()

	monkeypatch.setattr(BrowserSession, 'reconnect', reconnect)

	connection_closed = asyncio.get_running_loop().create_future()
	task = asyncio.ensure_future(connection_closed)
	session._cdp_client_root = MessageHandlerClient(task)  # type: ignore[assignment]
	initial_reconnect = asyncio.create_task(session._auto_reconnect(max_attempts=1))
	await reconnect_started.wait()
	session._attach_ws_drop_callback()
	connection_closed.set_exception(ConnectionResetError('ws dropped again'))
	await asyncio.sleep(0)
	connection_closed.exception()
	allow_reconnect.set()
	await initial_reconnect

	assert session._reconnect_task is not None
	await session._reconnect_task
	assert reconnect_attempts == 2
	await session.event_bus.stop(clear=True, timeout=5)


async def test_a_profile_held_by_a_live_chrome_launches_on_a_temporary_profile_instead_of_dying(tmp_path) -> None:
	# Two MCP servers on one machine default to the same profile. Chrome's singleton lock makes the second
	# launch hand its URL to the first and exit, which surfaced as "exited before CDP became available".
	first = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=tmp_path / 'shared', keep_alive=True))
	second = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=tmp_path / 'shared', keep_alive=True))
	try:
		await first.start()
		await second.start()
		assert second.cdp_url and second.cdp_url != first.cdp_url
		await second.navigate_to('about:blank')
		await first.navigate_to('about:blank')
	finally:
		await second.kill()
		await first.kill()


# One process: wait for a shared start instant, start a browser on the given profile, say how it went.
_RACER = """
import asyncio, sys, time
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

async def main(profile, go_at):
	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=profile, keep_alive=False))
	while time.time() < go_at:
		await asyncio.sleep(0.002)
	try:
		await session.start()
		print('STARTED', flush=True)
		await asyncio.sleep(2)
	except Exception as e:
		print('FAILED', type(e).__name__, e, flush=True)
	finally:
		await session.kill()

asyncio.run(main(sys.argv[1], float(sys.argv[2])))
"""


async def test_two_processes_starting_on_one_profile_at_the_same_moment_both_get_a_browser(tmp_path) -> None:
	"""Two servers (say two MCP clients) launch on the same profile at once. Each sees the profile free, both start
	Chrome on it, and the later Chrome hands off to the earlier one and exits. The loser has to notice who holds the
	profile now and launch on a temporary one, as it would have had it come second."""
	import sys
	import time

	profile = tmp_path / 'shared-profile'
	go_at = time.time() + 4.0  # both interpreters are up and importing well before this
	racers = [
		await asyncio.create_subprocess_exec(
			sys.executable,
			'-c',
			_RACER,
			str(profile),
			str(go_at),
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.DEVNULL,
		)
		for _ in range(2)
	]
	outputs = [(await asyncio.wait_for(r.communicate(), 90))[0].decode() for r in racers]
	assert all('STARTED' in out for out in outputs), outputs
