"""What the AI may do through the person's own browser: what the person could do from that tab, nothing more.

Looking, reading, scrolling, clicking, typing, navigating, screenshots and running script in the page all pass,
and input arrives as ordinary trusted events. Refused are the things a person sitting at the browser cannot do
from a tab: changing what the browser says it is or where it is, rewriting traffic, fetching with their cookies
from outside any page, writing cookies behind the site's back, switching off protections, and reaching past the
shared tabs into the browser itself. (Reading other sites' cookies and data is cut off in the relay, by site.)

While the person holds the wheel, or after they press Cancel, only looking passes: the methods listed in
extension/policy.json, which the extension's worker reads too. Everything else counts as acting, including page
script (script can click, type and navigate, and nobody can tell a read from a write in it) and any method the list
does not know, so a new CDP method cannot slip through by being unlisted.
"""

import json
import re
from pathlib import Path

DISGUISE = 'it would change what the browser says it is or where it is, which the person cannot do from a tab'
TRAFFIC = 'it would rewrite or inject network traffic instead of letting the site see what the browser really sends'
STORAGE = "it would write cookies or site data directly instead of through the site's own pages"
PROTECTION = 'it would switch off a browser protection'
CREDENTIALED = "it fetches any address with the person's cookies, past the cross-site checks every page is held to"
BROWSER = 'it reaches past the shared tabs into the browser itself'
HOLDING = (
	'the person is using the browser right now; wait a few seconds and try again, or ask them to hand back the wheel'
	' (Alt+Shift+Z)'
)

STOPPED = 'the person pressed Cancel on the debugging bar, which stops the AI; ask them to share a tab again'
FILES = (
	"it would hand a page files from the person's disk, or write downloads where they didn't choose; they pick files themselves"
)

# The extension's worker refuses sites the person hasn't allowed with these words (worker.js, notAllowed). Such a
# refusal comes before anything is sent.
NOT_ALLOWED = 'is not a site the person has allowed'

REFUSED: dict[str, str] = {
	**dict.fromkeys(
		(
			'Network.setUserAgentOverride',
			'Emulation.setUserAgentOverride',
			'Emulation.setGeolocationOverride',
			'Emulation.setTimezoneOverride',
			'Emulation.setLocaleOverride',
			'Emulation.setDeviceMetricsOverride',
			'Emulation.setTouchEmulationEnabled',
			'Emulation.setEmitTouchEventsForMouse',
			'Emulation.setHardwareConcurrencyOverride',
			'Emulation.setSensorOverrideEnabled',
			'Emulation.setIdleOverride',
		),
		DISGUISE,
	),
	**dict.fromkeys(('Network.setRequestInterception', 'Network.setExtraHTTPHeaders'), TRAFFIC),
	**dict.fromkeys(
		(
			'Network.setCookie',
			'Network.setCookies',
			'Network.deleteCookies',
			'Network.clearBrowserCookies',
			'Storage.setCookies',
			'Storage.clearCookies',
			'Storage.clearDataForOrigin',
			'Storage.clearDataForStorageKey',
		),
		STORAGE,
	),
	**dict.fromkeys(('Page.setBypassCSP', 'Security.setIgnoreCertificateErrors'), PROTECTION),
	'Network.loadNetworkResource': CREDENTIALED,
	**dict.fromkeys(('DOM.setFileInputFiles', 'Page.setDownloadBehavior', 'Page.handleFileChooser'), FILES),
	**dict.fromkeys(('Target.createBrowserContext', 'Target.disposeBrowserContext', 'Target.exposeDevToolsProtocol'), BROWSER),
}
REFUSED_DOMAINS: dict[str, str] = {'Fetch': TRAFFIC, 'Browser': BROWSER, 'SystemInfo': BROWSER}

POLICY_FILE = Path(__file__).parent / 'extension' / 'policy.json'


def _glob(pattern: str) -> str:
	return '.*'.join(re.escape(part) for part in pattern.split('*'))


PASSIVE_GLOBS: tuple[str, ...] = tuple(json.loads(POLICY_FILE.read_text())['passive'])
PASSIVE = re.compile('|'.join(f'(?:{_glob(g)})' for g in PASSIVE_GLOBS))


def acts(method: str) -> bool:
	"""Whether `method` does more than look (everything not on the passive list does)."""
	return PASSIVE.fullmatch(method) is None


def refusal(method: str, human_driving: bool = False, stopped: bool = False) -> str | None:
	"""Why `method` is refused through the bridge, or None when it is allowed."""
	assert '.' in method, f'not a CDP method: {method!r}'
	why = REFUSED.get(method) or REFUSED_DOMAINS.get(method.split('.', 1)[0])
	if why is None and (human_driving or stopped) and acts(method):
		why = STOPPED if stopped else HOLDING
	return f'{method} is refused through the extension bridge: {why}' if why else None
