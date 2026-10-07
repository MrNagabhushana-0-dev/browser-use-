"""What the AI may do through the person's own browser: what the person could do from that tab, nothing more.

Looking, reading, scrolling, clicking, typing, navigating, screenshots and running script in the page all pass,
and input arrives as ordinary trusted events. Refused are the things a person sitting at the browser cannot do
from a tab: changing what the browser says it is or where it is, rewriting traffic, writing cookies behind the
site's back, switching off protections, and reaching past the shared tabs into the browser itself.
"""

import re

DISGUISE = 'it would change what the browser says it is or where it is, which the person cannot do from a tab'
TRAFFIC = 'it would rewrite or inject network traffic instead of letting the site see what the browser really sends'
STORAGE = "it would write cookies or site data directly instead of through the site's own pages"
PROTECTION = 'it would switch off a browser protection'
BROWSER = 'it reaches past the shared tabs into the browser itself'
HOLDING = (
	'the person is using the browser right now; wait a few seconds and try again, or ask them to hand back the wheel'
	' (Alt+Shift+Z)'
)

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
	**dict.fromkeys(('Target.createBrowserContext', 'Target.disposeBrowserContext', 'Target.exposeDevToolsProtocol'), BROWSER),
}
REFUSED_DOMAINS: dict[str, str] = {'Fetch': TRAFFIC, 'Browser': BROWSER, 'SystemInfo': BROWSER}

# Methods that act on the page; refused while the person holds the wheel. Keep in step with worker.js.
ACTING = re.compile(
	r'^(Input\.|Page\.(navigate|navigateToHistoryEntry|reload|close)$'
	r'|DOM\.(setFileInputFiles|setAttributeValue|setAttributesAsText|setOuterHTML|setNodeValue|removeNode|removeAttribute)$'
	r'|Target\.(createTarget|closeTarget|activateTarget)$)'
)


def refusal(method: str, human_driving: bool = False) -> str | None:
	"""Why `method` is refused through the bridge, or None when it is allowed."""
	assert '.' in method, f'not a CDP method: {method!r}'
	why = REFUSED.get(method) or REFUSED_DOMAINS.get(method.split('.', 1)[0])
	if why is None and human_driving and ACTING.match(method):
		why = HOLDING
	return f'{method} is refused through the extension bridge: {why}' if why else None
