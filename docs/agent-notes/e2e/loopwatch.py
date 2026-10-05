"""Diagnostic pytest plugin: log event-loop stalls (loop running but not ticking) with the test and main stack.

Use: LOOPWATCH_OUT=stalls.log PYTHONPATH=docs/agent-notes/e2e uv run pytest -p loopwatch tests/ci
"""

import asyncio
import os
import sys
import threading
import time
import traceback

import pytest

OUT = os.environ.get('LOOPWATCH_OUT', '/tmp/loopwatch.log')
state = {'beat': time.monotonic(), 'test': '-', 'loop': None, 'main': threading.main_thread().ident}


def _beat(loop):
	state['beat'] = time.monotonic()
	loop.call_later(0.2, _beat, loop)


def _watch():
	start = None
	dumped = False
	worst = 0.0
	with open(OUT, 'a', buffering=1) as f:
		f.write(f'--- loopwatch start {time.strftime("%T")}\n')
		while True:
			time.sleep(0.1)
			loop = state['loop']
			if loop is None or not loop.is_running():
				start = None
				dumped = False
				state['beat'] = time.monotonic()
				continue
			lag = time.monotonic() - state['beat']
			if lag > 1.0:
				start = start or state['beat']
				worst = max(worst, lag)
				if lag > 3.0 and not dumped:
					frame = sys._current_frames().get(state['main'])
					f.write(
						f'\n=== STALL >3s in {state["test"]} at {time.strftime("%T")}\n'
						+ ''.join(traceback.format_stack(frame)[-30:])
					)
					dumped = True
			elif start is not None:
				f.write(f'stall {worst:.1f}s in {state["test"]} ending {time.strftime("%T")}\n')
				start = None
				dumped = False
				worst = 0.0


@pytest.fixture(autouse=True)
async def _loopwatch(request):
	state['test'] = request.node.nodeid
	loop = asyncio.get_running_loop()
	if state['loop'] is not loop:
		state['loop'] = loop
		state['beat'] = time.monotonic()
		loop.call_soon(_beat, loop)
	yield


def pytest_configure(config):
	threading.Thread(target=_watch, daemon=True).start()
