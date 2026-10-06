"""eyesbench in CI: seeded dynamic-content tasks, scored from ground truth with no LLM judge.

The retina must get both tasks exactly right. The screenshot loop is measured, not asserted to fail: whether a
1.5 s loop catches a 0.4 s flash is chance, and the benchmark's job is to report that chance honestly.
"""

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.eyes import Eyes, asr, bench


@pytest.fixture(scope='module')
def server():
	s = HTTPServer()
	s.start()
	yield s
	s.stop()


@pytest.fixture(scope='module')
async def session():
	s = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True, user_data_dir=None, keep_alive=True, args=['--autoplay-policy=no-user-gesture-required']
		)
	)
	await s.start()
	yield s
	await s.kill()


async def test_the_retina_gets_every_seeded_task_right_and_costs_far_less(server, session, tmp_path):
	def serve(page_path: str, html: str, media_path: str, media: bytes) -> None:
		server.expect_request(page_path).respond_with_data(html, content_type='text/html')
		if media:
			server.expect_request(media_path).respond_with_handler(lambda r: bench.media_response(r, media))

	eyes = Eyes(session, speech=False, now_path=False)
	try:
		rows = await bench.run(session, eyes, server.url_for('').rstrip('/'), serve, seeds=(1,), work=tmp_path)
	finally:
		await eyes.close()
	print(bench.table(rows))
	retina = [r for r in rows if r['mode'] == 'retina']
	shots = [r for r in rows if r['mode'] == 'screenshots']
	snaps = [r for r in rows if r['mode'] == 'snapshots']
	assert {r['task'] for r in retina} == {'flash', 'beeps', 'toast'}
	assert all(r['correct'] for r in retina), bench.table(rows)
	assert all(not r['captured'] for r in shots + snaps if r['task'] == 'beeps'), 'neither carries sound'
	assert all(not r['captured'] for r in snaps if r['task'] == 'flash'), "a video's pixels are not in the tree"
	assert sum(r['tokens'] for r in retina) * 10 < sum(r['tokens'] for r in shots), bench.table(rows)


async def test_the_flash_detector_sees_a_flash_frame_when_the_video_is_paused_on_it(server, session, tmp_path):
	# Guards the scorer itself: without this, "screenshots missed it" could be a detector bug (it once was).
	import asyncio

	task = bench.flash_task(1, tmp_path)
	server.expect_request('/paused-flash').respond_with_data(task.page('/paused-flash.webm'), content_type='text/html')
	server.expect_request('/paused-flash.webm').respond_with_handler(lambda r: bench.media_response(r, task.media))
	await session.navigate_to(server.url_for('/paused-flash'))
	cdp = await session.get_or_create_cdp_session(focus=False)
	await asyncio.sleep(1.0)
	at = task.truth['at'] + 0.2
	await cdp.cdp_client.send.Runtime.evaluate(
		params={
			'expression': f"(async()=>{{const v=document.querySelector('video');v.pause();v.currentTime={at};"
			"await new Promise(r=>v.addEventListener('seeked',r,{once:true}));await new Promise(r=>setTimeout(r,300))})()",
			'awaitPromise': True,
		},
		session_id=cdp.session_id,
	)
	shots, tokens = await bench.screenshot_loop(session, 0.1)
	assert shots and bench._shows_colour(shots[0], task.truth['colour'], centre_only=True)
	assert tokens == 1196, 'one 1280x720 screenshot, costed as the screenshot agents downscale'


async def test_the_retina_counts_bounces_drawn_on_a_canvas(server, session, tmp_path):
	# No <video> and nothing in the DOM: the information is only in a canvas animation, which screenshots
	# sample too sparsely to count and accessibility trees do not contain.
	def serve(page_path: str, html: str, media_path: str, media: bytes) -> None:
		server.expect_request(page_path).respond_with_data(html, content_type='text/html')

	eyes = Eyes(session, speech=False, now_path=False)
	try:
		rows = await bench.run(
			session,
			eyes,
			server.url_for('').rstrip('/'),
			serve,
			seeds=(1, 2),
			work=tmp_path,
			tasks=(bench.bounce_task,),
			modes=('retina',),
		)
	finally:
		await eyes.close()
	print(bench.table(rows))
	assert all(r['correct'] and r['sent'] for r in rows), bench.table(rows)


async def test_a_live_value_that_peaks_for_one_tick_is_read_by_the_retina_and_its_static_twin_by_every_mode(
	server, session, tmp_path
):
	# A dashboard value updates four times a second and crosses its alert line once, for 250 ms. The static twin
	# holds that peak on screen: every mode must read it there, so a miss on the live page is the sampling, not
	# a scorer that cannot see.
	def serve(page_path: str, html: str, media_path: str, media: bytes) -> None:
		server.expect_request(page_path).respond_with_data(html, content_type='text/html')

	eyes = Eyes(session, speech=False, now_path=False)
	try:
		rows = await bench.run(
			session,
			eyes,
			server.url_for('').rstrip('/'),
			serve,
			seeds=(1, 2),
			work=tmp_path,
			tasks=(bench.ticker_task, bench.ticker_static_task),
		)
	finally:
		await eyes.close()
	print(bench.table(rows))
	assert all(r['correct'] for r in rows if r['mode'] == 'retina'), bench.table(rows)
	assert all(r['correct'] for r in rows if r['task'] == 'ticker-static'), bench.table(rows)


async def test_the_flash_and_toast_twins_are_read_by_every_mode_that_can_see_them(server, session, tmp_path):
	# Static twins of the flash (the colour held for the whole video) and the toast (shown from load, never
	# removed). Snapshots are exempt from the flash twin by nature: a video's pixels are not in the tree.
	def serve(page_path: str, html: str, media_path: str, media: bytes) -> None:
		server.expect_request(page_path).respond_with_data(html, content_type='text/html')
		if media:
			server.expect_request(media_path).respond_with_handler(lambda r: bench.media_response(r, media))

	eyes = Eyes(session, speech=False, now_path=False)
	try:
		rows = await bench.run(
			session,
			eyes,
			server.url_for('').rstrip('/'),
			serve,
			seeds=(1,),
			work=tmp_path,
			tasks=(bench.flash_static_task, bench.toast_static_task),
		)
	finally:
		await eyes.close()
	print(bench.table(rows))
	blind = {('flash-static', 'snapshots')}
	assert all(r['correct'] for r in rows if (r['task'], r['mode']) not in blind), bench.table(rows)
	assert not any(r['captured'] for r in rows if (r['task'], r['mode']) in blind), bench.table(rows)


async def test_a_carousel_slide_shown_by_a_class_change_is_read_by_the_retina_and_its_twin_by_every_mode(
	server, session, tmp_path
):
	# Slides are in the page from the start, hidden, and shown in turn by a class change: no text is inserted.
	# The question is about the third slide, up for 0.5 s at a seeded moment.
	def serve(page_path: str, html: str, media_path: str, media: bytes) -> None:
		server.expect_request(page_path).respond_with_data(html, content_type='text/html')

	eyes = Eyes(session, speech=False, now_path=False)
	try:
		rows = await bench.run(
			session,
			eyes,
			server.url_for('').rstrip('/'),
			serve,
			seeds=(1, 2),
			work=tmp_path,
			tasks=(bench.carousel_task, bench.carousel_static_task),
		)
	finally:
		await eyes.close()
	print(bench.table(rows))
	assert all(r['correct'] for r in rows if r['mode'] == 'retina'), bench.table(rows)
	assert all(r['correct'] for r in rows if r['task'] == 'carousel-static'), bench.table(rows)


@pytest.mark.skipif(not asr.available(), reason='speech extra (faster-whisper) not installed')
async def test_a_spoken_code_is_heard_and_transcribed_by_the_retina_and_by_no_other_mode(server, session, tmp_path):
	# A video reads out a seeded four-digit code (recorded digits, see assets/fsdd). The answer is only in the sound.
	def serve(page_path: str, html: str, media_path: str, media: bytes) -> None:
		server.expect_request(page_path).respond_with_data(html, content_type='text/html')
		server.expect_request(media_path).respond_with_handler(lambda r: bench.media_response(r, media))

	eyes = Eyes(session, speech=True, now_path=False)  # words need the speech model, as an agent with the extra has
	try:
		rows = await bench.run(
			session, eyes, server.url_for('').rstrip('/'), serve, seeds=(1, 2), work=tmp_path, tasks=(bench.spoken_task,)
		)
	finally:
		await eyes.close()
	print(bench.table(rows))
	assert all(r['correct'] for r in rows if r['mode'] == 'retina'), bench.table(rows)
	assert not any(r['captured'] for r in rows if r['mode'] != 'retina'), bench.table(rows)


async def test_a_flash_drawn_with_webgl_is_seen_by_the_retina(server, session, tmp_path):
	# Most WebGL pages keep the default preserveDrawingBuffer: false, so the drawing buffer is cleared once it has
	# been shown. A flash drawn there must still reach the retina.
	def serve(page_path: str, html: str, media_path: str, media: bytes) -> None:
		server.expect_request(page_path).respond_with_data(html, content_type='text/html')

	eyes = Eyes(session, speech=False, now_path=False)
	try:
		rows = await bench.run(
			session, eyes, server.url_for('').rstrip('/'), serve, seeds=(1, 2), work=tmp_path, tasks=(bench.glflash_task,)
		)
	finally:
		await eyes.close()
	print(bench.table(rows))
	assert all(r['correct'] for r in rows if r['mode'] == 'retina'), bench.table(rows)
