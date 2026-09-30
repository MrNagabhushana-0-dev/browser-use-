"""Recognise when a site is refusing us, so the answer is "blocked" and not an empty page.

An agent that does not know it hit a bot wall reports a site as broken, or worse, reports what
it "saw" on a challenge page as the site's content. These are the walls met in practice. They
are recognised, named and reported; nothing here tries to get past one. A wall from a network
(Google's "unusual traffic" page shown to a whole datacenter IP range) will not move for any
browser setting; the way through is the person's own browser and connection
(`browser_use.cobrowse`).
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Wall:
	kind: str
	evidence: str
	advice: str


_ADVICE_NETWORK = (
	'the refusal is aimed at this network, not the browser: run from your own machine and '
	'connection, ideally attached to your own signed-in Chrome (browser_use.cobrowse)'
)
_ADVICE_CHALLENGE = 'a human verification step: open it in your own browser and complete it yourself, then hand the tab over'

_RULES: list[tuple[str, re.Pattern[str], str, str]] = [
	('google-unusual-traffic', re.compile(r'google\.[a-z.]+/sorry/'), 'url', _ADVICE_NETWORK),
	('google-unusual-traffic', re.compile(r'unusual traffic from your computer network', re.I), 'text', _ADVICE_NETWORK),
	('youtube-bot-check', re.compile(r'sign in to confirm (that )?you.?re not a bot', re.I), 'text', _ADVICE_NETWORK),
	('cloudflare-challenge', re.compile(r'^(just a moment|attention required)', re.I), 'title', _ADVICE_CHALLENGE),
	('cloudflare-challenge', re.compile(r'verify you are human by completing the action below', re.I), 'text', _ADVICE_CHALLENGE),
	(
		'access-denied',
		re.compile(r'^access denied$|you don.t have permission to access .* on this server', re.I),
		'title+text',
		_ADVICE_NETWORK,
	),
	(
		'captcha',
		re.compile(r'(hcaptcha\.com|recaptcha/(api|enterprise)|challenges\.cloudflare\.com)'),
		'frames',
		_ADVICE_CHALLENGE,
	),
	(
		'rate-limited',
		re.compile(r'^(429|too many requests)', re.I),
		'title',
		'the site is rate-limiting: slow down and retry later',
	),
]


def detect(url: str, title: str = '', text: str = '', frames: list[str] | None = None) -> Wall | None:
	"""The wall this page is, if it is one. `frames` are iframe/script URLs on the page."""
	fields = {
		'url': url or '',
		'title': (title or '').strip(),
		'text': (text or '')[:4000],
		'title+text': f'{title}\n{(text or "")[:4000]}',
		'frames': '\n'.join(frames or []),
	}
	for kind, pattern, field, advice in _RULES:
		# A contact form with reCAPTCHA on it is a page, not a wall: only a page that is little
		# more than the challenge counts.
		if kind == 'captcha' and len((text or '').strip()) > 400:
			continue
		match = pattern.search(fields[field])
		if match:
			return Wall(kind, match.group(0)[:120], advice)
	return None


# Collected in the page: what `detect` needs, cheaply.
PROBE_JS = """JSON.stringify({url: location.href, title: document.title,
	text: document.body ? document.body.innerText.slice(0, 4000) : '',
	frames: [...document.querySelectorAll('iframe[src], script[src]')].map(e => e.src).slice(0, 80)})"""
