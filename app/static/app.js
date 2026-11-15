'use strict';

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const S = {
  cfg: { users: [] },
  user: null,
  items: [],
  byId: new Map(),
  bursts: new Map(),   // burst id -> its photos, in time order
  status: {},
  view: 'sort',
  mode: 'sort',      // what the viewer is doing: 'sort' (auto-advance) or 'browse' (lightbox)
  queue: [],
  pos: 0,
  busy: false,
  gridList: [],
  gridShown: 0,
};
const GRID_PAGE = 240;

// ------------------------------------------------------------------ helpers

async function api(path, body) {
  const opts = body === undefined ? undefined
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText || `HTTP ${r.status}`;
    try { const j = await r.json(); if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch (e) { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}

let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
}

const mine = (it) => it.r[S.user] || [null, 0];
const isRated = (it) => mine(it)[0] != null;
const scoresOf = (it) => S.cfg.users.map((u) => (it.r[u] || [null])[0]).filter((v) => v != null);
const avg = (it) => { const s = scoresOf(it); return s.length ? s.reduce((a, b) => a + b, 0) / s.length : null; };
const anyStar = (it) => S.cfg.users.some((u) => (it.r[u] || [0, 0])[1]);
const allRated = (it) => scoresOf(it).length === S.cfg.users.length;
const current = () => S.byId.get(S.queue[S.pos]);
const FLAG_TEXT = { b: 'looks blurry', d: 'very dark', o: 'overexposed', f: 'no detail' };
const flagText = (it) => (it.q ? [...it.q].map((f) => FLAG_TEXT[f]).filter(Boolean).join(', ') : '');
const burstOf = (it) => { const m = it && it.b != null ? S.bursts.get(it.b) : null; return m && m.length > 1 ? m : null; };

function fmtDate(t) {
  if (!t) return '';
  const d = new Date(t);
  return isNaN(d) ? '' : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
function fmtDur(d) {
  if (d == null) return '';
  const s = Math.round(d);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
function store(key, val) {
  try {
    if (val === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, val);
  } catch (e) { /* private mode: just don't remember */ }
  return null;
}

// ------------------------------------------------------------------ data

async function loadMedia() {
  const data = await api('/api/media');
  S.items = data.items;
  S.byId = new Map(S.items.map((it) => [it.id, it]));
  S.bursts = new Map();
  for (const it of S.items) {
    if (it.b == null) continue;
    if (!S.bursts.has(it.b)) S.bursts.set(it.b, []);
    S.bursts.get(it.b).push(it);
  }
  for (const m of S.bursts.values()) m.sort((a, b) => (a.t || '').localeCompare(b.t || '') || a.id - b.id);
  S.status = data.status;
  updateChrome();
  fillSenders();
}

function updateChrome() {
  const total = S.items.length;
  const rated = S.items.filter(isRated).length;
  $('#progress-text').textContent = total ? `${rated} of ${total} rated · ${total - rated} left` : '';
  $('#progress-fill').style.width = total ? `${(100 * rated) / total}%` : '0';
  $('#who').textContent = S.user || '';

  const st = S.status;
  const b = $('#banner');
  if (st.phase && st.phase !== 'idle') {
    const n = st.total ? ` ${st.done} / ${st.total}` : '';
    b.textContent = `Preparing the library: ${st.phase}${n}. Photos show up here as they become ready.`;
    b.hidden = false;
  } else if (st.error) {
    b.textContent = `Library scan failed: ${st.error}`;
    b.hidden = false;
  } else if (st.problems && st.problems.length) {
    b.textContent = `Some folders could not be read (app runs as ${st.uid}): ${st.problems[0]}`;
    b.hidden = false;
  } else {
    b.hidden = true;
  }
}

function fillSenders() {
  const stats = new Map();
  for (const it of S.items) {
    const s = stats.get(it.s) || { total: 0, left: 0 };
    s.total += 1;
    if (!isRated(it)) s.left += 1;
    stats.set(it.s, s);
  }
  const names = [...stats.keys()].sort((a, b) => a.localeCompare(b));
  const leftAll = S.items.filter((it) => !isRated(it)).length;
  for (const [id, label] of [['#sort-sender', (n, s) => `${n} (${s.left} left)`], ['#browse-sender', (n, s) => `${n} (${s.total})`]]) {
    const sel = $(id);
    const keep = sel.value;
    const all = id === '#sort-sender' ? `All guests (${leftAll} left)` : `All guests (${S.items.length})`;
    sel.innerHTML = `<option value="">${esc(all)}</option>` +
      names.map((n) => `<option value="${esc(n)}">${esc(label(n, stats.get(n)))}</option>`).join('');
    if (keep && stats.has(keep)) sel.value = keep;
  }
}

// ------------------------------------------------------------------ viewer

function placeViewer(overlay) {
  const v = $('#viewer');
  v.hidden = false;
  v.classList.toggle('overlay', overlay);
  $('#v-close').hidden = !overlay;
  (overlay ? document.body : $('#viewer-home')).appendChild(v);
}

function buildQueue() {
  const sender = $('#sort-sender').value;
  const order = $('#sort-order').value;
  let list = S.items.filter((it) => !isRated(it) && (!sender || it.s === sender));
  if (order === 'time') {
    list.sort((a, b) => (a.t || '').localeCompare(b.t || '') || a.id - b.id);
  } else if (order === 'shuffle') {
    for (let i = list.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [list[i], list[j]] = [list[j], list[i]];
    }
  }
  S.queue = list.map((it) => it.id);
  S.pos = 0;
  S.mode = 'sort';
  placeViewer(false);
  show();
}

function noPhotosMessage() {
  const st = S.status;
  const who = st.uid ? ` The app runs as user:group ${esc(st.uid)}.` : '';
  if (st.problems && st.problems.length) {
    return `<div class="emoji">🔒</div><p><strong>The app cannot read the photo folder.</strong>${who}</p>` +
      `<p class="muted">${st.problems.slice(0, 3).map(esc).join('<br>')}</p><p><button id="e-reload">Check again</button></p>`;
  }
  if (st.errors) {
    return `<div class="emoji">⚠️</div><p>${st.found} files were found but none could be turned into a preview. The Results tab lists why.</p>`;
  }
  if (!st.found) {
    return `<div class="emoji">📂</div><p><strong>The photo folder is empty as far as the app can see.</strong> The path on the NAS side of the mount is probably wrong (Docker creates an empty folder when it does not exist), or the guest folders are not readable.${who}</p><p><button id="e-reload">Check again</button></p>`;
  }
  return '<div class="emoji">⏳</div><p>Photos found, previews are on their way.</p><p><button id="e-reload">Check again</button></p>';
}

function emptyMessage() {
  if (S.mode !== 'sort') return '<p>Nothing here.</p>';
  if (!S.items.length) {
    const st = S.status;
    return st.phase && st.phase !== 'idle'
      ? '<div class="emoji">⏳</div><p>The photos are still being prepared. They will appear here shortly.</p><p><button id="e-reload">Check again</button></p>'
      : noPhotosMessage();
  }
  const leftAll = S.items.filter((it) => !isRated(it)).length;
  if (!leftAll) {
    return `<div class="emoji">🎉</div><p><strong>All done, ${esc(S.user)}!</strong> Every photo has your rating.</p><p><button id="e-results" class="primary">See the results</button></p>`;
  }
  const sender = $('#sort-sender').value;
  if (S.queue.length && S.pos >= S.queue.length) {
    return `<div class="emoji">✅</div><p>End of this batch. ${leftAll} still unrated${sender ? ' across all guests' : ' (the ones you skipped)'}.</p><p><button id="e-reload" class="primary">Keep going</button></p>`;
  }
  return `<div class="emoji">✅</div><p>Nothing left to rate from ${esc(sender || 'this selection')}. ${leftAll} left from other guests.</p><p><button id="e-all" class="primary">Continue with all guests</button></p>`;
}

function show() {
  const it = current();
  const img = $('#v-img'), vid = $('#v-vid'), empty = $('#v-empty');
  vid.pause();
  $('#stage').classList.remove('flash');
  S.busy = false;

  if (!it) {
    img.hidden = true; vid.hidden = true;
    vid.removeAttribute('src'); vid.load();
    empty.innerHTML = emptyMessage();
    empty.hidden = false;
    $('#v-sender').textContent = '';
    $('#v-info').textContent = '';
    $('#v-count').textContent = '';
    $('#v-others').textContent = '';
    $('#v-info').classList.remove('warn');
    paintRating(null);
    return;
  }
  empty.hidden = true;
  if (it.k === 'v') {
    img.hidden = true; img.removeAttribute('src');
    vid.hidden = false;
    vid.poster = `/preview/${it.id}`;
    vid.src = `/video/${it.id}`;
    vid.play().catch(() => { /* autoplay with sound refused: the play button is right there */ });
  } else {
    vid.hidden = true; vid.removeAttribute('src'); vid.load();
    img.hidden = false;
    img.src = `/preview/${it.id}`;
  }
  $('#v-sender').textContent = it.s;
  const flags = flagText(it);
  $('#v-info').textContent = [flags ? `⚠ ${flags}` : '', fmtDate(it.t), it.k === 'v' ? `video ${fmtDur(it.d)}` : '', it.n].filter(Boolean).join(' · ');
  $('#v-info').classList.toggle('warn', !!flags);
  $('#v-count').textContent = `${S.pos + 1} / ${S.queue.length}`;
  paintRating(it);

  for (let i = 1; i <= 2; i++) {  // warm the next two photos
    const nx = S.byId.get(S.queue[S.pos + i]);
    if (nx && nx.k === 'i') new Image().src = `/preview/${nx.id}`;
  }
}

function paintRating(it) {
  const [score, star] = it ? mine(it) : [null, 0];
  document.querySelectorAll('#scores button[data-score]').forEach((b) => {
    b.classList.toggle('on', Number(b.dataset.score) === score);
    b.disabled = !it;
  });
  $('#v-star').classList.toggle('on', !!star);
  $('#v-star').disabled = !it;
  // The other person's opinion stays hidden while sorting, so the two ratings are independent.
  let others = '';
  if (it && S.mode === 'browse') {
    others = S.cfg.users.filter((u) => u !== S.user).map((u) => {
      const r = it.r[u];
      return `${u}: ${r && r[0] != null ? r[0] : 'not rated'}${r && r[1] ? ' ★' : ''}`;
    }).join(' · ');
    if (it.x) others += ' · already exported';
  }
  $('#v-others').textContent = others;
  paintBurst(it);
}

function paintBurst(it) {
  const el = $('#v-burst');
  const members = burstOf(it);
  if (!members) { el.hidden = true; el.innerHTML = ''; return; }
  const sharpest = members.reduce((a, b) => ((b.sh || 0) > (a.sh || 0) ? b : a));
  const unratedOthers = members.filter((m) => m !== it && !isRated(m)).length;
  el.innerHTML = `<span>Burst of ${members.length}</span>` + members.map((m) => {
    const score = mine(m)[0];
    return `<button class="bthumb${m === it ? ' on' : ''}" data-id="${m.id}" title="${esc(m.n)}"><img src="/thumb/${m.id}" alt="">` +
      (score != null ? `<span class="badge">${score}</span>` : '') +
      (m === sharpest ? '<span class="sharpest">sharp</span>' : '') + '</button>';
  }).join('') + (unratedOthers ? `<button id="v-best" title="Rates the ${unratedOthers} other unrated shots of this burst as 1 (B)">Keep only this one</button>` : '');
  el.hidden = false;
}

function jumpTo(id) {
  if (S.busy) return;
  let i = S.queue.indexOf(id);
  if (i < 0) { i = S.pos + 1; S.queue.splice(i, 0, id); }
  S.pos = i;
  show();
}

async function rateMany(items, score) {
  // Only touches photos this person has not rated; undone locally if the save fails.
  const todo = items.filter((m) => !isRated(m));
  if (!todo.length) return 0;
  todo.forEach((m) => { m.r[S.user] = [score, 0]; });
  updateChrome();
  try {
    await api('/api/rate-bulk', { user: S.user, ids: todo.map((m) => m.id), score });
  } catch (e) {
    todo.forEach((m) => { delete m.r[S.user]; });
    updateChrome();
    toast(`Could not save: ${e.message}`);
    return 0;
  }
  return todo.length;
}

async function bestOfBurst() {
  const it = current();
  const members = burstOf(it);
  if (!members || S.busy) return;
  const others = members.filter((m) => m !== it && !isRated(m));
  if (!others.length) return;
  const drop = new Set(others.map((m) => m.id));
  const p = rateMany(others, 1);
  if (S.mode === 'sort') {  // the rejected shots no longer need to come up
    S.queue = S.queue.slice(0, S.pos + 1).concat(S.queue.slice(S.pos + 1).filter((id) => !drop.has(id)));
    $('#v-count').textContent = `${S.pos + 1} / ${S.queue.length}`;
  }
  paintRating(it);
  await p;
  if (current() === it) paintRating(it);
}

async function setRating(it, score, star) {
  const prev = it.r[S.user];
  if (score == null && !star) delete it.r[S.user];
  else it.r[S.user] = [score, star ? 1 : 0];
  if (current() === it) paintRating(it);
  updateChrome();
  try {
    await api('/api/rate', { id: it.id, user: S.user, score, star: !!star });
  } catch (e) {
    if (prev) it.r[S.user] = prev; else delete it.r[S.user];
    if (current() === it) paintRating(it);
    updateChrome();
    toast(`Could not save that rating: ${e.message}`);
  }
}

function advance() {
  S.busy = true;
  $('#stage').classList.add('flash');
  setTimeout(() => { S.pos += 1; show(); }, 130);
}

function rate(score) {
  const it = current();
  if (!it || S.busy) return;
  setRating(it, score, mine(it)[1]);
  if (S.mode === 'sort') advance();
}

function toggleStar() {
  const it = current();
  if (!it || S.busy) return;
  const [score, star] = mine(it);
  const on = !star;
  setRating(it, score != null ? score : (on ? 5 : null), on);  // a super-star with no score counts as a 5
  if (S.mode === 'sort' && on && score == null) advance();
}

function clearRating() {
  const it = current();
  if (it && !S.busy) setRating(it, null, false);
}

function next() {
  if (S.busy) return;
  const max = S.mode === 'sort' ? S.queue.length : S.queue.length - 1;
  if (S.pos < max) { S.pos += 1; show(); }
}

function back() {
  if (S.busy) return;
  if (S.pos > 0) { S.pos -= 1; show(); }
}

function closeOverlay() {
  $('#v-vid').pause();
  $('#viewer').hidden = true;
  $('#viewer').classList.remove('overlay');
  if (S.view === 'browse') renderGrid(true);
  if (S.view === 'export') renderExport();
}

function openOverlay(ids, pos) {
  S.queue = ids;
  S.pos = pos;
  S.mode = 'browse';
  placeViewer(true);
  show();
}

// ------------------------------------------------------------------ browse

const FILTERS = {
  all: () => true,
  unrated: (it) => !isRated(it),
  rated: (it) => isRated(it),
  star: (it) => anyStar(it),
  top: (it) => avg(it) != null && avg(it) >= 4,
  agree: (it) => allRated(it) && scoresOf(it).every((s) => s >= 4),
  disagree: (it) => { const s = scoresOf(it); return s.length > 1 && Math.max(...s) - Math.min(...s) >= 2; },
  video: (it) => it.k === 'v',
  junk: (it) => !!it.q,
  burst: (it) => !!burstOf(it),
};

function tileHtml(it, idx) {
  const badges = S.cfg.users.map((u) => {
    const r = it.r[u];
    return r && r[0] != null ? `<span class="badge">${esc(u.slice(0, 1))} ${r[0]}</span>` : '';
  }).join('') + (anyStar(it) ? '<span class="badge star">★</span>' : '');
  const corner = it.k === 'v' ? `<span class="corner badge">▶ ${fmtDur(it.d)}</span>`
    : it.q ? `<span class="corner badge flag" title="${esc(flagText(it))}">⚠</span>` : '';
  return `<button class="tile" data-i="${idx}" title="${esc(it.s)}"><img loading="lazy" src="/thumb/${it.id}" alt="">${corner}<span class="badges">${badges}</span></button>`;
}

function renderGrid(keepCount) {
  const sender = $('#browse-sender').value;
  const filter = FILTERS[$('#browse-filter').value] || FILTERS.all;
  const sort = $('#browse-sort').value;
  const list = S.items.filter((it) => (!sender || it.s === sender) && filter(it));
  if (sort === 'score') {
    const key = (it) => (avg(it) == null ? -1 : avg(it)) + (anyStar(it) ? 10 : 0);
    list.sort((a, b) => key(b) - key(a) || a.id - b.id);
  } else if (sort === 'time') {
    list.sort((a, b) => (a.t || '').localeCompare(b.t || '') || a.id - b.id);
  } else if (sort === 'blur') {
    const key = (it) => (it.sh == null ? Infinity : it.sh);
    list.sort((a, b) => key(a) - key(b) || a.id - b.id);
  }
  const rejectable = $('#browse-filter').value === 'junk' ? list.filter((it) => !isRated(it)).length : 0;
  $('#bulk-reject').hidden = !rejectable;
  $('#bulk-reject').textContent = `Rate the ${rejectable} I haven't rated as 1`;
  S.gridList = list;
  S.gridShown = Math.min(list.length, keepCount ? Math.max(S.gridShown, GRID_PAGE) : GRID_PAGE);
  $('#browse-count').textContent = `${list.length} item${list.length === 1 ? '' : 's'}`;
  $('#grid').innerHTML = list.slice(0, S.gridShown).map(tileHtml).join('');
}

function gridMore() {
  if (S.view !== 'browse' || S.gridShown >= S.gridList.length) return;
  const from = S.gridShown;
  S.gridShown = Math.min(S.gridList.length, from + GRID_PAGE);
  $('#grid').insertAdjacentHTML('beforeend', S.gridList.slice(from, S.gridShown).map((it, i) => tileHtml(it, from + i)).join(''));
}

// ------------------------------------------------------------------ results / export

function selection() {
  const min = Number($('#ex-min').value);
  const both = $('#ex-both').checked;
  const stars = $('#ex-star').checked;
  return S.items.filter((it) => (stars && anyStar(it)) || (avg(it) != null && avg(it) >= min && (!both || allRated(it))));
}

function renderExport() {
  const total = S.items.length;
  const cards = S.cfg.users.map((u) => {
    const dist = [0, 0, 0, 0, 0, 0];
    let stars = 0;
    for (const it of S.items) {
      const r = it.r[u];
      if (r && r[0] != null) dist[r[0]] += 1;
      if (r && r[1]) stars += 1;
    }
    const rated = dist.reduce((a, b) => a + b, 0);
    const max = Math.max(1, ...dist);
    const rows = [5, 4, 3, 2, 1].map((s) =>
      `<span>${s}</span><span><div class="bar" style="width:${(100 * dist[s]) / max}%"></div></span><span>${dist[s]}</span>`).join('');
    return `<div class="card"><h3>${esc(u)}</h3><div class="num">${rated} <span class="muted">/ ${total}</span></div>
      <div class="muted">${total - rated} left · ${stars} super-starred</div><div class="dist">${rows}</div></div>`;
  });
  const both = S.items.filter(allRated).length;
  const agree = S.items.filter(FILTERS.agree).length;
  const disagree = S.items.filter(FILTERS.disagree).length;
  cards.push(`<div class="card"><h3>Together</h3><div class="num">${both} <span class="muted">/ ${total}</span></div>
    <div class="muted">rated by everyone</div>
    <div class="dist"><span>Both 4+</span><span></span><span>${agree}</span><span>Disagree by 2+</span><span></span><span>${disagree}</span>
    <span>Super-starred</span><span></span><span>${S.items.filter(anyStar).length}</span></div></div>`);
  $('#stats').innerHTML = cards.join('');

  const sel = selection();
  const vids = sel.filter((it) => it.k === 'v').length;
  const sent = sel.filter((it) => it.x).length;
  $('#ex-summary').textContent = `${sel.length} selected: ${sel.length - vids} photos, ${vids} videos` + (sent ? ` (${sent} already exported)` : '');
  $('#ex-immich').disabled = !S.cfg.immich || !sel.length;
  $('#ex-folder').hidden = !S.cfg.export_dir;
  $('#ex-folder').disabled = !sel.length;
  $('#ex-preview').disabled = !sel.length;
  $('#ex-hint').textContent = S.cfg.immich
    ? `Uploads the originals straight from the NAS into the Immich album “${S.cfg.immich_album}”, with the average score as the star rating and super-stars as favourites. Safe to run again: nothing is uploaded twice.`
    : 'Immich is not connected yet: set IMMICH_URL and IMMICH_API_KEY in the stack's .env to enable this button.';

  const st = S.status;
  const bits = [`${total} photos and videos ready to rate`];
  if (st.pending) bits.push(`${st.pending} still being prepared`);
  if (st.duplicates) bits.push(`${st.duplicates} exact duplicates hidden`);
  if (st.live) bits.push(`${st.live} Live Photo clips hidden`);
  const junk = S.items.filter((it) => it.q).length;
  if (junk) bits.push(`${junk} flagged as possibly junk`);
  if (S.bursts.size) bits.push(`${S.bursts.size} bursts`);
  if (st.raw_ignored) bits.push(`${st.raw_ignored} RAW files ignored`);
  if (st.missing) bits.push(`${st.missing} no longer on disk`);
  if (st.errors) bits.push(`${st.errors} could not be read`);
  $('#lib-text').textContent = bits.join(' · ') + '.';
  $('#show-errors').hidden = !st.errors;
}

async function startExport(target) {
  const ids = selection().map((it) => it.id);
  const label = target === 'immich' ? 'Send to Immich' : 'Copy to the export folder';
  if (!confirm(`${label}: ${ids.length} items?`)) return;
  try {
    if (target === 'immich') await api('/api/immich/check');
    await api('/api/export', { target, ids });
    pollExport();
  } catch (e) {
    toast(e.message);
  }
}

async function pollExport() {
  let job;
  try { job = await api('/api/export/status'); } catch (e) { return; }
  if (!job.target) return;
  $('#ex-job').hidden = false;
  $('#ex-job-fill').style.width = job.total ? `${(100 * job.done) / job.total}%` : '0';
  const verb = job.target === 'immich' ? 'uploaded' : 'copied';
  $('#ex-job-text').textContent = job.running
    ? `Working… ${job.done} / ${job.total}`
    : `Finished: ${job.uploaded} ${verb}, ${job.skipped} already there, ${job.errors.length} failed. ${job.message}`;
  $('#ex-job-errors').innerHTML = job.errors.slice(0, 50).map((e) => `<li>${esc(e)}</li>`).join('');
  if (job.running) {
    setTimeout(pollExport, 1000);
  } else {
    await loadMedia();
    if (S.view === 'export') renderExport();
  }
}

// ------------------------------------------------------------------ navigation

function setView(view) {
  S.view = view;
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.view === view));
  for (const v of ['sort', 'browse', 'export']) $(`#view-${v}`).hidden = v !== view;
  $('#v-vid').pause();
  if (view === 'sort') { fillSenders(); buildQueue(); }
  else {
    $('#viewer').hidden = true;
    if (view === 'browse') renderGrid(false);
    if (view === 'export') { renderExport(); pollExport(); }
  }
}

function pickUser() {
  $('#picker-users').innerHTML = S.cfg.users.map((u) => `<button data-user="${esc(u)}">${esc(u)}</button>`).join('');
  $('#picker').hidden = false;
}

function chooseUser(u) {
  S.user = u;
  store('triage-user', u);
  $('#picker').hidden = true;
  updateChrome();
  fillSenders();
  setView(S.view);
}

let ticks = 0;
async function pollStatus() {
  ticks += 1;
  const active = S.status.phase && S.status.phase !== 'idle';
  if (!active && !S.status.pending && ticks % 15) return;
  try {
    S.status = await api('/api/status');
    if (S.status.ready !== S.items.length) {
      // Don't shuffle things around under someone who is looking at a photo.
      const viewing = !$('#viewer').hidden && current();
      if (!viewing) {
        await loadMedia();
        if (S.view === 'sort') buildQueue();
        if (S.view === 'browse') renderGrid(true);
      }
    }
    updateChrome();
    if (S.view === 'export') renderExport();
  } catch (e) { /* NAS busy or asleep: try again next tick */ }
}

function wire() {
  $('#tabs').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setView(b.dataset.view); });
  $('#who').addEventListener('click', pickUser);
  $('#picker-users').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) chooseUser(b.dataset.user); });

  $('#sort-sender').addEventListener('change', buildQueue);
  $('#sort-order').addEventListener('change', buildQueue);
  $('#scores').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.id === 'v-star') toggleStar(); else rate(Number(b.dataset.score));
  });
  $('#v-back').addEventListener('click', back);
  $('#v-next').addEventListener('click', next);
  $('#v-close').addEventListener('click', closeOverlay);
  $('#v-empty').addEventListener('click', async (e) => {
    const id = e.target.id;
    if (id === 'e-results') setView('export');
    if (id === 'e-all') { $('#sort-sender').value = ''; buildQueue(); }
    if (id === 'e-reload') { await loadMedia(); buildQueue(); }
  });

  let touch = null;
  const stage = $('#stage');
  stage.addEventListener('touchstart', (e) => {
    touch = e.target.tagName === 'VIDEO' ? null : { x: e.touches[0].clientX, y: e.touches[0].clientY };
  }, { passive: true });
  stage.addEventListener('touchend', (e) => {
    if (!touch) return;
    const dx = e.changedTouches[0].clientX - touch.x, dy = e.changedTouches[0].clientY - touch.y;
    touch = null;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) { if (dx < 0) next(); else back(); }
  }, { passive: true });

  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (!$('#picker').hidden || $('#viewer').hidden) return;
    if (/^(SELECT|INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
    const k = e.key;
    if (k >= '1' && k <= '5') rate(Number(k));
    else if (k === 's' || k === 'S' || k === '*') toggleStar();
    else if (k === 'b' || k === 'B') bestOfBurst();
    else if (k === 'ArrowRight') next();
    else if (k === 'ArrowLeft' || k === 'z' || k === 'Z') back();
    else if (k === '0' || k === 'Backspace') clearRating();
    else if (k === 'Escape' && S.mode === 'browse') closeOverlay();
    else if (k === ' ') { const v = $('#v-vid'); if (!v.hidden) { if (v.paused) v.play(); else v.pause(); } }
    else return;
    e.preventDefault();
  });

  for (const id of ['#browse-sender', '#browse-filter', '#browse-sort']) $(id).addEventListener('change', () => renderGrid(false));
  $('#bulk-reject').addEventListener('click', async () => {
    const todo = S.gridList.filter((it) => !isRated(it));
    if (!todo.length || !confirm(`Give a 1 to these ${todo.length} photos? Have a scroll through first: anything you rescue with a real rating is left alone.`)) return;
    await rateMany(todo, 1);
    renderGrid(true);
  });
  $('#v-burst').addEventListener('click', (e) => {
    if (e.target.closest('#v-best')) { bestOfBurst(); return; }
    const b = e.target.closest('.bthumb');
    if (b) jumpTo(Number(b.dataset.id));
  });
  $('#grid').addEventListener('click', (e) => {
    const t = e.target.closest('.tile');
    if (t) openOverlay(S.gridList.map((it) => it.id), Number(t.dataset.i));
  });
  new IntersectionObserver((entries) => { if (entries[0].isIntersecting) gridMore(); },
    { root: $('#view-browse'), rootMargin: '600px' }).observe($('#grid-more'));

  for (const id of ['#ex-min', '#ex-both', '#ex-star']) $(id).addEventListener('change', renderExport);
  $('#ex-immich').addEventListener('click', () => startExport('immich'));
  $('#ex-folder').addEventListener('click', () => startExport('folder'));
  $('#ex-preview').addEventListener('click', () => openOverlay(selection().map((it) => it.id), 0));
  $('#rescan').addEventListener('click', async () => {
    try { await api('/api/rescan', {}); S.status = await api('/api/status'); updateChrome(); } catch (e) { toast(e.message); }
  });
  $('#show-errors').addEventListener('click', async () => {
    try {
      const rows = await api('/api/errors');
      $('#lib-errors').innerHTML = rows.map((r) => `<li>${esc(r.relpath)}: ${esc(r.error)}</li>`).join('');
    } catch (e) { toast(e.message); }
  });
}

async function main() {
  wire();
  try {
    S.cfg = await api('/api/config');
    await loadMedia();
  } catch (e) {
    toast(`Could not reach the server: ${e.message}`);
    return;
  }
  setInterval(pollStatus, 4000);
  const saved = store('triage-user');
  if (saved && S.cfg.users.includes(saved)) chooseUser(saved); else pickUser();
}

main();
