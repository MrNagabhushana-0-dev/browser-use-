// The person's answer to an AI's request: to use a new site, or to make one click that would place an order, pay,
// delete an account or grant access. This page is never shared with the AI, and only the person's own clicks count.
'use strict';
(() => {
	const asked = JSON.parse(decodeURIComponent(location.hash.slice(1) || '{}'));
	const $ = (id) => document.getElementById(id);
	if (asked.kind === 'click') {
		document.title = 'An AI asks to make a click';
		$('heading').textContent = 'An AI you run asks to click ';
		$('site').textContent = `"${asked.label || ''}"`;
		$('detail').textContent =
			`on ${asked.origin}. The page labels it so: it may place an order, pay, delete an account or grant access, ` +
			'signed in as you are. Allow lets this one click through, once, in the next minute.';
		$('always').hidden = true;
	} else {
		$('site').textContent = asked.origin || 'a site';
		$('url').textContent = asked.url || '';
		if (asked.once) {
			// a site the person asked to be asked about every time: this visit only, no Always
			$('lasts').textContent = 'You asked to be asked about this site every time: Allow lets it in for this visit only.';
			$('always').hidden = true;
		}
	}
	for (const answer of ['no', 'always', 'allow']) {
		$(answer).addEventListener('click', (e) => {
			if (!e.isTrusted) return;
			chrome.runtime.sendMessage({ answer, origin: asked.origin });
		});
	}
})();
