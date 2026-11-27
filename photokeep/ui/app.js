'use strict';
/* Photos: a local viewer for the library. All data comes from /api/items once; filtering,
   sorting and layout happen here, and only trash/restore/delete/favorite go back to the server. */

const TOKEN = document.querySelector('meta[name="token"]').content;
const $ = (sel, el = document) => el.querySelector(sel);
const put = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false && k !== ''));

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

/* Stroke icons on a 24-unit grid. */
const ICONS = {
  photos: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="M4 18l5-5 4 4 3-3 4 4"/>',
  star: '<path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8L3.5 9.7l5.9-.9z"/>',
  play: '<path d="M8 5.5v13l11-6.5z"/>',
  live: '<circle cx="12" cy="12" r="2.6"/><circle cx="12" cy="12" r="5.6"/><circle cx="12" cy="12" r="8.8" stroke-dasharray="1.6 2.4"/>',
  phone: '<rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="M10.5 18.5h3"/>',
  layers: '<rect x="3" y="7" width="12" height="12" rx="2"/><path d="M8 4h9a3 3 0 0 1 3 3v9"/>',
  blur: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4.5" stroke-dasharray="2 2"/><path d="M12 4v1.5M12 18.5V20M4 12h1.5M18.5 12H20"/>',
  moon: '<path d="M19.5 14.5A8 8 0 1 1 9.5 4.5a6.5 6.5 0 0 0 10 10z"/>',
  grain: '<circle cx="6" cy="6" r=".9"/><circle cx="12" cy="7" r=".9"/><circle cx="18" cy="5" r=".9"/><circle cx="8" cy="12" r=".9"/><circle cx="15" cy="12" r=".9"/><circle cx="5" cy="18" r=".9"/><circle cx="11" cy="17" r=".9"/><circle cx="18" cy="18" r=".9"/><circle cx="19" cy="11" r=".9"/>',
  grid: '<rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/><rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/>',
  weight: '<ellipse cx="12" cy="6" rx="7" ry="2.8"/><path d="M5 6v12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8V6M5 12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8"/>',
  album: '<rect x="4" y="3.5" width="16" height="17" rx="2"/><path d="M8 3.5v17M11.5 8h5M11.5 11.5h5"/>',
  memory: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/>',
  eyeoff: '<path d="M3 3l18 18M10.6 6.2A9.8 9.8 0 0 1 12 6c5 0 8.5 4.2 9.5 6-.5.9-1.6 2.4-3.1 3.7M6.6 7.7C4.6 9 3.2 10.9 2.5 12c1 1.8 4.5 6 9.5 6 1.6 0 3-.4 4.3-1.1"/><path d="M9.9 10a3 3 0 0 0 4.1 4.1"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12.5A2 2 0 0 0 9 21h6a2 2 0 0 0 2-1.5L18 7M9 7V4.5h6V7"/>',
  chart: '<path d="M4 20h16M7 16v-5M12 16V6M17 16v-8"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.3-4.3"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  left: '<path d="M15 5l-7 7 7 7"/>', right: '<path d="M9 5l7 7-7 7"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
  folder: '<path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  restore: '<path d="M4 12a8 8 0 1 0 2.4-5.7M4 4v4.5h4.5"/>',
  checkall: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 12.5l3 3 5-6"/>',
  calendar: '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
  camera: '<path d="M4 8.5a2 2 0 0 1 2-2h2l1.5-2h5l1.5 2h2a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><circle cx="12" cy="12.5" r="3.5"/>',
  pin: '<path d="M12 21s-6.5-6-6.5-11a6.5 6.5 0 0 1 13 0c0 5-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.3"/>',
  file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  zoom: '<rect x="3.5" y="3.5" width="7" height="7" rx="1"/><rect x="13.5" y="13.5" width="7" height="7" rx="1"/><path d="M13.5 4h7v7M4 13.5v7h7"/>',
};
function icon(name, size) {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('class', 'i');
  if (size) s.style.width = s.style.height = size + 'px';
  s.innerHTML = ICONS[name] || '';
  return s;
}

/* Formatting */
const nf = new Intl.NumberFormat();
function fmtSize(b) {
  if (!b) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const e = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1000)));
  const v = b / 1000 ** e;
  return `${v >= 100 || e === 0 ? Math.round(v) : v.toFixed(1)} ${u[e]}`;
}
function fmtDur(s) {
  if (s == null || !Number.isFinite(+s)) return '';
  s = Math.round(+s);
  const m = Math.floor(s / 60), r = s % 60;
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${m}:${String(r).padStart(2, '0')}`;
}
// "YYYY-MM-DDTHH:MM:SS" without zone is read as local wall clock. Logs write microseconds, and only
// Chromium parses more than three fraction digits.
const parseLocal = t => (t ? new Date(t.replace(/(\.\d{3})\d+/, '$1')) : null);
function fmtDate(t, long) {
  const d = parseLocal(t);
  if (!d || Number.isNaN(d.getTime())) return 'No date';
  return d.toLocaleString(undefined, long ? { weekday: 'short', dateStyle: undefined, year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' } : { dateStyle: 'medium' });
}
function monthLabel(key) {
  if (key === 'undated') return 'No date';
  const [y, m] = key.split('-').map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
}
const plural = (n, w) => `${nf.format(n)} ${w}${n === 1 ? '' : 's'}`;
const daysLeft = it => (it.left == null ? null : `${plural(it.left, 'day')} left`);
const dims = it => (it.w && it.h ? `${it.w}×${it.h}` : null);

/* Video support: many iPhone clips are HEVC, which not every browser can decode. */
const probe = document.createElement('video');
const CAN_HEVC = ['hvc1.1.6.L93.B0', 'hev1.1.6.L93.B0'].some(c => probe.canPlayType(`video/mp4; codecs="${c}"`) !== '');
const playable = codec => !(codec === 'hvc1' || codec === 'hev1') || CAN_HEVC;

/* State */
const store = { get(k, d) { try { const v = localStorage.getItem('photos.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
                set(k, v) { try { localStorage.setItem('photos.' + k, JSON.stringify(v)); } catch { /* private mode */ } } };
const ZOOM = [96, 128, 168, 224, 300];
const S = {
  items: [], byId: new Map(), groups: new Map(), list: [], view: 'all', q: '', type: 'all', year: 'all', sort: null,
  zoom: store.get('zoom', 2), sel: new Set(), anchor: null, viewer: null, info: store.get('info', false),
  pct: { blur: 5, noise: 3 }, thr: { dark: 38, mp: 1 }, root: '', collapsed: store.get('collapsed', { albums: false, memories: true }),
};

const SORTS = {
  'date-desc': ['Newest first', (a, b) => (b.t || '').localeCompare(a.t || '') || b.id.localeCompare(a.id)],
  'date-asc': ['Oldest first', (a, b) => (!a.t) - (!b.t) || (a.t || '').localeCompare(b.t || '') || a.id.localeCompare(b.id)],
  'size-desc': ['Largest first', (a, b) => b.s - a.s],
  'size-asc': ['Smallest first', (a, b) => a.s - b.s],
  'dur-desc': ['Longest videos', (a, b) => (b.d || 0) - (a.d || 0)],
  'sharp-asc': ['Blurriest first', (a, b) => (a.sh ?? 1e9) - (b.sh ?? 1e9)],
  'bright-asc': ['Darkest first', (a, b) => (a.br ?? 1e9) - (b.br ?? 1e9)],
  'noise-desc': ['Noisiest first', (a, b) => (b.no ?? -1) - (a.no ?? -1)],
  'mp-asc': ['Lowest resolution', (a, b) => (a.w * a.h || 1e12) - (b.w * b.h || 1e12)],
  'trashed-desc': ['Recently deleted', (a, b) => (b.ta || '').localeCompare(a.ta || '') || b.id.localeCompare(a.id)],
};
const TYPES = [['all', 'All'], ['photo', 'Photos'], ['video', 'Videos'], ['live', 'Live'], ['shot', 'Screenshots']];
const typeOk = (it, t) => t === 'all' || (t === 'photo' ? it.k === 'p' && !it.sc : t === 'video' ? it.k === 'v' : t === 'live' ? it.lv : it.sc);

const visible = it => !it.tr && !it.hi;
// Clean-up suggestions skip favorites and screenshots: those are kept on purpose.
const reviewable = it => visible(it) && it.k === 'p' && !it.sc && !it.f;

const VIEWS = {
  all: { label: 'Photos', icon: 'photos', sec: 'Library', f: visible },
  fav: { label: 'Favorites', icon: 'star', sec: 'Library', f: it => !it.tr && it.f },
  videos: { label: 'Videos', icon: 'play', sec: 'Library', f: it => visible(it) && it.k === 'v', types: false },
  live: { label: 'Live Photos', icon: 'live', sec: 'Library', f: it => visible(it) && it.lv, types: false },
  shots: { label: 'Screenshots', icon: 'phone', sec: 'Library', f: it => visible(it) && it.sc, types: false },
  similar: { label: 'Similar photos', icon: 'layers', sec: 'Clean up', f: it => visible(it) && it.g && (S.groups.get(it.g) || 0) > 1 },
  blurry: { label: 'Blurry', icon: 'blur', sec: 'Clean up', f: it => reviewable(it) && it.sh != null && it.sh <= S.thr.blur, sort: 'sharp-asc' },
  dark: { label: 'Too dark', icon: 'moon', sec: 'Clean up', f: it => reviewable(it) && it.br != null && it.br <= S.thr.dark, sort: 'bright-asc' },
  noisy: { label: 'Noisy / grainy', icon: 'grain', sec: 'Clean up', f: it => reviewable(it) && it.no != null && it.no >= S.thr.noise, sort: 'noise-desc' },
  lowres: { label: 'Low resolution', icon: 'grid', sec: 'Clean up', f: it => reviewable(it) && it.w && it.h && it.w * it.h < S.thr.mp * 1e6, sort: 'mp-asc' },
  largest: { label: 'Largest files', icon: 'weight', sec: 'Clean up', f: visible, sort: 'size-desc', sizes: true },
  hidden: { label: 'Hidden', icon: 'eyeoff', sec: 'More', f: it => !it.tr && it.hi },
  trash: { label: 'Trash', icon: 'trash', sec: 'More', f: it => it.tr, sort: 'trashed-desc' },
  stats: { label: 'Storage & stats', icon: 'chart', sec: 'More', special: true },
};
function viewDef(id) {
  if (id.startsWith('album:')) { const n = id.slice(6); return { label: n, icon: 'album', f: it => !it.tr && it.al.includes(n) }; }
  if (id.startsWith('mem:')) { const n = id.slice(4); return { label: n, icon: 'memory', f: it => !it.tr && it.me.includes(n) }; }
  return VIEWS[id] || VIEWS.all;
}

/* Data */
async function api(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function prep(it) {
  it.mk = it.t ? it.t.slice(0, 7) : 'undated';
  it.y = it.t ? it.t.slice(0, 4) : 'No date';
  it.q = [it.n, it.o, it.t && it.t.slice(0, 10), it.cam, ...it.al, ...it.me, it.k === 'v' ? 'video' : 'photo',
          it.lv && 'live', it.sc && 'screenshot', it.f && 'favorite'].filter(Boolean).join(' ').toLowerCase();
  return it;
}
function reindex() {
  S.byId = new Map(S.items.map(it => [it.id, it]));
  S.groups = new Map();
  for (const it of S.items) if (it.g && visible(it)) S.groups.set(it.g, (S.groups.get(it.g) || 0) + 1);
  const quant = (vals, p) => { vals.sort((a, b) => a - b); return vals.length ? vals[Math.min(vals.length - 1, Math.floor(vals.length * p / 100))] : 0; };
  const rev = S.items.filter(reviewable);
  S.thr.blur = quant(rev.map(i => i.sh).filter(v => v != null), S.pct.blur);
  S.thr.noise = quant(rev.map(i => i.no).filter(v => v != null), 100 - S.pct.noise);
}
function applyChanges(res) {
  const gone = new Set(res.gone || []);
  for (const c of res.items || []) { const old = S.byId.get(c.id); if (old) Object.assign(old, prep(c)); }
  if (gone.size) { S.items = S.items.filter(it => !gone.has(it.id)); for (const id of gone) S.sel.delete(id); }
  reindex();
}

function computeList() {
  const v = viewDef(S.view);
  if (v.special) return (S.list = []);
  const q = S.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  let list = S.items.filter(v.f);
  if (v.types !== false && S.type !== 'all') list = list.filter(it => typeOk(it, S.type));
  if (S.year !== 'all') list = list.filter(it => it.y === S.year);
  if (q.length) list = list.filter(it => q.every(w => it.q.includes(w)));
  const sort = S.sort || v.sort || 'date-desc';
  list.sort(SORTS[sort][1]);
  if (S.view === 'similar') {
    // Keep each group together, newest group first, best photo first inside it.
    const first = new Map();
    list.forEach((it, i) => { if (!first.has(it.g)) first.set(it.g, i); });
    list.sort((a, b) => first.get(a.g) - first.get(b.g) || b.b - a.b || (a.t || '').localeCompare(b.t || ''));
  }
  return (S.list = list);
}

/* Sidebar */
function renderNav() {
  const nav = $('#nav');
  nav.replaceChildren();
  const count = f => S.items.reduce((n, it) => n + (f(it) ? 1 : 0), 0);
  let sec = null;
  for (const [id, v] of Object.entries(VIEWS)) {
    if (v.sec !== sec) { sec = v.sec; nav.append(h('div', { class: 'navsec' }, sec)); }
    const n = v.special ? null : count(v.f);
    nav.append(navBtn(id, v.label, v.icon, n));
  }
  const lists = (key, label, prefix, field, ico) => {
    const counts = new Map();
    for (const it of S.items) if (!it.tr) for (const a of it[field]) counts.set(a, (counts.get(a) || 0) + 1);
    if (!counts.size) return;
    const open = !S.collapsed[key];
    nav.append(h('div', { class: 'navsec' }, label, h('button', { onclick: () => { S.collapsed[key] = open; store.set('collapsed', S.collapsed); renderNav(); } }, open ? 'Hide' : `Show ${counts.size}`)));
    if (!open) return;
    [...counts].sort((a, b) => a[0].localeCompare(b[0])).forEach(([name, n]) => nav.append(navBtn(prefix + name, name, ico, n, true)));
  };
  lists('albums', 'Albums', 'album:', 'al', 'album');
  lists('memories', 'Memories', 'mem:', 'me', 'memory');

  const live = S.items.filter(visible), trash = S.items.filter(it => it.tr);
  const total = live.reduce((s, it) => s + it.s, 0), tsize = trash.reduce((s, it) => s + it.s, 0);
  put($('#sidefoot'),
    h('div', {}, h('b', {}, fmtSize(total)), ` in ${plural(live.length, 'item')}`),
    h('div', { class: 'meter' }, h('i', { style: `width:${Math.max(2, 100 * total / (total + tsize || 1))}%` })),
    tsize ? h('div', {}, `${fmtSize(tsize)} in Trash`) : null,
    h('div', { style: 'margin-top:8px;word-break:break-all;opacity:.8' }, S.root));
}
function navBtn(id, label, ico, n, sub) {
  return h('button', { class: `nav${sub ? ' sub' : ''}${S.view === id ? ' on' : ''}`, title: label, onclick: () => go(id) },
    icon(ico, sub ? 18 : 20), h('span', { class: 'lbl' }, label), n == null ? null : h('span', { class: 'cnt' }, nf.format(n)));
}
function go(id) {
  S.view = id; S.sort = null; S.year = 'all'; S.sel.clear(); S.anchor = null;
  $('#scroller').scrollTop = 0;
  render();
}

/* Toolbar */
function renderTools() {
  const v = viewDef(S.view);
  const types = $('#types');
  types.replaceChildren(...(v.types === false || v.special ? [] : TYPES.map(([id, label]) =>
    h('button', { class: `chip${S.type === id ? ' on' : ''}`, onclick: () => { S.type = id; render(); } }, label))));
  const years = [...new Set(S.items.filter(v.f || (() => false)).map(it => it.y))].sort().reverse();
  const ys = $('#year');
  ys.hidden = !!v.special;
  ys.replaceChildren(h('option', { value: 'all' }, 'All years'), ...years.map(y => h('option', { value: y, selected: y === S.year }, y)));
  const so = $('#sort');
  so.hidden = !!v.special || S.view === 'similar';
  const cur = S.sort || v.sort || 'date-desc';
  so.replaceChildren(...Object.entries(SORTS).filter(([k]) => k !== 'trashed-desc' || S.view === 'trash')
    .map(([k, [label]]) => h('option', { value: k, selected: k === cur }, label)));
}

function renderHead() {
  const v = viewDef(S.view);
  const head = $('#viewhead');
  const size = S.list.reduce((s, it) => s + it.s, 0);
  const sub = v.special ? 'Where your space goes, and what you could free' : `${plural(S.list.length, 'item')} · ${fmtSize(size)}`;
  const ctl = h('div', { class: 'ctl' });
  const slider = (label, min, max, step, val, set, fmt) => {
    const out = h('b', {}, fmt(val));
    ctl.append(h('label', { style: 'display:flex;gap:10px;align-items:center' }, label,
      h('input', { type: 'range', min, max, step, value: val, oninput: e => { out.textContent = fmt(+e.target.value); },
                   onchange: e => { set(+e.target.value); reindex(); render(true); } }), out));
  };
  if (S.view === 'blurry') slider('Show the blurriest', 1, 30, 1, S.pct.blur, x => (S.pct.blur = x), x => `${x}%`);
  if (S.view === 'noisy') slider('Show the noisiest', 1, 30, 1, S.pct.noise, x => (S.pct.noise = x), x => `${x}%`);
  if (S.view === 'dark') slider('Brightness below', 5, 90, 1, S.thr.dark, x => (S.thr.dark = x), x => `${x}`);
  if (S.view === 'lowres') slider('Smaller than', 0.1, 4, 0.1, S.thr.mp, x => (S.thr.mp = x), x => `${x.toFixed(1)} MP`);
  if (S.view === 'similar' && S.list.length) {
    const extra = S.list.filter(it => !it.b);
    const size = fmtSize(extra.reduce((s, it) => s + it.s, 0));
    ctl.append(h('button', { class: 'btn primary', title: `Keep the best of every group, trash ${nf.format(extra.length)} (${size})`, onclick: () => keepBest(extra) }, icon('check'), `Keep best · ${size}`));
  }
  if (S.view === 'trash' && S.list.length) {
    ctl.append(h('button', { class: 'iconbtn', title: 'Restore all', 'aria-label': 'Restore all', onclick: () => act('restore', S.list.map(i => i.id)) }, icon('restore')),
      h('button', { class: 'iconbtn danger', title: 'Empty trash', 'aria-label': 'Empty trash', onclick: () => deleteForever(S.list.map(i => i.id)) }, icon('trash')));
  }
  head.replaceChildren(h('div', {}, h('h1', {}, v.label), h('div', { class: 'sub' }, sub)), ctl);
}

function renderSelbar() {
  const bar = $('#selbar');
  document.body.classList.toggle('selecting', S.sel.size > 0);
  if (!S.sel.size) { bar.hidden = true; return; }
  const items = [...S.sel].map(id => S.byId.get(id)).filter(Boolean);
  const size = items.reduce((s, it) => s + it.s, 0);
  const inTrash = items.every(it => it.tr);
  const allFav = items.every(it => it.f);
  bar.hidden = false;
  const btn = (ico, title, onclick, cls = '') => h('button', { class: `iconbtn ${cls}`, title, 'aria-label': title, onclick }, icon(ico));
  put(bar,
    btn('close', 'Clear (Esc)', clearSel),
    h('span', { class: 'what' }, `${nf.format(items.length)} · ${fmtSize(size)}`),
    btn('checkall', 'Select all (Ctrl+A)', selectAll),
    inTrash ? null : btn('star', allFav ? 'Unfavorite' : 'Favorite', () => act('favorite', [...S.sel], { value: !allFav }), allFav ? 'faved' : ''),
    inTrash ? btn('restore', 'Restore', () => act('restore', [...S.sel])) : null,
    inTrash ? btn('trash', 'Delete forever', () => deleteForever([...S.sel]), 'danger')
            : btn('trash', 'Move to trash (Delete)', () => act('trash', [...S.sel]), 'danger'));
}

/* Grid with lazily filled sections, so 15,000 photos stay fast. */
let observer = null;
let TL = null;
function sections(list) {
  const sort = S.sort || viewDef(S.view).sort || 'date-desc';
  if (S.view === 'similar') {
    const out = new Map();
    list.forEach((it, i) => { if (!out.has(it.g)) out.set(it.g, []); out.get(it.g).push(i); });
    return [...out.values()].map(idx => ({ key: 'g' + list[idx[0]].g, group: true, idx }));
  }
  if (sort.startsWith('date')) {
    const out = [];
    list.forEach((it, i) => { const last = out[out.length - 1]; if (!last || last.key !== it.mk) out.push({ key: it.mk, title: monthLabel(it.mk), idx: [i] }); else last.idx.push(i); });
    return out;
  }
  const out = [];
  for (let i = 0; i < list.length; i += 240) out.push({ key: 'c' + i, idx: list.slice(i, i + 240).map((_, k) => i + k) });
  return out;
}

function render(keepScroll) {
  const sc = $('#scroller');
  const top = keepScroll ? sc.scrollTop : 0;
  document.documentElement.style.setProperty('--cell', ZOOM[S.zoom] + 'px');
  renderNav();
  renderTools();
  computeList();
  renderHead();
  renderSelbar();
  const content = $('#content');
  if (observer) observer.disconnect();
  const v = viewDef(S.view);
  if (v.special) { renderStats(content); TL.set([]); sc.scrollTop = top; return; }
  if (!S.list.length) {
    content.replaceChildren(h('div', { class: 'empty' }, icon(v.icon), h('div', {}, S.q || S.year !== 'all' || S.type !== 'all' ? 'Nothing matches these filters.' : S.view === 'trash' ? 'Trash is empty.' : 'Nothing here.')));
    TL.set([]);
    return;
  }
  const secs = sections(S.list);
  const width = sc.clientWidth - 84;
  const cell = ZOOM[S.zoom], gap = 3;
  const cols = Math.max(1, Math.floor((width + gap) / (cell + gap)));
  const tileW = (width - gap * (cols - 1)) / cols;
  observer = new IntersectionObserver(entries => {
    for (const e of entries) if (e.isIntersecting) { fill(e.target); observer.unobserve(e.target); }
  }, { root: sc, rootMargin: '1600px 0px' });
  const nodes = secs.map(s => {
    const grid = h('div', { class: 'grid' });
    const rows = Math.ceil(s.idx.length / cols);
    grid.style.height = `${rows * tileW + (rows - 1) * gap}px`;
    grid._idx = s.idx;
    const ids = s.idx.map(i => S.list[i].id);
    const allSel = ids.every(id => S.sel.has(id));
    const selAll = h('button', { class: `selall${allSel ? ' on' : ''}`, title: 'Select this group', onclick: () => toggleMany(ids) }, icon('check', 16));
    let node;
    if (s.group) {
      const items = s.idx.map(i => S.list[i]);
      const extra = items.filter(it => !it.b);
      node = h('section', { class: 'group', 'data-key': s.key },
        h('header', {}, h('b', {}, plural(items.length, 'similar photo')),
          h('span', { class: 'meta' }, `${fmtDate(items[0].t)} · ${fmtSize(items.reduce((a, it) => a + it.s, 0))}`),
          h('button', { class: 'iconbtn', title: `Keep best, trash ${extra.length}`, 'aria-label': 'Keep best', onclick: () => keepBest(extra) }, icon('check'))), grid);
    } else {
      const items = s.idx.map(i => S.list[i]);
      node = h('section', { class: 'sec', 'data-key': s.key },
        h('header', {}, selAll, s.title ? h('h2', {}, s.title) : h('h2', {}, `${nf.format(s.idx[0] + 1)}–${nf.format(s.idx[s.idx.length - 1] + 1)}`),
          h('span', { class: 'meta' }, `${nf.format(items.length)} · ${fmtSize(items.reduce((a, it) => a + it.s, 0))}`)), grid);
    }
    observer.observe(grid);
    return node;
  });
  content.replaceChildren(...nodes);
  sc.scrollTop = top;
  TL.set(timelineSections(secs, nodes));
}
function fill(grid) {
  const v = viewDef(S.view);
  const sort = S.sort || v.sort || 'date-desc';
  const label = v.sizes || sort.startsWith('size') ? 'size' : sort === 'dur-desc' ? 'dur' : sort === 'mp-asc' ? 'mp' : null;
  grid.replaceChildren(...grid._idx.map(i => tile(S.list[i], i, label)));
  grid.style.height = '';
}
function tile(it, i, label) {
  const badges = h('div', { class: 'badges' },
    it.k === 'v' ? h('span', {}, fmtDur(it.d), icon('play')) : null,
    it.lv ? h('span', { title: 'Live Photo' }, icon('live')) : null);
  const lab = it.tr ? daysLeft(it) : label === 'size' ? fmtSize(it.s) : label === 'dur' && it.d ? fmtDur(it.d) : label === 'mp' ? dims(it) : null;
  return h('div', { class: `tile${S.sel.has(it.id) ? ' sel' : ''}`, 'data-i': i, 'data-id': it.id, title: `${fmtDate(it.t, true)} · ${fmtSize(it.s)}` },
    it.th ? h('img', { src: `/t/${it.id}`, loading: 'lazy', decoding: 'async', alt: '' }) : h('div', { class: 'noimg' }, (it.n.split('.').pop() || '').toUpperCase()),
    h('button', { class: 'check', 'aria-label': 'Select', tabindex: -1 }, icon('check')),
    badges,
    it.f ? h('span', { class: 'fav' }, icon('star')) : null,
    S.view === 'similar' && it.b ? h('span', { class: 'best' }, 'BEST') : null,
    lab ? h('span', { class: 'label' }, lab) : null);
}
function timelineSections(secs, nodes) {
  const sort = S.sort || viewDef(S.view).sort || 'date-desc';
  if (!sort.startsWith('date') || S.view === 'similar' || secs.length < 2) return [];
  return secs.map((s, k) => {
    const [y, m] = s.key.split('-').map(Number);
    return { node: nodes[k], year: s.key === 'undated' ? '—' : String(y),
             label: s.key === 'undated' ? 'No date' : new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: 'short', year: 'numeric' }) };
  });
}

/* Selection */
function setTileSel(id) {
  const on = S.sel.has(id);
  document.querySelectorAll(`.tile[data-id="${id}"]`).forEach(t => t.classList.toggle('sel', on));
}
function toggleSel(i, shift) {
  const it = S.list[i];
  if (shift && S.anchor != null) {
    const [a, b] = [Math.min(S.anchor, i), Math.max(S.anchor, i)];
    const on = !S.sel.has(it.id);
    for (let k = a; k <= b; k++) { const id = S.list[k].id; on ? S.sel.add(id) : S.sel.delete(id); setTileSel(id); }
  } else {
    S.sel.has(it.id) ? S.sel.delete(it.id) : S.sel.add(it.id);
    setTileSel(it.id);
  }
  S.anchor = i;
  renderSelbar();
}
function toggleMany(ids) {
  const on = !ids.every(id => S.sel.has(id));
  ids.forEach(id => { on ? S.sel.add(id) : S.sel.delete(id); setTileSel(id); });
  renderSelbar();
  document.querySelectorAll('.selall').forEach(b => b.classList.remove('on'));
}
function selectAll() { S.list.forEach(it => S.sel.add(it.id)); document.querySelectorAll('.tile').forEach(t => t.classList.add('sel')); renderSelbar(); }
function clearSel() { S.sel.clear(); S.anchor = null; document.querySelectorAll('.tile.sel').forEach(t => t.classList.remove('sel')); renderSelbar(); }

$('#content').addEventListener('click', e => {
  const t = e.target.closest('.tile');
  if (!t || !t.closest('#content')) return;
  const i = +t.dataset.i;
  if (e.target.closest('.check') || S.sel.size || e.ctrlKey || e.metaKey || e.shiftKey) toggleSel(i, e.shiftKey);
  else openViewer(i);
});

/* Actions */
async function act(kind, ids, extra = {}, quiet) {
  if (!ids.length) return;
  try {
    const res = await api('/api/' + kind, { ids, ...extra });
    applyChanges(res);
    if (kind !== 'favorite') ids.forEach(id => S.sel.delete(id));
    if (S.viewer) viewerAfterChange(); else render(true);
    if (quiet) return;
    const n = ids.length > 1 ? `${nf.format(ids.length)} ` : '';
    if (kind === 'trash') toast(`${n}Moved to trash`, () => act('restore', ids, {}, true));
    if (kind === 'restore') toast(`${n}Restored`, () => act('trash', ids, {}, true));
    if (kind === 'delete') toast(`${n}Deleted`);
  } catch (err) {
    toast(`Could not ${kind}: ${err.message}`);
  }
}
async function deleteForever(ids) {
  const size = ids.reduce((s, id) => s + (S.byId.get(id)?.s || 0), 0);
  if (await confirmBox('Delete forever?', `${plural(ids.length, 'item')} · ${fmtSize(size)}. Can't be undone.`, 'Delete', true))
    act('delete', ids);
}
async function keepBest(extra) {
  if (!extra.length) return;
  const size = extra.reduce((s, it) => s + it.s, 0);
  if (await confirmBox('Keep the best?', `${plural(extra.length, 'photo')} · ${fmtSize(size)} to trash.`, 'Trash'))
    act('trash', extra.map(it => it.id));
}

let toastTimer = null;
function toast(msg, undo) {
  const t = $('#toast');
  put(t, h('span', {}, msg), undo ? h('button', { onclick: () => { t.hidden = true; undo(); } }, 'Undo') : null,
    h('button', { 'aria-label': 'Dismiss', onclick: () => { t.hidden = true; } }, icon('close', 16)));
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, undo ? 9000 : 5000);
}
function confirmBox(title, text, ok, danger) {
  return new Promise(resolve => {
    const d = $('#dialog');
    const done = v => { d.hidden = true; document.removeEventListener('keydown', onKey, true); resolve(v); };
    const onKey = e => { if (e.key === 'Escape') { e.stopPropagation(); done(false); } };
    const okBtn = h('button', { class: `btn ${danger ? 'danger' : 'primary'}`, onclick: () => done(true) }, ok);
    d.replaceChildren(h('div', { class: 'box', role: 'dialog', 'aria-modal': 'true' }, h('h2', {}, title), h('p', {}, text),
      h('div', { class: 'row' }, h('button', { class: 'btn ghost', onclick: () => done(false) }, 'Cancel'), okBtn)));
    d.hidden = false;
    document.addEventListener('keydown', onKey, true);
    okBtn.focus();
  });
}

/* Viewer */
function openViewer(i) { S.viewer = { i }; renderViewer(); }
function closeViewer() {
  const v = $('#viewer');
  v.querySelectorAll('video').forEach(x => x.pause());
  v.hidden = true;
  v.replaceChildren();
  S.viewer = null;
  render(true);
}
function viewerAfterChange() {
  computeList();
  if (!S.list.length) return closeViewer();
  S.viewer.i = Math.min(S.viewer.i, S.list.length - 1);
  renderViewer();
}
function step(d) {
  if (!S.viewer) return;
  const n = S.viewer.i + d;
  if (n < 0 || n >= S.list.length) return;
  S.viewer.i = n;
  renderViewer();
}
function renderViewer() {
  const it = S.list[S.viewer.i];
  const box = $('#viewer');
  box.querySelectorAll('video').forEach(x => x.pause());
  const stage = h('div', { class: 'stage' });
  if (it.k === 'v') {
    if (playable(it.c)) {
      const vid = h('video', { class: 'media', src: `/v/${it.id}`, controls: true, autoplay: true, playsinline: true, poster: it.th ? `/t/${it.id}` : null });
      vid.addEventListener('error', () => vid.replaceWith(fallback(it)));
      stage.append(vid);
    } else stage.append(fallback(it));
  } else {
    const img = h('img', { class: 'media', alt: it.n, src: it.th ? `/t/${it.id}` : '' });
    const full = new Image();
    full.onload = () => { img.src = full.src; };
    full.src = `/p/${it.id}`;
    stage.append(img);
    if (it.lv) {
      const liveBtn = h('button', { class: 'live', title: 'Play the Live Photo' }, icon('live'), 'LIVE');
      liveBtn.addEventListener('click', () => {
        if (!playable(it.lc)) return api('/api/open', { id: it.id, live: true }).catch(err => toast(err.message));
        const clip = h('video', { class: 'media', src: `/l/${it.id}`, autoplay: true, playsinline: true });
        clip.addEventListener('ended', () => clip.replaceWith(img));
        clip.addEventListener('error', () => { clip.replaceWith(img); api('/api/open', { id: it.id, live: true }); });
        img.replaceWith(clip);
      });
      stage.append(liveBtn);
    }
  }
  const favBtn = h('button', { class: `iconbtn${it.f ? ' faved' : ''}`, title: 'Favorite (F)', onclick: () => act('favorite', [it.id], { value: !it.f }, true) }, icon('star'));
  const trashBtn = it.tr
    ? h('button', { class: 'iconbtn', title: 'Restore', onclick: () => act('restore', [it.id]) }, icon('restore'))
    : h('button', { class: 'iconbtn', title: 'Move to trash (Delete)', onclick: () => act('trash', [it.id]) }, icon('trash'));
  stage.append(...[ // Element.append writes a null as the text "null"
    h('div', { class: 'vbar' },
      h('button', { class: 'iconbtn', title: 'Close (Esc)', onclick: closeViewer }, icon('close')),
      h('div', { class: 'title' }, fmtDate(it.t, true), h('small', {}, [it.n, fmtSize(it.s), dims(it)].filter(Boolean).join(' · '))),
      it.tr ? null : favBtn,
      h('button', { class: `iconbtn${S.info ? ' active' : ''}`, title: 'Info (I)', onclick: () => { S.info = !S.info; store.set('info', S.info); renderViewer(); } }, icon('info')),
      h('a', { class: 'iconbtn', title: 'Download original', href: `/o/${it.id}`, download: it.n }, icon('download')),
      h('button', { class: 'iconbtn', title: 'Open in another app', onclick: () => api('/api/open', { id: it.id }).catch(err => toast(err.message)) }, icon('external')),
      trashBtn),
    S.viewer.i > 0 ? h('button', { class: 'nav-btn prev', title: 'Previous (←)', onclick: () => step(-1) }, icon('left')) : null,
    S.viewer.i < S.list.length - 1 ? h('button', { class: 'nav-btn next', title: 'Next (→)', onclick: () => step(1) }, icon('right')) : null,
    h('div', { class: 'counter' }, `${nf.format(S.viewer.i + 1)} of ${nf.format(S.list.length)}`)].filter(Boolean));
  put(box, stage, S.info ? infoPanel(it) : null);
  box.hidden = false;
  [S.list[S.viewer.i + 1], S.list[S.viewer.i - 1]].forEach(n => { if (n && n.k === 'p') new Image().src = `/p/${n.id}`; });
}
function fallback(it) {
  return h('div', { class: 'fallback' },
    it.th ? h('img', { src: `/t/${it.id}`, alt: '' }) : null,
    h('button', { class: 'nav-btn play', title: 'Play in video player', 'aria-label': 'Play in video player', onclick: () => api('/api/open', { id: it.id }).catch(err => toast(err.message)) }, icon('play')));
}
function infoPanel(it) {
  const row = (ico, t, d) => h('div', { class: 'irow' }, icon(ico), h('div', {}, h('div', { class: 't' }, t), d ? h('div', { class: 'd' }, d) : null));
  const srcNote = { exif: 'from the camera', provider: 'from the photo service (no camera date in the file)' }[it.ds] || '';
  const mp = dims(it) && Number.isFinite(it.w * it.h) ? `${(it.w * it.h / 1e6).toFixed(1)} MP · ${dims(it)}` : '';
  const codec = { hvc1: 'HEVC', avc1: 'H.264' }[it.c] || it.c;
  const kind = it.k === 'v' ? ['Video', fmtDur(it.d), codec].filter(Boolean).join(' · ')
             : it.sc ? 'Screenshot' : it.lv ? 'Live Photo' : 'Photo';
  const tags = (list, prefix) => h('div', { class: 'tags' }, list.map(a => h('button', { onclick: () => { closeViewer(); go(prefix + a); } }, a)));
  const score = (label, val, max, fmt) => val == null ? null : h('div', { class: 'score' }, h('span', {}, label),
    h('div', { class: 'bar' }, h('i', { style: `width:${Math.min(100, 100 * val / max)}%` })), h('span', {}, fmt(val)));
  const sim = it.g && (S.groups.get(it.g) || 0) > 1;
  return h('aside', { id: 'info' },
    h('h3', {}, 'Info', h('button', { class: 'iconbtn', title: 'Close info', onclick: () => { S.info = false; store.set('info', false); renderViewer(); } }, icon('close'))),
    row('calendar', fmtDate(it.t, true), [it.z ? `UTC${it.z}` : '', srcNote].filter(Boolean).join(' · ')),
    row('file', it.n, `${fmtSize(it.s)} · ${kind}${mp ? ' · ' + mp : ''}`),
    it.cam ? row('camera', it.cam) : null,
    Number.isFinite(it.la) && Number.isFinite(it.lo) ? row('pin', `${it.la.toFixed(5)}, ${it.lo.toFixed(5)}`,
      h('a', { href: `https://www.openstreetmap.org/?mlat=${it.la}&mlon=${it.lo}#map=16/${it.la}/${it.lo}`, target: '_blank', rel: 'noreferrer' }, 'Map')) : null,
    it.al.length ? row('album', 'Albums', tags(it.al, 'album:')) : null,
    it.me.length ? row('memory', 'Memories', tags(it.me, 'mem:')) : null,
    row('folder', it.tr ? 'In Trash' : it.dir, it.tr ? [it.r, it.ta && fmtDate(it.ta, true), daysLeft(it)].filter(Boolean).join(' · ') : it.o ? `Originally ${it.o}` : null),
    it.k === 'p' ? h('div', { class: 'irow' }, icon('chart'), h('div', { style: 'flex:1' }, h('div', { class: 't' }, 'Quality hints'),
      score('Sharpness', it.sh, 1500, v => Math.round(v)), score('Brightness', it.br, 255, v => Math.round(v)), score('Grain', it.no, 8, v => v.toFixed(1)))) : null,
    sim ? h('div', { class: 'irow' }, icon('layers'), h('div', {}, h('div', { class: 't' }, `${S.groups.get(it.g)} similar photos${it.b ? ' · this is the best' : ''}`),
      h('div', { class: 'd' }, h('a', { href: '#', onclick: e => { e.preventDefault(); closeViewer(); S.q = ''; $('#q').value = ''; go('similar'); } }, 'Review')))) : null,
    h('div', { class: 'irow' }, icon('folder'), h('div', {}, h('a', { href: '#', onclick: e => { e.preventDefault(); api('/api/open', { id: it.id, folder: true }); } }, 'Show in folder'))));
}

/* Stats */
function renderStats(content) {
  const live = S.items.filter(it => !it.tr), trash = S.items.filter(it => it.tr);
  const sum = l => l.reduce((s, it) => s + it.s, 0);
  const photos = live.filter(it => it.k === 'p'), videos = live.filter(it => it.k === 'v');
  const card = (k, v, s, view) => h('div', { class: `card${view ? ' click' : ''}`, onclick: view ? () => go(view) : null }, h('div', { class: 'k' }, k), h('div', { class: 'v' }, v), s ? h('div', { class: 's' }, s) : null);
  const simExtra = live.filter(it => visible(it) && it.g && (S.groups.get(it.g) || 0) > 1 && !it.b);
  const blurry = live.filter(VIEWS.blurry.f), shots = live.filter(it => it.sc);
  const years = new Map();
  for (const it of live) { const y = years.get(it.y) || { p: 0, v: 0, n: 0 }; y[it.k] += it.s; y.n++; years.set(it.y, y); }
  const maxY = Math.max(1, ...[...years.values()].map(y => y.p + y.v));
  const top = [...live].sort((a, b) => b.s - a.s).slice(0, 12);
  content.replaceChildren(
    h('div', { class: 'stats' },
      card('Library', fmtSize(sum(live)), plural(live.length, 'item')),
      card('Photos', fmtSize(sum(photos)), plural(photos.length, 'photo'), 'all'),
      card('Videos', fmtSize(sum(videos)), `${plural(videos.length, 'video')} · ${fmtDur(videos.reduce((s, it) => s + (it.d || 0), 0))} total`, 'videos'),
      card('Live Photos', nf.format(live.filter(it => it.lv).length), 'photo + short clip', 'live'),
      card('Trash', fmtSize(sum(trash)), plural(trash.length, 'item') + ' · not freed yet', 'trash')),
    h('div', { class: 'panel' }, h('h3', {}, 'You could free'),
      h('div', { class: 'stats', style: 'margin-top:0' },
        card('Similar photos (not the best)', fmtSize(sum(simExtra)), plural(simExtra.length, 'photo'), 'similar'),
        card(`Blurriest ${S.pct.blur}%`, fmtSize(sum(blurry)), plural(blurry.length, 'photo'), 'blurry'),
        card('Screenshots', fmtSize(sum(shots)), plural(shots.length, 'screenshot'), 'shots'),
        card('Videos over 100 MB', fmtSize(sum(videos.filter(v => v.s > 1e8))), plural(videos.filter(v => v.s > 1e8).length, 'video'), 'largest'))),
    h('div', { class: 'panel' }, h('h3', {}, 'By year'),
      h('div', { class: 'legend' }, h('span', {}, h('i', { style: 'background:var(--accent)' }), 'Photos'), h('span', {}, h('i', { style: 'background:#f9ab00' }), 'Videos')),
      h('div', { class: 'bars' }, [...years].sort((a, b) => b[0].localeCompare(a[0])).map(([y, v]) => [
        h('b', {}, y),
        h('div', { class: 'bar' }, h('i', { class: 'p', style: `width:${100 * v.p / maxY}%` }), h('i', { class: 'v', style: `width:${100 * v.v / maxY}%` })),
        h('span', { class: 'n' }, `${nf.format(v.n)} · ${fmtSize(v.p + v.v)}`)]))),
    h('div', { class: 'panel' }, h('h3', {}, 'Heaviest files'),
      h('div', { class: 'toplist' }, top.map(it => {
        const t = tile(it, 0, 'size');
        t.addEventListener('click', () => { go('largest'); const i = S.list.findIndex(x => x.id === it.id); if (i >= 0) openViewer(i); });
        return t;
      }))));
}

/* Keyboard */
document.addEventListener('keydown', e => {
  if (!$('#dialog').hidden) return;
  const typing = e.target.matches('input, select, textarea');
  if (S.viewer) {
    const it = S.list[S.viewer.i];
    if (e.key === 'Escape') closeViewer();
    else if (e.key === 'ArrowRight') step(1);
    else if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'Delete' || e.key === 'Backspace') act(it.tr ? 'restore' : 'trash', [it.id]);
    else if (e.key.toLowerCase() === 'f' && !it.tr) act('favorite', [it.id], { value: !it.f }, true);
    else if (e.key.toLowerCase() === 'i') { S.info = !S.info; store.set('info', S.info); renderViewer(); }
    else return;
    e.preventDefault();
    return;
  }
  if (typing) { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.key === 'Escape' && S.sel.size) clearSel();
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') { e.preventDefault(); selectAll(); }
  else if (e.key === 'Delete' && S.sel.size) { const ids = [...S.sel]; ids.every(id => S.byId.get(id)?.tr) ? deleteForever(ids) : act('trash', ids); }
  else if (e.key === '/') { e.preventDefault(); $('#q').focus(); }
});

/* Wiring */
function setTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  store.set('theme', t);
}
function init() {
  $('#logo').append(icon('photos'));
  $('#searchicon').append(icon('search'));
  $('#zoomicon').append(icon('zoom', 18));
  $('#theme').append(icon('sun'));
  setTheme(store.get('theme', null));
  $('#theme').addEventListener('click', () => {
    const dark = getComputedStyle(document.documentElement).colorScheme.includes('dark');
    setTheme(dark ? 'light' : 'dark');
  });
  const zoom = $('#zoom');
  zoom.value = S.zoom;
  zoom.addEventListener('input', () => { S.zoom = +zoom.value; store.set('zoom', S.zoom); render(true); });
  let qt = null;
  $('#q').addEventListener('input', e => { clearTimeout(qt); qt = setTimeout(() => { S.q = e.target.value; render(); }, 180); });
  $('#year').addEventListener('change', e => { S.year = e.target.value; render(); });
  $('#sort').addEventListener('change', e => { S.sort = e.target.value; render(); });
  let rt = null;
  addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => { if (!S.viewer) render(true); }, 200); });
  TL = createTimeline($('#scroller'), $('#rail'));
  fetch('/api/items').then(r => r.json()).then(j => {
    S.root = j.root;
    S.items = j.items.map(prep);
    reindex();
    render();
  }).catch(err => { $('#content').replaceChildren(h('div', { class: 'empty' }, `Could not load the library: ${err.message}`)); });
}
init();
