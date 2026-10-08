// Runs in every page. Two jobs:
// 1. Tell the bridge when someone uses this page, so the AI pauses while the person is busy in a shared tab
//    (the worker ignores tabs that aren't shared and input that it sent itself).
// 2. In a shared tab, show the person that the AI is working here, with one button to take the wheel or hand it
//    back. Every Chromium browser shows it, including Vivaldi, which has no "started debugging" bar. The pill is
//    marked data-browser-use-exclude and lives in a closed shadow root, so it is not part of the page the AI reads.
'use strict';
(() => {
	let last = 0;
	let host = null; // the sharing pill, below
	const tell = (e) => {
		if (!e.isTrusted) return;
		if (host && e.composedPath().includes(host)) return; // the pill is the person's control, not page use
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

	if (window.top !== window) return; // one pill per tab, in its top document

	let state = { shared: false, holder: 'agent', stopped: false };

	function build() {
		host = document.createElement('retinat-bridge-pill');
		host.setAttribute('data-browser-use-exclude', 'true');
		host.style.cssText =
			'all: initial; display: block; position: fixed; z-index: 2147483647; left: 50%; bottom: 14px;' +
			' transform: translateX(-50%);';
		const root = host.attachShadow({ mode: 'closed' });
		root.innerHTML = `<style>
			.pill { display: flex; align-items: center; gap: 10px; padding: 6px 6px 6px 14px; border-radius: 999px;
				font: 13px/1.2 system-ui, sans-serif; color: #fff; box-shadow: 0 2px 10px #0004; white-space: nowrap; }
			.pill.agent { background: #1a7f37; } .pill.human { background: #b26a00; }
			button { font: inherit; border: 0; border-radius: 999px; padding: 6px 12px; cursor: pointer;
				background: #fff; color: #222; }
		</style><div class="pill"><span class="text"></span><button type="button"></button></div>`;
		const pill = root.querySelector('.pill');
		const text = root.querySelector('.text');
		const button = root.querySelector('button');
		button.addEventListener('click', (e) => {
			e.stopPropagation();
			const holder = state.holder === 'human' ? 'agent' : 'human';
			try {
				chrome.runtime.sendMessage({ ask: 'holder', holder }, (s) => s && render({ ...state, holder: s.holder }));
			} catch (err) {
				// orphaned copy
			}
		});
		host.paint = () => {
			const human = state.holder === 'human';
			pill.className = 'pill ' + (human ? 'human' : 'agent');
			text.textContent = human ? 'You have the wheel. The AI is paused here.' : 'An AI is working in this tab';
			button.textContent = human ? 'Hand back' : 'Take the wheel';
		};
	}

	function render(next) {
		state = next;
		const show = state.shared && !state.stopped;
		if (!show) {
			if (host) host.remove();
			return;
		}
		if (!host) build();
		host.paint();
		if (!host.isConnected) document.documentElement.append(host);
	}

	chrome.runtime.onMessage.addListener((msg) => {
		if (msg && msg.pill) render(msg.pill);
	});
	try {
		chrome.runtime.sendMessage({ ask: 'pill' }, (s) => s && !s.error && render(s));
	} catch (err) {
		// orphaned copy
	}
	// A page may rebuild its document or remove nodes it does not know; put the pill back while the tab is shared.
	setInterval(() => {
		if (host && state.shared && !state.stopped && !host.isConnected) document.documentElement.append(host);
	}, 2000);
})();
