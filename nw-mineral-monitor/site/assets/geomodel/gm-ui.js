/* gm-ui.js — tiny DOM helpers + widgets shared by the viewer and the tools. */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'value') el.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'selected') { if (v) el.setAttribute(k, ''); el[k] = !!v; }
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) { if (c == null || c === false) continue; el.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c); }
  return el;
}
export const clear = el => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
export function row(label, ...controls) { return h('div', { class: 'frow' }, h('label', {}, label), h('div', { class: 'fctl' }, ...controls)); }
export function num(value, attrs = {}) { return h('input', Object.assign({ type: 'number', value: value == null ? '' : value, step: 'any' }, attrs)); }
export function txt(value, attrs = {}) { return h('input', Object.assign({ type: 'text', value: value == null ? '' : value }, attrs)); }
export function sel(options, value, attrs = {}) { const s = h('select', attrs); for (const o of options) { const [v, label] = Array.isArray(o) ? o : [o, o]; s.appendChild(h('option', { value: v, selected: String(v) === String(value) }, label)); } return s; }
/* Every button keeps the base `.b` style: a caller that passes { class: 'x' }
   used to lose it and render as an unstyled browser button. */
export function btn(label, onclick, attrs = {}) { const a = Object.assign({ onclick }, attrs); a.class = 'b' + (attrs.class ? ' ' + attrs.class.replace(/(^|\s)b(\s|$)/, ' ').trim() : ''); return h('button', a, label); }
export function range(value, min, max, step, oninput, attrs = {}) { return h('input', Object.assign({ type: 'range', min, max, step, value, oninput }, attrs)); }
export function note(text, cls = 'note') { return h('div', { class: cls }, text); }
export function kv(pairs) { const dl = h('dl', { class: 'kv' }); for (const [k, v] of pairs) { if (v == null || v === '') continue; dl.appendChild(h('dt', {}, k)); dl.appendChild(h('dd', {}, typeof v === 'string' || typeof v === 'number' ? String(v) : v)); } return dl; }
export function section(title, ...children) { return h('div', { class: 'psec' }, h('h3', {}, title), ...children); }
/** toast(msg, kind, ms, { action: { label, onclick } }) — the action (an UNDO,
    a VIEW LAYER) is a real button inside the toast, which stays up while the
    pointer is over it. */
export function toast(msg, kind = 'info', ms = 3500, opts = {}) {
  let box = document.getElementById('toasts'); if (!box) { box = h('div', { id: 'toasts' }); document.body.appendChild(box); }
  let timer = null; const out = () => { t.classList.add('out'); setTimeout(() => t.remove(), 400); };
  const t = h('div', { class: 'toast ' + kind, onmouseenter: () => clearTimeout(timer), onmouseleave: () => { timer = setTimeout(out, 1500); } }, h('span', {}, msg));
  if (opts.action) t.appendChild(h('button', { class: 'act', onclick: () => { clearTimeout(timer); t.remove(); opts.action.onclick && opts.action.onclick(); } }, opts.action.label));
  if (opts.action || opts.closable) t.appendChild(h('button', { class: 'act x', title: 'dismiss', onclick: () => { clearTimeout(timer); t.remove(); } }, '✕'));
  box.appendChild(t); timer = setTimeout(out, ms); return t;
}
export function modal(title, body, opts = {}) {
  const close = () => { wrap.remove(); document.removeEventListener('keydown', esc, true); };
  const esc = e => { if (e.key === 'Escape' && !opts.sticky) { e.stopPropagation(); close(); } };
  const wrap = h('div', { class: 'modal open', onclick: e => { if (e.target === wrap && !opts.sticky) close(); } },
    h('div', { class: 'modal-card', style: opts.width ? { width: opts.width } : {} },
      h('div', { class: 'modal-head' }, h('h2', {}, title), h('button', { onclick: close, title: 'close (Esc)' }, '✕')),
      h('div', { class: 'modal-body' }, body)));
  document.body.appendChild(wrap); document.addEventListener('keydown', esc, true);
  const first = wrap.querySelector('input,select,textarea,button.b'); if (first) setTimeout(() => first.focus(), 0);
  return { close, el: wrap };
}
/** In-page replacements for window.confirm / window.prompt: styled, keyboard
    driven (Enter = OK, Esc = cancel) and able to list consequences. Both
    return a Promise. */
export function confirmModal(title, body, opts = {}) {
  return new Promise(resolve => {
    const done = v => { m.close(); resolve(v); };
    const content = h('div', {}, typeof body === 'string' ? h('p', {}, body) : body,
      h('div', { class: 'frow', style: { justifyContent: 'flex-end', marginTop: '10px' } },
        btn(opts.cancel || 'CANCEL', () => done(false)),
        btn(opts.ok || 'OK', () => done(true), { class: opts.danger ? 'danger' : 'primary' })));
    const m = modal(title, content, { sticky: true });
    m.el.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); done(true); } if (e.key === 'Escape') { e.preventDefault(); done(false); } });
  });
}
export function promptModal(title, label, value = '', opts = {}) {
  return new Promise(resolve => {
    const done = v => { m.close(); resolve(v); };
    const input = txt(value, { placeholder: opts.placeholder || '' });
    const content = h('div', {}, opts.note ? note(opts.note) : null, row(label, input),
      h('div', { class: 'frow', style: { justifyContent: 'flex-end', marginTop: '10px' } }, btn('CANCEL', () => done(null)), btn(opts.ok || 'OK', () => done(input.value), { class: 'primary' })));
    const m = modal(title, content, { sticky: true });
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); done(input.value); } });
    m.el.addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); done(null); } });
    setTimeout(() => { input.focus(); input.select(); }, 0);
  });
}
/** menu(anchor, items): anchor is an element or {x, y} (a right-click point).
    Items: '-' separator · { head: 'TEXT' } non-clickable group header ·
    { label, hint, disabled, onclick }. */
export function menu(anchor, items) {
  document.querySelectorAll('.ctxmenu').forEach(m => m.remove());
  const r = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : { left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y };
  const m = h('div', { class: 'ctxmenu', style: { left: Math.min(r.left, window.innerWidth - 270) + 'px', top: (r.bottom + 4) + 'px' } });
  for (const it of items) {
    if (it == null) continue;
    if (it === '-') { m.appendChild(h('div', { class: 'sep' })); continue; }
    if (it.head) { m.appendChild(h('div', { class: 'hdr' }, it.head)); continue; }
    m.appendChild(h('div', { class: 'mi' + (it.disabled ? ' dis' : '') + (it.cls ? ' ' + it.cls : ''), title: it.title || '', onclick: () => { if (it.disabled) return; m.remove(); it.onclick && it.onclick(); } }, it.label, it.hint ? h('span', { class: 'hint' }, it.hint) : null));
  }
  document.body.appendChild(m);
  const mh = m.getBoundingClientRect().height; if (r.bottom + 4 + mh > window.innerHeight) m.style.top = Math.max(4, window.innerHeight - mh - 8) + 'px';
  const off = e => { if (!m.contains(e.target)) { m.remove(); document.removeEventListener('mousedown', off, true); document.removeEventListener('keydown', key, true); } };
  const key = e => { if (e.key === 'Escape') { e.stopPropagation(); m.remove(); document.removeEventListener('mousedown', off, true); document.removeEventListener('keydown', key, true); } };
  setTimeout(() => { document.addEventListener('mousedown', off, true); document.addEventListener('keydown', key, true); }, 0);
  return m;
}
export function colorInput(rgb, onchange) { const hex = '#' + rgb.map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join(''); return h('input', { type: 'color', value: hex, class: 'swatch', title: 'layer colour', oninput: e => { const m = /^#(..)(..)(..)$/.exec(e.target.value); if (m) onchange([parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)]); } }); }
export function fmtNum(v, d = 2) { if (v == null || v !== v) return '—'; const a = Math.abs(v); return a >= 1e5 ? Math.round(v).toLocaleString('en-US') : a >= 100 ? (+v).toFixed(1) : (+v).toFixed(d); }

/* ---------------------------------------------------------------- tool head */
/** The uniform strip at the top of every tool panel: step number, purpose,
    then NEEDS (each prerequisite with ✓ / ✗ and an OPEN button that jumps to
    the tool that provides it), HAS (what this step has produced so far) and
    NEXT (where the result goes).  It is the first thing in the panel so the
    sentence precedes the controls. */
export function toolHead({ step, total, title, purpose, needs = [], has = null, next = null, open = null }) {
  const head = h('div', { class: 'toolhead' });
  if (purpose) head.appendChild(h('div', { class: 'tool-sub' }, purpose));
  const rows = [];
  if (needs.length) rows.push(['NEEDS', h('div', { class: 'needs' }, ...needs.map(n => h('span', { class: 'need ' + (n.ok ? 'ok' : 'no'), title: n.why || '' }, (n.ok ? '✓ ' : '✗ ') + n.label, !n.ok && n.open && open ? h('button', { class: 'b x', onclick: () => open(n.open) }, 'OPEN') : null)))]);
  if (has) rows.push(['HAS', typeof has === 'string' ? h('span', { class: 'has' }, has) : has]);
  if (next) rows.push(['NEXT', typeof next === 'string' ? h('span', { class: 'has' }, next) : next]);
  if (rows.length) head.appendChild(kv(rows));
  return head;
}
/** A confidence line sample (solid / dashed / dotted) for legends and lists. */
export function lineSample(dash, color = 'currentColor') { return h('i', { class: 'ln', style: { borderTopStyle: dash ? (dash[0] > 5 ? 'dashed' : 'dotted') : 'solid', color } }); }

export function plotVariogram(canvas, experimental, model, opts = {}) {
  const ctx = canvas.getContext('2d'); const W = canvas.width, H = canvas.height; ctx.clearRect(0, 0, W, H); ctx.fillStyle = '#0f1318'; ctx.fillRect(0, 0, W, H);
  const pad = { l: 44, r: 10, t: 10, b: 28 }; const lags = experimental.map(e => e.lag), gam = experimental.map(e => e.gamma);
  const xmax = Math.max(...lags, 1) * 1.05, ymax = Math.max(...gam, model ? model.sill : 0, 1e-9) * 1.15;
  const X = v => pad.l + v / xmax * (W - pad.l - pad.r), Y = v => H - pad.b - v / ymax * (H - pad.t - pad.b);
  ctx.strokeStyle = '#2a3340'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(pad.l, pad.t); ctx.lineTo(pad.l, H - pad.b); ctx.lineTo(W - pad.r, H - pad.b); ctx.stroke();
  ctx.fillStyle = '#8a97a6'; ctx.font = '10px ui-monospace, monospace'; ctx.textAlign = 'center'; for (let k = 0; k <= 4; k++) { const v = xmax * k / 4; ctx.fillText(fmtNum(v, 0), X(v), H - pad.b + 14); } ctx.textAlign = 'right'; for (let k = 0; k <= 4; k++) { const v = ymax * k / 4; ctx.fillText(fmtNum(v, 2), pad.l - 4, Y(v) + 3); }
  ctx.fillText('lag (m)', W - pad.r, H - 4); ctx.save(); ctx.translate(10, pad.t + 30); ctx.rotate(-Math.PI / 2); ctx.textAlign = 'center'; ctx.fillText('γ(h)', 0, 0); ctx.restore();
  if (model) { ctx.strokeStyle = '#2dd4bf'; ctx.lineWidth = 2; ctx.beginPath(); for (let i = 0; i <= 120; i++) { const hh = xmax * i / 120; const g = model.gamma(hh); if (i === 0) ctx.moveTo(X(hh), Y(g)); else ctx.lineTo(X(hh), Y(g)); } ctx.stroke(); }
  for (const e of experimental) { const r = Math.min(7, 2 + Math.log10(1 + e.pairs)); ctx.fillStyle = '#ffd27a'; ctx.beginPath(); ctx.arc(X(e.lag), Y(e.gamma), r, 0, Math.PI * 2); ctx.fill(); }
  if (opts.title) { ctx.fillStyle = '#c9d1d9'; ctx.textAlign = 'left'; ctx.fillText(opts.title, pad.l + 6, pad.t + 12); }
}
