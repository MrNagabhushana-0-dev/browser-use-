"""A launch that never comes up must not leave a browser behind.

`launch_for_human` owns a persistent profile directory, and Chrome takes an exclusive lock
on it. If the debugging port never opens and the process is left running, the caller gets
an exception and no handle, the lock is held forever, and every subsequent launch against
the same profile fails for a reason that has nothing to do with the second launch. The
stand-in browser here is a real executable that simply sleeps: no port, no CDP, no mocks.
"""

import asyncio
import os
import stat

import psutil
import pytest

from browser_use.cobrowse import HumanBrowser, launch_for_human

# A "browser" that starts, holds the profile, and never opens a port. `exec` matters: the
# shell replaces itself with sleep, so the pid we hand back is the pid that must die.
FAKE_BROWSER = '#!/bin/sh\nexec sleep 30\n'
# The same, but it ignores SIGTERM (disposition survives exec), like a wedged Chrome. Only a
# SIGKILL followed by a wait() gets rid of this one, so it is what exposes a missing reap.
STUBBORN_BROWSER = "#!/bin/sh\ntrap '' TERM\nexec sleep 30\n"


def _write_executable(path, body: str):
	path.write_text(body)
	path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
	return path


@pytest.fixture
def fake_browser(tmp_path):
	return _write_executable(tmp_path / 'fake-chrome.sh', FAKE_BROWSER)


@pytest.fixture
def stubborn_browser(tmp_path):
	return _write_executable(tmp_path / 'stubborn-chrome.sh', STUBBORN_BROWSER)


def _sleeping_children() -> set[int]:
	"""Pids of our own descendants that are the fake browser."""
	found = set()
	for child in psutil.Process(os.getpid()).children(recursive=True):
		try:
			if child.status() == psutil.STATUS_ZOMBIE:
				continue
			if 'sleep' in ' '.join(child.cmdline()):
				found.add(child.pid)
		except psutil.Error:
			continue
	return found


async def _settle(before: set[int], attempts: int = 20) -> set[int]:
	"""New fake-browser pids, given a moment for the reap to land."""
	leaked = _sleeping_children() - before
	for _ in range(attempts):
		if not leaked:
			return leaked
		await asyncio.sleep(0.1)
		leaked = _sleeping_children() - before
	return leaked


def _cleanup(pids: set[int]) -> None:
	for pid in pids:
		try:
			psutil.Process(pid).kill()
		except psutil.Error:
			pass


async def test_a_launch_that_times_out_does_not_leave_the_browser_running(fake_browser, tmp_path):
	before = _sleeping_children()
	try:
		with pytest.raises(TimeoutError):
			await launch_for_human(
				user_data_dir=tmp_path / 'profile',
				executable_path=str(fake_browser),
				launch_timeout=1.5,
			)
		leaked = await _settle(before)
		assert not leaked, f'launch_for_human orphaned the browser (pids {sorted(leaked)}), so the profile stays locked'
	finally:
		_cleanup(_sleeping_children() - before)


async def test_close_reaps_a_browser_that_ignores_sigterm(stubborn_browser, tmp_path):
	"""No CDP to ask nicely and SIGTERM is ignored: close() must escalate to SIGKILL *and* reap."""
	before = _sleeping_children()
	process = await asyncio.create_subprocess_exec(
		str(stubborn_browser), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
	)
	browser = HumanBrowser(
		cdp_url='ws://127.0.0.1:1/devtools/browser/nope',
		port=1,
		user_data_dir=tmp_path / 'profile',
		process=process,
	)
	try:
		await browser.close()
		assert process.returncode is not None, 'close() returned without reaping the process, leaving a zombie'
		assert not await _settle(before), 'close() left the browser running'
	finally:
		_cleanup(_sleeping_children() - before)
