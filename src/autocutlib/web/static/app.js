// autocut web UI. Plain JS, no build step. Talks to server.py's JSON API.

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, '');
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid);
  return e;
};

async function api(path, { method = 'GET', body, query } = {}) {
  const url = query ? `${path}?${new URLSearchParams(query)}` : path;
  const res = await fetch(url, {
    method,
    headers: { 'X-Autocut': '1', ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

const tc = (s) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${(s % 60).toFixed(2).padStart(5, '0')}`;
const pct = (r) => `${Math.round(r * 100)}%`;
const size = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(0)} MB` : `${Math.ceil(b / 1e3)} KB`);
// Paths may be POSIX (/Users/me/a.mov) or Windows (C:\Users\me\a.mov).
const basename = (p) => p.split(/[\\/]/).pop();
const dirname = (p) => {
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return i > 0 ? p.slice(0, i) : (p[0] === '/' ? '/' : p);
};
const joinPath = (dir, name) => (dir.includes('\\') && !dir.includes('/') ? `${dir.replace(/\\$/, '')}\\${name}` : `${dir.replace(/\/$/, '')}/${name}`);

// ---------------------------------------------------------------- state

const STORE_KEY = 'autocut.v1';
let CONFIG = {};
const S = {
  script: '', pages: '', parsed: null,
  takes: {},            // CHARACTER -> [{ path, info?, error? }]
  options: {}, name: '', preview: true, convertWhere: 'next',
  voices: {},           // CHARACTER -> ElevenLabs voice name/ID ('' = original voice)
};

function save() {
  try {
    const takes = Object.fromEntries(Object.entries(S.takes).map(([c, ts]) => [c, ts.map((t) => t.path)]));
    localStorage.setItem(STORE_KEY, JSON.stringify({
      script: S.script, pages: S.pages, takes, options: S.options, name: S.name,
      preview: S.preview, convertWhere: S.convertWhere, lastDir: S.lastDir, voices: S.voices,
    }));
  } catch { /* private mode etc. */ }
}

function restore() {
  try {
    const d = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
    Object.assign(S, d, { takes: {} });
    for (const [c, ps] of Object.entries(d.takes || {})) S.takes[c] = ps.map((path) => ({ path }));
  } catch { /* ignore */ }
}

// ---------------------------------------------------------------- file picking

function pickerButtons(kind, multiple, onPaths) {
  const span = el('span', { class: 'pickers' });
  if (CONFIG.picker) {
    span.append(el('button', {
      type: 'button', title: 'macOS file dialog',
      onclick: async () => {
        try {
          const { paths } = await api('/api/pick', { method: 'POST', body: { kind, multiple, start: S.lastDir } });
          if (paths.length) { S.lastDir = dirname(paths[0]); onPaths(paths); }
        } catch (e) { alert(e.message); }
      },
    }, 'Choose…'));
  }
  span.append(el('button', {
    type: 'button', title: 'Browse folders on this Mac',
    onclick: async () => { const paths = await browseFiles(kind, multiple); if (paths.length) onPaths(paths); },
  }, 'Browse…'));
  span.append(el('button', {
    type: 'button', title: `Copies the file into ${CONFIG.uploads || 'the workspace'}`,
    onclick: () => uploadFile(kind, span, (p) => onPaths([p])),
  }, 'Upload…'));
  return span;
}

function uploadFile(kind, anchor, done) {
  const input = $('#upload-input');
  input.accept = kind === 'pdf' ? '.pdf,application/pdf' : 'video/*,.mpeg,.mpg,.mxf,.mts';
  input.value = '';
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    const status = el('span', { class: 'muted small' }, ` uploading ${file.name}…`);
    anchor.append(status);
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/api/upload?${new URLSearchParams({ name: file.name })}`);
    xhr.setRequestHeader('X-Autocut', '1');
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) status.textContent = ` uploading ${file.name}… ${pct(e.loaded / e.total)}`; };
    xhr.onload = () => {
      status.remove();
      if (xhr.status === 200) done(JSON.parse(xhr.responseText).path);
      else alert(`Upload failed: ${xhr.statusText}`);
    };
    xhr.onerror = () => { status.remove(); alert('Upload failed.'); };
    xhr.send(file);
  };
  input.click();
}

function browseFiles(kind, multiple) {
  const dlg = $('#browser');
  const list = $('#br-list');
  const pathInput = $('#br-path');
  let selected = new Set();
  let cur = '';

  const load = async (path) => {
    try {
      const d = await api('/api/fs', { query: { path: path || '', kind } });
      cur = d.path; pathInput.value = d.path; selected = new Set(); list.replaceChildren();
      if (d.parent) list.append(el('li', { class: 'dir', onclick: () => load(d.parent) }, '..'));
      for (const name of d.dirs) list.append(el('li', { class: 'dir', onclick: () => load(joinPath(cur, name)) }, name));
      for (const f of d.files) {
        const li = el('li', { class: 'file' }, f.name, el('span', { class: 'size' }, size(f.size)));
        const full = joinPath(cur, f.name);
        li.onclick = () => {
          if (!multiple) { selected.clear(); list.querySelectorAll('.sel').forEach((x) => x.classList.remove('sel')); }
          if (selected.has(full)) { selected.delete(full); li.classList.remove('sel'); } else { selected.add(full); li.classList.add('sel'); }
          $('#br-count').textContent = selected.size ? `${selected.size} selected` : '';
        };
        li.ondblclick = () => { selected = new Set([full]); dlg.close('ok'); };
        list.append(li);
      }
      if (!d.dirs.length && !d.files.length) list.append(el('li', { class: 'muted' }, kind === 'pdf' ? 'No PDFs here' : 'No videos here'));
      $('#br-count').textContent = '';
    } catch (e) { alert(e.message); }
  };

  return new Promise((resolve) => {
    $('#br-go').onclick = (e) => { e.preventDefault(); load(pathInput.value); };
    pathInput.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); load(pathInput.value); } };
    dlg.onclose = () => {
      if (cur) S.lastDir = cur;
      save();
      resolve(dlg.returnValue === 'ok' ? [...selected] : []);
    };
    dlg.returnValue = '';
    load(S.lastDir || (S.script ? dirname(S.script) : ''));
    dlg.showModal();
  });
}

// ---------------------------------------------------------------- 1. screenplay

async function parseScript() {
  S.script = $('#script-path').value.trim();
  S.pages = $('#pages').value.trim();
  save();
  const status = $('#script-status');
  if (!S.script) { status.textContent = 'Choose a PDF first.'; return; }
  status.textContent = 'Parsing…';
  try {
    S.parsed = await api('/api/script/parse', { method: 'POST', body: { path: S.script, pages: S.pages } });
    status.textContent = '';
    renderScript();
    renderTakes();
  } catch (e) {
    S.parsed = null;
    status.replaceChildren(el('span', { class: 'error' }, e.message));
    $('#script-result').hidden = true;
  }
}

function renderScript() {
  const p = S.parsed;
  $('#script-result').hidden = false;
  $('#char-chips').replaceChildren(
    el('span', { class: 'muted' }, `${p.lines.length} lines · characters:`),
    ...p.characters.map((c) => el('span', { class: 'chip' }, `${c.name} (${c.lines})`)),
  );
  $('#dialogue tbody').replaceChildren(...p.lines.map((ln) => el('tr', {},
    el('td', { class: 'num' }, ln.n), el('td', { class: 'num' }, ln.page),
    el('td', {}, ln.character), el('td', {}, ln.text))));
  $('#dump').textContent = p.dump;
  if (!$('#opt-name').value) $('#opt-name').placeholder = basename(S.script).replace(/\.pdf$/i, '');
}

// ---------------------------------------------------------------- 2. takes

async function addTakes(char, paths) {
  const list = (S.takes[char] ||= []);
  for (const path of paths) {
    if (list.some((t) => t.path === path)) continue;
    list.push({ path });
  }
  save();
  renderTakes();
  await Promise.all(list.filter((t) => !t.info && !t.error).map(probeTake));
}

async function probeTake(t) {
  try {
    t.info = await api('/api/probe', { method: 'POST', body: { path: t.path, convert_next_to_original: S.convertWhere === 'next' } });
    t.error = null;
  } catch (e) { t.error = e.message; }
  renderTakes();
}

function takeRow(char, t, i, list) {
  const info = t.info;
  const meta = info
    ? `${info.width}×${info.height} · ${info.fps.toFixed(3).replace(/\.?0+$/, '')} fps · ${tc(info.duration)} · ${info.vcodec}${info.acodec ? '/' + info.acodec : ', no audio'} · ${size(info.size)}`
    : t.error ? '' : 'checking…';
  const badges = [];
  if (info?.problems.length) {
    badges.push(el('span', { class: 'badge warn', title: `Resolve can't use: ${info.problems.join(', ')}.\nWill be converted to ${info.converted}` },
      info.converted_exists ? 'ProRes copy ready' : '→ ProRes'));
  }
  if (info?.cached) badges.push(el('span', { class: 'badge ok', title: 'A .words.json transcript exists; no need to re-transcribe' }, 'transcript cached'));
  const move = (d) => { const j = i + d; [list[i], list[j]] = [list[j], list[i]]; save(); renderTakes(); };
  return el('div', { class: 'take' },
    el('div', { class: 'take-order' }, el('span', { class: i === 0 ? 'badge main' : 'badge' }, i === 0 ? 'Main' : `Alt ${i + 1}`)),
    el('div', {},
      el('div', {}, el('span', { class: 'name' }, basename(t.path)), ...badges),
      el('div', { class: 'meta' }, meta),
      t.error ? el('div', { class: 'error' }, t.error) : null,
      el('div', { class: 'path' }, dirname(t.path))),
    el('div', {},
      el('button', { class: 'icon', title: 'Move up (make main)', disabled: i === 0, onclick: () => move(-1) }, '↑'),
      el('button', { class: 'icon', title: 'Move down', disabled: i === list.length - 1, onclick: () => move(1) }, '↓'),
      el('button', { class: 'icon', title: 'Remove', onclick: () => { list.splice(i, 1); if (!list.length) delete S.takes[char]; save(); renderTakes(); } }, '✕')));
}

function renderTakes() {
  const box = $('#takes');
  if (!S.parsed) { box.replaceChildren(el('p', { class: 'muted' }, 'Parse the screenplay first.')); return; }
  const speaking = S.parsed.characters.map((c) => c.name);
  const extra = Object.keys(S.takes).filter((c) => !speaking.includes(c));
  box.replaceChildren(
    ...S.parsed.characters.map((c) => charCard(c.name, c.lines)),
    ...extra.map((c) => charCard(c, 0)),
  );
}

function charCard(char, nLines) {
  const list = S.takes[char] || [];
  const pathInput = el('input', { class: 'path', placeholder: 'or paste a path and press Enter', spellcheck: 'false' });
  pathInput.onkeydown = (e) => {
    if (e.key === 'Enter' && pathInput.value.trim()) addTakes(char, [pathInput.value.trim().replace(/^'(.*)'$/, '$1')]);
  };
  return el('div', { class: `char${list.length ? '' : ' empty'}` },
    el('header', {}, el('h3', {}, char),
      nLines ? el('span', { class: 'muted small' }, `${nLines} line${nLines === 1 ? '' : 's'}`)
        : el('span', { class: 'badge warn' }, 'not a speaker in this script — remove these takes'),
      !list.length && nLines ? el('span', { class: 'muted small' }, '· no takes: these lines will be dropped') : null),
    ...list.map((t, i) => takeRow(char, t, i, list)),
    el('div', { class: 'row' }, pickerButtons('video', true, (paths) => addTakes(char, paths)), pathInput),
    nLines ? voiceRow(char, list) : null);
}

function voiceRow(char, list) {
  const input = el('input', { list: 'voice-list', value: S.voices?.[char] || '', placeholder: 'original voice', spellcheck: 'false', size: 22 });
  const audio = el('audio', { controls: true, hidden: true });
  const status = el('span', { class: 'muted small' });
  const btn = el('button', {
    type: 'button', title: `Converts ~8 seconds of the main take (about $${(CONFIG.voice_price * 8 / 60).toFixed(2)})`,
    disabled: !input.value.trim() || !list.length,
    onclick: async () => {
      if (!CONFIG.fal_key) { alert('Set your fal.ai API key first (Options › Voice).'); return; }
      btn.disabled = true;
      status.textContent = ' converting…';
      try {
        const r = await api('/api/voice/preview', { method: 'POST', body: { path: list[0].path, voice: input.value.trim(), denoise: S.options.voice_denoise, seed: S.options.voice_seed, stability: S.options.voice_stability } });
        status.textContent = '';
        audio.src = r.url;
        audio.hidden = false;
        audio.play().catch(() => {});
      } catch (e) {
        status.replaceChildren(el('span', { class: 'error' }, e.message));
      } finally { btn.disabled = false; }
    },
  }, '▶ Preview');
  input.oninput = () => {
    S.voices = { ...S.voices, [char]: input.value.trim() };
    btn.disabled = !input.value.trim() || !list.length;
    save();
  };
  return el('div', { class: 'row voice' },
    el('label', {}, 'Voice ', input), btn, status, audio,
    input.value.trim() ? el('span', { class: 'muted small' }, 'ElevenLabs via fal.ai') : null);
}

// ---------------------------------------------------------------- 3. options

const NUM_OPTS = ['pre', 'post', 'merge_gap', 'voice_seed'];
const BOOL_OPTS = ['no_merge', 'pick_best', 'enable_alts', 'retranscribe', 'convert', 'voice_denoise'];

function initOptions() {
  const d = CONFIG.defaults;
  const o = (S.options = { ...d, ...S.options });
  $('#models').replaceChildren(...CONFIG.models.map((m) => el('option', { value: m })));
  $('#opt-model').value = o.model;
  $('#opt-language').value = o.language;
  for (const k of NUM_OPTS) $(`#opt-${k}`).value = o[k];
  for (const k of BOOL_OPTS) $(`#opt-${k}`).checked = !!o[k];
  $('#opt-preview').checked = S.preview;
  $('#stab-on').checked = o.voice_stability != null;
  $('#stab').value = Math.round((o.voice_stability ?? 0.5) * 100);
  $('#opt-name').value = S.name || '';
  $('#conv-dir').textContent = CONFIG.converted;
  $('#voice-list').replaceChildren(...CONFIG.voices.map((v) => el('option', { value: v })));
  $('#voice-price').textContent = `$${CONFIG.voice_price.toFixed(2)}`;
  renderFalKey();
  document.querySelector(`input[name=convwhere][value=${S.convertWhere}]`).checked = true;
  const sync = () => {
    o.model = $('#opt-model').value.trim() || d.model;
    o.language = $('#opt-language').value.trim() || d.language;
    for (const k of NUM_OPTS) o[k] = parseFloat($(`#opt-${k}`).value) || 0;
    for (const k of BOOL_OPTS) o[k] = $(`#opt-${k}`).checked;
    S.preview = $('#opt-preview').checked;
    $('#stab').disabled = !$('#stab-on').checked;
    o.voice_stability = $('#stab-on').checked ? Number($('#stab').value) / 100 : null;
    $('#stab-val').textContent = $('#stab-on').checked ? `${$('#stab').value}%` : 'default (≈50%)';
    S.name = $('#opt-name').value.trim();
    const where = document.querySelector('input[name=convwhere]:checked').value;
    const whereChanged = where !== S.convertWhere;
    S.convertWhere = where;
    $('#opt-merge_gap').disabled = o.no_merge;
    $('#convert-where').hidden = !o.convert;
    save();
    if (whereChanged) Object.values(S.takes).flat().forEach(probeTake);
  };
  $('#sec-options').addEventListener('input', sync);
  $('#sec-options').addEventListener('change', sync);
  sync();
}

function renderFalKey() {
  const box = $('#fal-key');
  if (CONFIG.fal_key_from_env) { box.replaceChildren(el('span', { class: 'badge ok' }, 'fal.ai key from FAL_KEY')); return; }
  const input = el('input', { type: 'password', placeholder: 'fal.ai API key', size: 28, autocomplete: 'off' });
  const saveKey = async (value) => {
    try {
      const r = await api('/api/settings', { method: 'POST', body: { fal_key: value } });
      CONFIG.fal_key = r.fal_key;
      renderFalKey();
    } catch (e) { alert(e.message); }
  };
  box.replaceChildren(...(CONFIG.fal_key
    ? [el('span', { class: 'badge ok' }, 'fal.ai key saved'), el('button', { type: 'button', onclick: () => saveKey('') }, 'Remove')]
    : [input, el('button', { type: 'button', onclick: () => input.value.trim() && saveKey(input.value.trim()) }, 'Save key'),
      el('a', { href: 'https://fal.ai/dashboard/keys', target: '_blank', class: 'small' }, 'get a key')]));
}

// ---------------------------------------------------------------- 4. run

let source = null;
let lastReplace = null;

async function runJob() {
  if (!S.parsed) { alert('Parse the screenplay first.'); return; }
  const takes = Object.fromEntries(Object.entries(S.takes)
    .filter(([, ts]) => ts.length).map(([c, ts]) => [c, ts.map((t) => t.path)]));
  if (!Object.keys(takes).length) { alert('Add at least one take.'); return; }
  const voices = Object.fromEntries(Object.entries(S.voices || {}).filter(([c, v]) => v && takes[c]));
  if (Object.keys(voices).length && !CONFIG.fal_key) { alert('A voice is set, but there is no fal.ai API key. Add it under Options › Voice.'); return; }
  const options = { ...S.options, convert_next_to_original: S.convertWhere === 'next', voices };
  try {
    const job = await api('/api/jobs', {
      method: 'POST',
      body: { script: S.script, pages: S.pages, takes, options, preview: S.preview, name: S.name },
    });
    await loadRecent(job.id);
    follow(job.id);
  } catch (e) { alert(e.message); }
}

function follow(jobId) {
  if (source) source.close();
  currentJob = jobId;
  $('#log').replaceChildren();
  lastReplace = null;
  $('#sec-result').hidden = true;
  $('#run').disabled = true;
  $('#cancel').hidden = false;
  $('#progress-wrap').hidden = false;
  $('#log-box').open = true;
  setStatus('Running…', '');
  setStage('Starting…', null);

  source = new EventSource(`/api/jobs/${jobId}/events`);
  source.onmessage = (m) => handleEvent(JSON.parse(m.data));
  source.addEventListener('end', async (m) => {
    source.close(); source = null;
    finish(JSON.parse(m.data));
  });
}

let currentJob = null;

function handleEvent(ev) {
  switch (ev.type) {
    case 'log': appendLog(ev.text, ev.replace); break;
    case 'stage': setStage(ev.name, null); appendLog(`\n== ${ev.name}`); break;
    case 'progress': setStage(null, ev.frac, ev.detail); break;
    case 'error': appendLog(`ERROR: ${ev.message}`); break;
    case 'result': showResult(ev.result, currentJob); break;
    default: break;
  }
}

function appendLog(text, replace = false) {
  const log = $('#log');
  if (replace && lastReplace) { lastReplace.data = text + '\n'; } else {
    const node = document.createTextNode(text + '\n');
    log.append(node);
    lastReplace = replace ? node : null;
  }
  if ($('#autoscroll').checked) log.scrollTop = log.scrollHeight;
}

function setStage(name, frac, detail = '') {
  if (name != null) $('#stage').textContent = name;
  const bar = $('#bar');
  if (frac == null) { bar.classList.add('indeterminate'); bar.style.width = ''; } else {
    bar.classList.remove('indeterminate');
    bar.style.width = pct(frac);
  }
  if (detail) $('#stage').dataset.detail = detail;
}

function setStatus(text, cls) {
  const s = $('#run-status');
  s.textContent = text;
  s.className = cls;
}

async function finish(summary) {
  $('#run').disabled = false;
  $('#cancel').hidden = true;
  $('#progress-wrap').hidden = true;
  if (summary.status === 'done') {
    setStatus('Done.', 'done');
    const detail = await api(`/api/jobs/${summary.id}`);
    if (detail.result) showResult(detail.result, summary.id);
  } else if (summary.status === 'cancelled') {
    setStatus('Cancelled.', 'failed');
  } else {
    setStatus(`Failed: ${summary.error || summary.status}`, 'failed');
  }
  loadRecent(summary.id);
}

// ---------------------------------------------------------------- 5. result

function showResult(r, jobId) {
  $('#sec-result').hidden = false;
  const flagged = r.segments.filter((s) => s.flag).length;
  $('#result-summary').replaceChildren(el('p', {},
    `${r.segments.length} clips, ${tc(r.duration)} long. `,
    flagged ? el('span', { class: 'badge warn' }, `${flagged} low match${flagged === 1 ? '' : 'es'} (< ${pct(r.low_match)}) to check`)
      : el('span', { class: 'badge ok' }, 'every clip matched well')));

  const fcp = $('#dl-fcpxml');
  fcp.href = `/api/jobs/${jobId}/file/fcpxml?download=1`;
  $('#fcpxml-path').textContent = r.fcpxml;
  const reveal = $('#reveal-fcpxml');
  reveal.hidden = !CONFIG.mac;
  reveal.onclick = () => api('/api/reveal', { method: 'POST', body: { path: r.fcpxml } }).catch((e) => alert(e.message));

  const video = $('#preview');
  $('#preview-box').hidden = !r.preview;
  if (r.preview) {
    video.src = `/api/jobs/${jobId}/file/preview?t=${Date.now()}`;
    $('#dl-preview').href = `/api/jobs/${jobId}/file/preview?download=1`;
  } else {
    video.removeAttribute('src');
  }

  // Timeline position of each clip in the preview (clips are back to back).
  let at = 0;
  const rows = r.segments.map((s) => {
    const start = at;
    const len = s.primary.end - s.primary.start;
    at += len;
    const tr = el('tr', { class: s.flag ? 'flag' : '', title: r.preview ? 'Play from here' : '' },
      el('td', { class: 'num' }, s.n),
      el('td', {}, s.character),
      el('td', { class: 'num' }, s.primary.take),
      el('td', { class: 'tc' }, tc(s.primary.start)),
      el('td', { class: 'tc' }, tc(s.primary.end)),
      el('td', { class: 'tc' }, len.toFixed(2) + 's'),
      el('td', { class: 'match num' }, pct(s.primary.ratio), s.flag ? ' ⚠' : ''),
      el('td', { class: 'small' }, s.alts.map((a) => `t${a.take} ${pct(a.ratio)}`).join(', ') || '—'),
      el('td', { class: 'small' }, s.lines.join(', ')),
      el('td', {}, s.text));
    tr.onclick = () => { if (r.preview) { video.currentTime = start + 0.01; video.play(); } };
    return tr;
  });
  $('#cut tbody').replaceChildren(...rows);
  applyFlagFilter();

  $('#skipped').replaceChildren(...(r.skipped.length ? [
    el('h3', {}, `Skipped lines (${r.skipped.length})`),
    el('p', { class: 'muted small' }, "Not found in any take for that character, or that character has no take."),
    el('ul', {}, ...r.skipped.map((s) => el('li', {}, `${s.n}. ${s.character}: ${s.text}`))),
  ] : []));
  const conv = r.takes.filter((t) => t.converted);
  $('#converted').replaceChildren(...(conv.length ? [
    el('h3', {}, 'Converted files'),
    el('ul', { class: 'small' }, ...conv.map((t) => el('li', {}, el('code', {}, basename(t.source)), ' → ', el('code', {}, t.path),
      t.voice ? el('span', { class: 'badge' }, `voice: ${t.voice}`) : null))),
  ] : []));
}

function applyFlagFilter() {
  const only = $('#only-flagged').checked;
  for (const tr of $('#cut tbody').children) tr.hidden = only && !tr.classList.contains('flag');
}

// ---------------------------------------------------------------- past runs

async function loadRecent(selectId) {
  const { jobs } = await api('/api/jobs');
  const sel = $('#recent');
  sel.replaceChildren(el('option', { value: '' }, '—'), ...jobs.map((j) => el('option', { value: j.id },
    `${new Date(j.created * 1000).toLocaleString()} · ${j.name} · ${j.status}`)));
  if (selectId) sel.value = selectId;
  return jobs;
}

async function openJob(id) {
  const job = await api(`/api/jobs/${id}`);
  if (job.status === 'running' || job.status === 'starting') { follow(id); return; }
  currentJob = id;
  // Replay the stored log, then show the result.
  if (source) source.close();
  $('#log').replaceChildren();
  lastReplace = null;
  $('#sec-result').hidden = true;
  source = new EventSource(`/api/jobs/${id}/events`);
  source.onmessage = (m) => { const ev = JSON.parse(m.data); if (ev.type !== 'result') handleEvent(ev); };
  source.addEventListener('end', () => { source.close(); source = null; });
  setStatus(job.status === 'done' ? `Loaded run ${job.name}.` : `Run ${job.name}: ${job.status}${job.error ? ' — ' + job.error : ''}`,
    job.status === 'done' ? 'done' : 'failed');
  if (job.result) showResult(job.result, id);
}

// ---------------------------------------------------------------- boot

async function boot() {
  restore();
  CONFIG = await api('/api/config');

  const warn = [];
  if (!CONFIG.ffmpeg) warn.push('ffmpeg/ffprobe not found on PATH — install with: brew install ffmpeg');
  if (!CONFIG.whisperx) warn.push('WhisperX is not installed, so only takes with cached .words.json transcripts will work. See the README (pip install -e ".[whisper]").');
  $('#env-warnings').replaceChildren(...warn.map((w) => el('div', { class: 'warning' }, w)));

  $('#script-path').value = S.script || '';
  $('#pages').value = S.pages || '';
  document.querySelector('.pickers[data-target=script]').replaceWith(
    pickerButtons('pdf', false, ([p]) => { $('#script-path').value = p; parseScript(); }));
  $('#parse').onclick = parseScript;
  $('#script-path').onkeydown = (e) => { if (e.key === 'Enter') parseScript(); };
  $('#pages').onkeydown = (e) => { if (e.key === 'Enter') parseScript(); };

  initOptions();
  $('#run').onclick = runJob;
  $('#cancel').onclick = () => currentJob && api(`/api/jobs/${currentJob}/cancel`, { method: 'POST' });
  $('#only-flagged').onchange = applyFlagFilter;
  $('#recent').onchange = (e) => e.target.value && openJob(e.target.value);

  if (S.script) await parseScript();
  Object.values(S.takes).flat().forEach(probeTake);

  const jobs = await loadRecent();
  const running = jobs.find((j) => j.status === 'running' || j.status === 'starting');
  if (running) { $('#recent').value = running.id; follow(running.id); }
}

boot().catch((e) => {
  document.body.prepend(el('div', { class: 'warning' }, `Couldn't start: ${e.message}`));
});
