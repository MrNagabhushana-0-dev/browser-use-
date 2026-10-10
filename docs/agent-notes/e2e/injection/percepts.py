import asyncio
import json
import re
import secrets
import sys
from pathlib import Path

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.eyes import Eyes

HERE = Path(sys.argv[1])


async def main():
	s = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True, user_data_dir=None, keep_alive=True, args=['--autoplay-policy=no-user-gesture-required']
		)
	)
	await s.start()
	runs = []
	for i, cond in enumerate(['marked', 'unmarked'] * 2):
		tok = secrets.token_hex(5)
		eyes = Eyes(s, speech=False, now_path=False)
		await eyes.open()
		await s.navigate_to(f'http://127.0.0.1:8765/clipb-{tok}')
		p = await eyes.watch(seconds=3.0, until='time')
		text = p.text
		if cond == 'unmarked':
			text = re.sub(r'  ⚠ [^\n]*', '', text)
		runs.append({'run': i + 3, 'cond': cond, 'tok': tok, 'percept': text})
		await eyes.close()
		await s.navigate_to('about:blank')
	(HERE / 'runs_b.json').write_text(json.dumps(runs, indent=1))
	await s.kill()


asyncio.run(main())
