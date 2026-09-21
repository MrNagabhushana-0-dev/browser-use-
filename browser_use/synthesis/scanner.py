"""Read a page's affordances the way an assistive technology would.

Not the HTML. The three ways an agent can perceive a page each fail differently: pixels
carry no semantics and cost a fortune, a DOM dump buries the four things you can actually
do under ten thousand nodes of layout, and WebMCP is perfect but almost nobody implements
it. What is left is the layer browsers already compute for screen readers — role,
accessible name, value, state — which is small, semantic, and exists on every site whether
its authors thought about agents or not.

This scanner extracts affordances from that layer: the forms, the controls, the buttons a
person could operate. It runs as one script in the page, so the cost is a single round
trip rather than a document crossing the context window.
"""

# Accessible-name computation, trimmed to what actually decides a control's name in
# practice: the explicit label, then the associated <label>, then the visible text, then
# the fallbacks authors reach for. Full AccName is a spec unto itself and the remainder of
# it almost never changes the answer for interactive controls.
SCAN_JS = r"""
const MAX_CONTROLS = 40;
const MAX_FORMS = 12;
const MAX_BUTTONS = 25;

const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120);


// querySelectorAll stops at a shadow boundary, which makes every control inside a web
// component invisible — and web components are exactly where the interesting custom UI
// lives. Walk open roots too. Closed roots are unreachable by design and stay so.
const MAX_SHADOW_ROOTS = 400;
const deepQuery = (selector, root) => {
	root = root || document;
	const out = [...root.querySelectorAll(selector)];
	let budget = MAX_SHADOW_ROOTS;
	const descend = (node) => {
		for (const el of node.querySelectorAll('*')) {
			if (budget <= 0) return;
			if (el.shadowRoot) {
				budget--;
				out.push(...el.shadowRoot.querySelectorAll(selector));
				descend(el.shadowRoot);
			}
		}
	};
	descend(root);
	return out;
};

const INTERACTIVE = 'a[href], button, input, select, textarea, option, label, [role="button"], [role="link"], [role="tab"], [role="checkbox"], [role="switch"], [role="menuitem"]';

const accessibleName = (el) => {
	if (!el) return '';
	const aria = el.getAttribute && el.getAttribute('aria-label');
	if (aria) return clean(aria);
	const by = el.getAttribute && el.getAttribute('aria-labelledby');
	if (by) {
		const parts = by.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean);
		if (parts.length) return clean(parts.map(p => p.innerText || p.textContent).join(' '));
	}
	if (el.id) {
		const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
		if (lab) return clean(lab.innerText || lab.textContent);
	}
	const wrapping = el.closest && el.closest('label');
	if (wrapping) return clean(wrapping.innerText || wrapping.textContent);
	for (const attr of ['placeholder', 'title', 'alt', 'name']) {
		const v = el.getAttribute && el.getAttribute(attr);
		if (v) return clean(v);
	}
	// A <caption> names its table and a <legend> names its fieldset — that is the accessible
	// name per spec, not a fallback. Without this a table inside a web component has no
	// handle at all: no id, no visible-text name, and no CSS path across the boundary.
	const tag = el.tagName.toLowerCase();
	if (tag === 'table' || tag === 'fieldset' || el.getAttribute('role') === 'table' || el.getAttribute('role') === 'grid') {
		const cap = el.querySelector('caption, legend');
		if (cap) return clean(cap.innerText || cap.textContent);
	}
	// Visible text names a control; it does not name a container. Falling back to innerText
	// for a <table> or <form> yields its entire subtree as the "name", which is both useless
	// and unmatchable when resolving the locator later.
	// Deliberately not el.value: a field's current contents are not its name. Falling back
	// to it writes whatever the user typed into a persisted locator, and binds that locator
	// to a string that is gone on the next page load. An unnamed field gets a CSS path.
	if (el.matches && el.matches(INTERACTIVE)) return clean(el.innerText);
	return '';
};

// The role the platform would report. Explicit role wins; otherwise derive it from the
// tag and type the way the HTML-AAM mapping does for the elements that matter here.
const roleOf = (el) => {
	const explicit = el.getAttribute && el.getAttribute('role');
	if (explicit) return explicit.toLowerCase();
	const tag = el.tagName.toLowerCase();
	if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
	if (tag === 'button') return 'button';
	if (tag === 'select') return 'combobox';
	if (tag === 'textarea') return 'textbox';
	if (tag === 'input') {
		const t = (el.getAttribute('type') || 'text').toLowerCase();
		if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
		if (t === 'checkbox') return 'checkbox';
		if (t === 'radio') return 'radio';
		if (t === 'search') return 'searchbox';
		return 'textbox';
	}
	return 'generic';
};

// A handle that survives a re-render. data-testid first because it is the one attribute
// authors keep stable on purpose; then id; then role plus accessible name, which is what
// a person would say out loud; a CSS path only as a last resort.
const locatorFor = (el) => {
	const testid = el.getAttribute('data-testid') || el.getAttribute('data-test-id') || el.getAttribute('data-test');
	const loc = {testid: testid || null, id: el.id || null, role: roleOf(el), name: accessibleName(el), css: null};
	const inShadow = el.getRootNode() !== document;
	if (!testid && !el.id && !loc.name && !inShadow) {
		const parts = [];
		let node = el;
		for (let depth = 0; node && node.nodeType === 1 && depth < 8; depth++) {
			let seg = node.tagName.toLowerCase();
			const parent = node.parentElement;
			if (parent) {
				const sibs = [...parent.children].filter(c => c.tagName === node.tagName);
				if (sibs.length > 1) seg += `:nth-of-type(${sibs.indexOf(node) + 1})`;
			}
			parts.unshift(seg);
			if (node === document.body || !parent) break;
			node = parent;
		}
		loc.css = parts.join(' > ');
	}
	return loc;
};

const visible = (el) => {
	const r = el.getBoundingClientRect();
	if (r.width < 2 || r.height < 2) return false;
	const s = getComputedStyle(el);
	if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0') return false;
	// Painted but not operable. Content behind a modal is usually marked one of these ways,
	// and a tool bound to it would click something the user cannot reach.
	if (el.closest('[inert], [aria-hidden="true"]')) return false;
	return true;
};

// While a modal is open, it is the only thing a person can interact with. Synthesizing
// tools for the page behind it produces tools that look fine and click nothing.
const openModal = () => {
	const candidates = deepQuery('dialog[open], [role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]');
	for (let i = candidates.length - 1; i >= 0; i--) {
		const el = candidates[i];
		const r = el.getBoundingClientRect();
		const s = getComputedStyle(el);
		if (r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none') return el;
	}
	return null;
};

const MODAL = openModal();
// Everything below scans this root. With no modal it is the document, and nothing changes.
const SCOPE = MODAL || document;

// Never describe a field that holds a secret, and never read its value back.
const sensitive = (el) => {
	const t = (el.getAttribute('type') || '').toLowerCase();
	if (t === 'password') return true;
	const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
	return ac.includes('cc-') || ac.includes('one-time-code') || ac === 'current-password' || ac === 'new-password';
};

const describeControl = (el) => ({
	role: roleOf(el),
	name: accessibleName(el),
	type: (el.getAttribute('type') || '').toLowerCase() || null,
	required: !!(el.required || el.getAttribute('aria-required') === 'true'),
	sensitive: sensitive(el),
	options: el.tagName.toLowerCase() === 'select'
		? [...el.options].slice(0, 25).map(o => clean(o.textContent)).filter(Boolean)
		: null,
	locator: locatorFor(el),
});

const CONTROL_SELECTOR = 'input, textarea, select, [contenteditable="true"], [role="textbox"], [role="searchbox"], [role="combobox"]';
// Deliberately excludes a[href] unless it is marked up as a button. A link changes the
// page's location, which `navigate` already does; treating anchors as tools turned a news
// front page into two dozen 'tools' named after its headlines.
const BUTTON_SELECTOR = 'button, input[type=submit], input[type=button], [role="button"]';

// Deliberately NOT accessibleName(form): a form's innerText is every label, option and
// hint inside it, which yields names like "Email Password Sign in". What a person would
// call a form is what its submit button says.
const formName = (form, submit) => {
	const aria = form.getAttribute('aria-label');
	if (aria) return clean(aria);
	const by = form.getAttribute('aria-labelledby');
	if (by) {
		const parts = by.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean);
		if (parts.length) return clean(parts.map(p => p.innerText).join(' '));
	}
	if (submit) {
		const label = accessibleName(submit);
		if (label) return label;
	}
	const heading = form.querySelector('h1, h2, h3, legend');
	if (heading) return clean(heading.innerText);
	return clean(form.getAttribute('name') || form.getAttribute('id') || '');
};

// Which candidates the budget buys, when there are more than it can hold.
//
// Taking them in document order is the obvious implementation and it is wrong on exactly
// the pages where the cap binds. Measured on reddit.com: 42 buttons, 5 of them on screen,
// and document order spent 25 slots on 22 the reader could not see while dropping 2 of
// the 5 they could. A control below the fold is still usable — the resolver scrolls to
// it — so this ranks rather than excludes, and only matters once the budget is tight.
const byRelevance = (elements, limit) => {
	const scored = elements.map((el, index) => {
		const r = el.getBoundingClientRect();
		const onScreen = r.bottom > 0 && r.top < innerHeight;
		// How far it is from the part of the page being looked at, downwards or up.
		const distance = onScreen ? 0 : r.top >= innerHeight ? r.top - innerHeight : -r.bottom;
		return {el, index, onScreen, distance};
	});
	scored.sort((a, b) =>
		(b.onScreen - a.onScreen) || (a.distance - b.distance) || (a.index - b.index));
	// Back into document order once chosen: the order decides tool names, and a name that
	// changes with the scroll position is a name nothing can rely on.
	return scored.slice(0, limit).sort((a, b) => a.index - b.index).map(s => s.el);
};

const forms = [];
for (const form of deepQuery('form', SCOPE).slice(0, MAX_FORMS)) {
	if (!visible(form)) continue;
	const controls = byRelevance(deepQuery(CONTROL_SELECTOR, form).filter(visible), MAX_CONTROLS)
		.map(describeControl);
	if (!controls.length) continue;
	const submit = form.querySelector('button[type=submit], input[type=submit], button:not([type])');
	forms.push({
		name: formName(form, submit),
		action: form.getAttribute('action') || '',
		controls: controls,
		submit: submit ? {label: accessibleName(submit), locator: locatorFor(submit)} : null,
		locator: locatorFor(form),
	});
}

// Buttons that are not inside a form: the standalone verbs of the page.
const buttonCandidates = deepQuery(BUTTON_SELECTOR, SCOPE)
	.slice(0, 200)
	.filter(el => visible(el) && !el.closest('form') && accessibleName(el));
const buttons = byRelevance(buttonCandidates, MAX_BUTTONS)
	.map(el => ({role: roleOf(el), name: accessibleName(el), locator: locatorFor(el)}));

// Controls outside any form — a site-wide search box usually lives here.
const looseCandidates = deepQuery(CONTROL_SELECTOR, SCOPE)
	.slice(0, 120)
	.filter(el => visible(el) && !el.closest('form'));
const loose = byRelevance(looseCandidates, MAX_CONTROLS).map(describeControl);

// Tables and repeated lists are where the page's *data* lives. Turning them into read
// tools is what stops an agent paging a table into its context one screenshot at a time.
const tables = [];
for (const table of deepQuery('table, [role="table"], [role="grid"]', SCOPE).slice(0, 6)) {
	if (!visible(table)) continue;
	const headerCells = [...table.querySelectorAll('thead th, thead td, tr:first-child th')]
		.map(h => clean(h.innerText)).filter(Boolean).slice(0, 12);
	const bodyRows = table.querySelectorAll('tbody tr').length || Math.max(0, table.querySelectorAll('tr').length - 1);
	if (!headerCells.length || !bodyRows) continue;
	const caption = table.querySelector('caption');
	tables.push({
		name: clean(caption ? caption.innerText : '') || accessibleName(table) || '',
		headers: headerCells,
		rows: bodyRows,
		locator: locatorFor(table),
	});
}

// Tabs and in-page navigation: the verbs that move between views without a form.
const views = [];
for (const el of deepQuery('[role="tab"]', SCOPE).slice(0, 60)) {
	if (views.length >= 12) break;
	if (!visible(el)) continue;
	const name = accessibleName(el);
	if (!name || name.length > 40) continue;
	views.push({role: roleOf(el), name: name, locator: locatorFor(el)});
}

// Pagination, recognised by what the control says rather than by any particular markup.
const PAGER = {next: /^(next|next page|\u203a|\u00bb|\u2192)$/i, previous: /^(prev|previous|previous page|\u2039|\u00ab|\u2190)$/i};
const pagers = [];
for (const el of deepQuery(BUTTON_SELECTOR, SCOPE).slice(0, 200)) {
	if (!visible(el)) continue;
	const name = accessibleName(el);
	for (const kind of Object.keys(PAGER)) {
		if (PAGER[kind].test(name) && !pagers.some(p => p.kind === kind)) {
			pagers.push({kind: kind, name: name, locator: locatorFor(el)});
		}
	}
}

// Standalone checkboxes and switches: settings, filters, consent.
const toggles = [];
for (const el of deepQuery('input[type=checkbox], [role="switch"], [role="checkbox"]', SCOPE).slice(0, 40)) {
	if (toggles.length >= 12) break;
	if (!visible(el) || el.closest('form')) continue;
	const name = accessibleName(el);
	if (!name) continue;
	toggles.push({name: name, checked: !!(el.checked || el.getAttribute('aria-checked') === 'true'), locator: locatorFor(el)});
}

return {url: location.href, origin: location.origin, title: document.title,
        modal: MODAL ? (accessibleName(MODAL) || clean(MODAL.innerText).slice(0, 60)) : null,
        forms: forms, buttons: buttons, controls: loose,
        tables: tables, views: views, pagers: pagers, toggles: toggles};
"""
