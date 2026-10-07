// Tells the bridge when someone uses this page, so the AI pauses while the person is busy in a shared tab.
// Runs in every page; the worker ignores tabs that aren't shared and input that it sent itself.
'use strict';
(() => {
	let last = 0;
	const tell = (e) => {
		if (!e.isTrusted) return;
		const now = Date.now();
		if (now - last < 250) return;
		last = now;
		try {
			chrome.runtime.sendMessage({ input: e.type });
		} catch (err) {
			// the extension was reloaded; this page's copy is orphaned
		}
	};
	for (const type of ['pointerdown', 'keydown', 'wheel']) addEventListener(type, tell, { capture: true, passive: true });
})();
