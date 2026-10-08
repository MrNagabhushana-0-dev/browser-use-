// The person's answer to an AI's request to use a new site. This page is never shared with the AI, and only the
// person's own clicks count.
'use strict';
(() => {
	const asked = JSON.parse(decodeURIComponent(location.hash.slice(1) || '{}'));
	document.getElementById('site').textContent = asked.origin || 'a site';
	document.getElementById('url').textContent = asked.url || '';
	for (const answer of ['no', 'always', 'allow']) {
		document.getElementById(answer).addEventListener('click', (e) => {
			if (!e.isTrusted) return;
			chrome.runtime.sendMessage({ answer, origin: asked.origin });
		});
	}
})();
