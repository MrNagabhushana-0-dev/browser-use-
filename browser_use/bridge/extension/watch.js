// Runs in every page. Two jobs:
// 1. Tell the bridge when someone uses this page, so the AI pauses while the person is busy in a shared tab
//    (the worker ignores tabs that aren't shared and input that it sent itself).
// 2. In a shared tab, show the person that the AI is working here, with one button to take the wheel or hand it
//    back. Every Chromium browser shows it, including Vivaldi, which has no "started debugging" bar. The pill is
//    marked data-browser-use-exclude and lives in a closed shadow root, so it is not part of the page the AI reads.
//    In a tab opened from a shared tab by the person or the page, it asks instead: share this one too? The AI has no
//    input in an unshared tab, and the page can't reach into the closed root, so only the person can answer.
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

	// 3. While the AI acts in a shared tab (the bridge arms this copy just before each action), hold an activation of a
	//    control that places an order, pays, deletes an account or grants access: in the event itself, before any of
	//    the page's listeners (these are registered first, at document start) and before its default action. The
	//    bridge then refuses the AI's action and asks the person. Every frame runs this, payment forms' included.
	let armed = { id: 0, until: 0, words: [], grants: [] };
	const reported = new Set();
	const PAYMENT_FRAME = /(^|\.)(stripe\.com|paypal\.com|braintreegateway\.com|adyen\.com|checkout\.com|klarna\.com|mollie\.com|squareup\.com)$/;
	const ACTIVATABLE =
		'button, input[type=submit i], input[type=button i], input[type=image i], a[href], summary, [role=button], [role=link], [role=menuitem], [onclick]';
	const norm = (s) =>
		String(s || '')
			.normalize('NFKC')
			.replace(/[\u200b-\u200d\u2060\ufeff]/g, '')
			.replace(/\s+/g, ' ')
			.trim()
			.toLowerCase();
	const labelOf = (el) =>
		norm([el.getAttribute('aria-label'), el.innerText || el.textContent, el.value, el.title, el.getAttribute('alt')].filter(Boolean).join(' ')).slice(0, 160);
	const phrase = (label) => armed.words.find((w) => new RegExp('(^|[^a-z0-9])' + w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '($|[^a-z0-9])').test(label));
	const activatable = (e) => {
		for (const n of e.composedPath()) {
			if (n === host) return null; // the sharing pill is the person's
			if (n.nodeType === 1 && n.matches(ACTIVATABLE)) return n;
		}
		return null;
	};
	const ACTIVATING_KEYS = new Set(['Enter', ' ', 'Spacebar', 'NumpadEnter']);
	const gate = (e) => {
		if (Date.now() > armed.until) return;
		let el = null;
		if (e.type === 'submit') {
			const form = e.target;
			el = e.submitter || (form.querySelector && form.querySelector('button:not([type]), [type=submit i], [type=image i]')) || form;
		} else if (e.type.startsWith('key')) {
			if (!ACTIVATING_KEYS.has(e.key) && e.code !== 'Enter' && e.code !== 'NumpadEnter') return;
			el = activatable(e); // Enter in a text field submits its form: the submit event is gated instead
		} else {
			el = activatable(e);
		}
		if (!el) return;
		const label = labelOf(el);
		const paying = window.top !== window && PAYMENT_FRAME.test(location.hostname);
		const hit = phrase(label) || (paying ? 'a payment form from ' + location.hostname : null);
		if (!hit) return;
		const key = location.origin + '|' + label;
		if (armed.grants.includes(key)) {
			if (e.type === 'click' || e.type === 'submit') {
				try {
					chrome.runtime.sendMessage({ consumed: key });
				} catch (err) {
					// orphaned copy
				}
			}
			return; // the person allowed this one
		}
		e.preventDefault();
		e.stopImmediatePropagation();
		if (reported.has(armed.id)) return;
		reported.add(armed.id);
		try {
			chrome.runtime.sendMessage({ held: { id: armed.id, label, origin: location.origin, hit } });
		} catch (err) {
			// orphaned copy
		}
	};
	const GATED = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'dblclick', 'auxclick', 'touchstart', 'touchend'];
	for (const type of [...GATED, 'keydown', 'keypress', 'keyup', 'submit']) addEventListener(type, gate, { capture: true });
	chrome.runtime.onMessage.addListener((msg, sender, reply) => {
		if (!msg || !msg.arm) return;
		armed = { id: msg.arm.id, until: Date.now() + msg.arm.ms, words: msg.arm.words || [], grants: msg.arm.grants || [] };
		reply({ armed: true });
	});

	if (window.top !== window) return; // one pill per tab, in its top document

	let state = { shared: false, offered: false, holder: 'agent', stopped: false };

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
			.pill.agent { background: #1a7f37; } .pill.human { background: #b26a00; } .pill.offer { background: #3949ab; }
			button.quiet { background: transparent; color: #fff; text-decoration: underline; padding: 6px 4px; }
			button { font: inherit; border: 0; border-radius: 999px; padding: 6px 12px; cursor: pointer;
				background: #fff; color: #222; }
		</style><div class="pill"><span class="text"></span><button type="button" class="quiet"></button>` +
			`<button type="button" class="main"></button></div>`;
		const pill = root.querySelector('.pill');
		const text = root.querySelector('.text');
		const button = root.querySelector('button.main');
		const quiet = root.querySelector('button.quiet');
		const ask = (msg, then) => {
			try {
				chrome.runtime.sendMessage(msg, (s) => s && !s.error && then && then(s));
			} catch (err) {
				// orphaned copy
			}
		};
		button.addEventListener('click', (e) => {
			e.stopPropagation();
			if (!e.isTrusted) return; // only the person's own click
			if (state.offered) return ask({ ask: 'share-here' });
			const holder = state.holder === 'human' ? 'agent' : 'human';
			ask({ ask: 'holder', holder }, (s) => render({ ...state, holder: s.holder }));
		});
		quiet.addEventListener('click', (e) => {
			e.stopPropagation();
			if (e.isTrusted) ask({ ask: 'decline-here' });
		});
		host.paint = () => {
			const human = state.holder === 'human';
			if (state.offered) {
				pill.className = 'pill offer';
				const site = state.moved && state.moved.site;
				text.textContent = !site
					? 'Opened from a tab you share with an AI. Share this one too?'
					: state.moved.byAi
						? `The AI came to ${site}, a site you haven't let it use. Let it work here?`
						: `This tab is now on ${site}, a site you haven't let the AI use. Share it here too?`;
				quiet.hidden = false;
				quiet.textContent = 'Not now';
				button.textContent = state.moved ? 'Allow here' : 'Share this tab';
				return;
			}
			quiet.hidden = true;
			pill.className = 'pill ' + (human ? 'human' : 'agent');
			text.textContent = human ? 'You have the wheel. The AI is paused here.' : 'An AI is working in this tab';
			button.textContent = human ? 'Hand back' : 'Take the wheel';
		};
	}

	function render(next) {
		state = next;
		const show = (state.shared && !state.stopped) || state.offered;
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
		if (host && ((state.shared && !state.stopped) || state.offered) && !host.isConnected) {
			document.documentElement.append(host);
		}
	}, 2000);
})();
