// Read-only checks run inside a page after it has been scrolled through. Returns JSON.
// Everything here is what a careful person would notice, measured instead of eyeballed.
(async () => {
	const out = {};
	const short = (s, n = 80) => String(s || '').replace(/\s+/g, ' ').trim().slice(0, n);
	const visible = (el) => {
		const r = el.getBoundingClientRect();
		const s = getComputedStyle(el);
		return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
	};
	const name = (el) =>
		short(
			el.getAttribute('aria-label') ||
				(el.getAttribute('aria-labelledby') &&
					el.getAttribute('aria-labelledby').split(/\s+/).map((id) => document.getElementById(id)?.innerText || '').join(' ')) ||
				el.innerText ||
				el.getAttribute('title') ||
				el.querySelector('img[alt]')?.getAttribute('alt') ||
				el.querySelector('svg title')?.textContent ||
				''
		);
	const where = (el) => {
		const id = el.id ? '#' + el.id : '';
		const cls = typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
		return (el.tagName.toLowerCase() + id + cls).slice(0, 80);
	};

	out.title = document.title;
	out.lang = document.documentElement.lang || '';
	out.description = document.querySelector('meta[name="description"]')?.content || '';
	out.canonical = document.querySelector('link[rel="canonical"]')?.href || '';
	out.icon = !!document.querySelector('link[rel~="icon"]');
	out.h1 = [...document.querySelectorAll('h1')].filter(visible).map((h) => short(h.innerText, 60));
	out.headingSkips = (() => {
		let prev = 0;
		const skips = [];
		for (const h of document.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
			if (!visible(h)) continue;
			const lvl = +h.tagName[1];
			if (prev && lvl > prev + 1) skips.push(`h${prev} -> h${lvl} "${short(h.innerText, 40)}"`);
			prev = lvl;
		}
		return skips.slice(0, 5);
	})();

	const imgs = [...document.images];
	out.images = imgs.length;
	out.brokenImages = imgs.filter((i) => i.complete && i.naturalWidth === 0 && (i.currentSrc || i.src)).map((i) => (i.currentSrc || i.src).slice(0, 160));
	out.imagesWithoutAlt = imgs.filter((i) => !i.hasAttribute('alt') && visible(i)).map((i) => (i.currentSrc || i.src).slice(-80));
	out.oversizedImages = imgs
		.filter((i) => i.naturalWidth > 0 && visible(i))
		.map((i) => [i, i.getBoundingClientRect().width * devicePixelRatio])
		.filter(([i, w]) => w > 0 && i.naturalWidth > 2.5 * w && i.naturalWidth > 1200)
		.map(([i, w]) => `${(i.currentSrc || i.src).slice(-60)} (${i.naturalWidth}px shown at ${Math.round(w)}px)`)
		.slice(0, 5);

	const controls = [...document.querySelectorAll('button, [role="button"], a[href], input:not([type="hidden"]), select, textarea')].filter(visible);
	out.unnamedControls = controls
		.filter((el) => {
			if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) {
				return !(el.labels && el.labels.length) && !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby') && !el.placeholder && !el.title;
			}
			return !name(el);
		})
		.map(where)
		.slice(0, 10);
	out.controls = controls.length;

	// Anything wider than the viewport makes the page scroll sideways.
	out.horizontalOverflow = Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth);
	out.overflowing = out.horizontalOverflow
		? [...document.body.querySelectorAll('*')]
				.filter((el) => {
					const r = el.getBoundingClientRect();
					return r.right > document.documentElement.clientWidth + 2 && r.width > 0 && visible(el) && getComputedStyle(el).position !== 'fixed';
				})
				.slice(0, 5)
				.map((el) => `${where(el)} (right edge ${Math.round(el.getBoundingClientRect().right)}px)`)
		: [];

	const tinyTargets = controls.filter((el) => {
		const r = el.getBoundingClientRect();
		return r.width > 0 && r.height > 0 && (r.width < 24 || r.height < 24) && el.tagName !== 'A';
	});
	out.smallTapTargets = tinyTargets.slice(0, 8).map((el) => `${where(el)} ${Math.round(el.getBoundingClientRect().width)}x${Math.round(el.getBoundingClientRect().height)}`);

	const ids = {};
	for (const el of document.querySelectorAll('[id]')) ids[el.id] = (ids[el.id] || 0) + 1;
	out.duplicateIds = Object.entries(ids).filter(([, n]) => n > 1).map(([id, n]) => `${id} x${n}`).slice(0, 8);

	out.links = [...document.querySelectorAll('a[href]')]
		.map((a) => {
			try {
				return new URL(a.getAttribute('href'), location.href).href.split('#')[0];
			} catch (e) {
				return null;
			}
		})
		.filter(Boolean);
	out.blankTargetsWithoutRel = [...document.querySelectorAll('a[target="_blank"]')].filter((a) => !/noopener|noreferrer/.test(a.rel)).length;

	out.media = {
		videos: document.querySelectorAll('video').length,
		canvases: [...document.querySelectorAll('canvas')].filter(visible).length,
		iframes: document.querySelectorAll('iframe').length,
	};
	out.dataset = Object.fromEntries(Object.entries(document.documentElement.dataset).slice(0, 20));

	const nav = performance.getEntriesByType('navigation')[0];
	out.timing = nav
		? {
				status: nav.responseStatus,
				ttfb: Math.round(nav.responseStart),
				dcl: Math.round(nav.domContentLoadedEventEnd),
				load: Math.round(nav.loadEventEnd),
				transferKB: Math.round((nav.transferSize || 0) / 1024),
			}
		: {};
	out.lcp = await new Promise((resolve) => {
		let last = null;
		try {
			const po = new PerformanceObserver((list) => {
				for (const e of list.getEntries()) last = e;
			});
			po.observe({ type: 'largest-contentful-paint', buffered: true });
			setTimeout(() => {
				po.disconnect();
				resolve(last ? { ms: Math.round(last.startTime), element: last.element ? where(last.element) : '' } : null);
			}, 150);
		} catch (e) {
			resolve(null);
		}
	});
	out.cls = await new Promise((resolve) => {
		let total = 0;
		try {
			const po = new PerformanceObserver((list) => {
				for (const e of list.getEntries()) if (!e.hadRecentInput) total += e.value;
			});
			po.observe({ type: 'layout-shift', buffered: true });
			setTimeout(() => {
				po.disconnect();
				resolve(+total.toFixed(3));
			}, 150);
		} catch (e) {
			resolve(null);
		}
	});
	const res = performance.getEntriesByType('resource');
	out.resources = res.length;
	out.transferKB = Math.round(res.reduce((a, r) => a + (r.transferSize || 0), 0) / 1024) + (out.timing.transferKB || 0);
	out.heaviest = res
		.filter((r) => r.transferSize > 300 * 1024)
		.sort((a, b) => b.transferSize - a.transferSize)
		.slice(0, 3)
		.map((r) => `${r.name.split('?')[0].slice(-70)} ${Math.round(r.transferSize / 1024)} KB`);
	out.scrollHeight = document.documentElement.scrollHeight;
	out.text = short(document.body.innerText, 400);
	return JSON.stringify(out);
})()
