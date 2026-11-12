var HC_PAGE_ai_insights__seizure_realtime = JSON.parse(document.getElementById('hc-page-data-ai_insights__seizure_realtime').textContent);
// ── Constants ────────────────────────────────────────────────────────────────
const DISPLAY_SAMP = 2000;   // 10 s at 200 Hz
const MAX_CH       = 19;
let   CH_HEIGHT    = 40;
const COLORS = [
  '#10b981','#818cf8','#f59e0b','#34d399','#f472b6',
  '#60a5fa','#a78bfa','#fb923c','#2dd4bf','#e879f9',
  '#f43f5e','#06b6d4','#84cc16','#d946ef','#0ea5e9',
  '#fbbf24','#a3e635','#38bdf8','#c084fc',
];
const VARIANT_NAMES = {
  cnn_transformer: 'CNN Transformer',
  gru_attention:   'LSTM + Attention',
  fusion:          'Fusion (CNN + LSTM)',
};

// ── State ────────────────────────────────────────────────────────────────────
let eegData = null, channels = [], currentSample = 0;
let playing = false, rafId = null, rafLastTs = null, lastPredSamp = 0;
let history = [], loadingFile = false;

const canvas = document.getElementById('eegCanvas');
const ctx    = canvas.getContext('2d');
const csrf   = HC_PAGE_ai_insights__seizure_realtime.k1;

// ── Load models into dropdown ─────────────────────────────────────────────────
(async () => {
  try {
    const r = await fetch(HC_PAGE_ai_insights__seizure_realtime.k2);
    const d = await r.json();
    const sel = document.getElementById('modelSelect');
    sel.innerHTML = '<option value="ensemble" selected>Ensemble (majority vote)</option>';
    (d.models || []).forEach(m => {
      if (m.variant === 'ensemble') return;
      const o = document.createElement('option');
      o.value = m.variant; o.textContent = m.title;
      sel.appendChild(o);
    });
  } catch(e) {}
})();

// ── Auto-load demo ────────────────────────────────────────────────────────────
window.addEventListener('load', () => setTimeout(loadSample, 400));

// ── File input ────────────────────────────────────────────────────────────────
document.getElementById('fileInput').addEventListener('change', async function() {
  if (this.files.length) await loadFiles(Array.from(this.files));
});

async function loadSample() {
  if (loadingFile) return;
  setStatus('Loading demo EEG recording…');
  try {
    const resp = await fetch('/static/samples/eeg_sample.parquet');
    if (!resp.ok) throw new Error('Sample file not available (HTTP ' + resp.status + ')');
    const blob = await resp.blob();
    await loadFiles([new File([blob], 'eeg_sample.parquet', {type: 'application/octet-stream'})]);
  } catch(e) { setStatus('Error: ' + e.message); }
}

async function loadFiles(files) {
  if (loadingFile) return;
  loadingFile = true;
  document.getElementById('loadSampleBtn').disabled = true;
  document.getElementById('playBtn').disabled = true;
  setStatus('Parsing EEG data…');
  resetPlayback(false);

  const fd = new FormData();
  files.forEach(f => fd.append('files', f));
  try {
    const resp = await fetch(HC_PAGE_ai_insights__seizure_realtime.k3, {
      method: 'POST', body: fd, headers: {'X-CSRFToken': csrf},
    });
    if (!resp.ok) { const e = await resp.json().catch(()=>({})); throw new Error(e.error||'Server error '+resp.status); }
    eegData = await resp.json();
    if (!eegData.columns || !eegData.data) throw new Error('Unexpected response format.');

    channels = eegData.columns.slice(0, MAX_CH);
    document.getElementById('uploadLabel').textContent = files[0].name;
    document.getElementById('canvasPlaceholder').style.display = 'none';

    const dur = eegData.duration_sec ? eegData.duration_sec.toFixed(1)+' s' : eegData.total_samples+' samples';
    setStatus(`Loaded — ${dur} · ${channels.length} channels`);
    resizeCanvas(); renderFrame(0);
    document.getElementById('playBtn').disabled = false;
    document.getElementById('stopBtn').disabled = false;
    setTimeout(startPlayback, 300);
  } catch(e) {
    setStatus('Failed: ' + e.message);
  } finally {
    loadingFile = false;
    document.getElementById('loadSampleBtn').disabled = false;
  }
}

// ── Playback ──────────────────────────────────────────────────────────────────
function togglePlay() { playing ? pausePlayback() : startPlayback(); }

function startPlayback() {
  if (!eegData) return;
  playing = true; rafLastTs = null;
  document.querySelector('#playIcon use').setAttribute('href', document.body.dataset.icons + '#i-pause');
  rafId = requestAnimationFrame(tick);
}

function pausePlayback() {
  playing = false;
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
  document.querySelector('#playIcon use').setAttribute('href', document.body.dataset.icons + '#i-play');
}

function resetPlayback(fullReset = true) {
  pausePlayback();
  currentSample = 0; lastPredSamp = 0; history = [];
  document.getElementById('histList').innerHTML = '<div class="hist-empty" id="histEmpty">No predictions yet</div>';
  document.getElementById('histCount').textContent = '0';
  document.getElementById('timeDisp').textContent = '— / — s';
  document.getElementById('canvasTimeDisp').textContent = '';
  resetClassification();
  if (eegData && fullReset) { renderFrame(0); setStatus('Ready'); }
}

function tick(ts) {
  if (!playing) return;
  if (rafLastTs === null) rafLastTs = ts;
  const dt = Math.min(ts - rafLastTs, 80);
  rafLastTs = ts;
  const speed = parseFloat(document.getElementById('speedSelect')?.value || '0.5');
  const sr = eegData.sampling_rate || 200;
  const add = Math.round(dt * sr / 1000 * speed);
  currentSample = Math.min(currentSample + add, eegData.total_samples);
  renderFrame(currentSample); updateProgress();
  if (currentSample >= DISPLAY_SAMP) {
    const stride = parseInt(document.getElementById('strideSelect').value) * sr;
    if (currentSample - lastPredSamp >= stride) { lastPredSamp = currentSample; runPrediction(currentSample); }
  }
  if (currentSample >= eegData.total_samples) { pausePlayback(); setStatus('Playback complete'); return; }
  rafId = requestAnimationFrame(tick);
}

function updateProgress() {
  if (!eegData) return;
  const sr = eegData.sampling_rate || 200;
  const cur = (currentSample / sr).toFixed(1);
  const tot = (eegData.total_samples / sr).toFixed(1);
  document.getElementById('timeDisp').textContent = `${cur} s / ${tot} s`;
  document.getElementById('canvasTimeDisp').textContent = `${cur} s`;
}

// ── Canvas ────────────────────────────────────────────────────────────────────
function resizeCanvas() {
  const body = canvas.parentElement;
  const w = body.offsetWidth, h = body.offsetHeight;
  if (!w || !h) return;
  CH_HEIGHT = Math.max(30, Math.floor(h / (channels.length || 1)));
  canvas.width  = w;
  canvas.height = CH_HEIGHT * channels.length;
}

function renderFrame(endSample) {
  if (!eegData || !channels.length) return;
  const W = canvas.width, H = canvas.height, chH = CH_HEIGHT;
  const sr = eegData.sampling_rate || 200;

  ctx.fillStyle = '#0f172a';
  ctx.fillRect(0, 0, W, H);

  // Gridlines
  ctx.strokeStyle = 'rgba(255,255,255,.04)'; ctx.lineWidth = 1;
  for (let s = 0; s <= DISPLAY_SAMP; s += sr) {
    const x = (s / DISPLAY_SAMP) * W;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }
  ctx.fillStyle = '#1e293b'; ctx.font = '10px system-ui';
  for (let s = sr; s < DISPLAY_SAMP; s += sr) {
    const x = (s / DISPLAY_SAMP) * W;
    ctx.fillText(`${s/sr}s`, x+3, H-3);
  }

  channels.forEach((ch, ci) => {
    const raw = eegData.data[ch]; if (!raw) return;
    const start = Math.max(0, endSample - DISPLAY_SAMP);
    const slice = raw.slice(start, endSample || DISPLAY_SAMP);
    if (!slice.length) return;

    const yCenter = (ci + 0.5) * chH;
    const sorted = slice.map(Math.abs).sort((a,b)=>a-b);
    const p95 = sorted[Math.floor(sorted.length * 0.95)] || 1;
    const scale = p95 > 0 ? (chH * 0.44) / p95 : chH * 0.44;

    if (ci > 0) {
      ctx.strokeStyle = 'rgba(255,255,255,.04)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, ci*chH); ctx.lineTo(W, ci*chH); ctx.stroke();
    }

    ctx.beginPath();
    ctx.strokeStyle = COLORS[ci % COLORS.length];
    ctx.lineWidth = 1.1; ctx.globalAlpha = .88;
    const step = Math.max(1, Math.floor(slice.length / W));
    for (let i = 0; i < slice.length; i += step) {
      const x = (i / DISPLAY_SAMP) * W;
      const y = yCenter - slice[i] * scale;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke(); ctx.globalAlpha = 1;

    // Channel label
    ctx.fillStyle = COLORS[ci % COLORS.length];
    ctx.globalAlpha = .7; ctx.font = 'bold 9px system-ui';
    ctx.fillText(ch, 5, ci * chH + 11);
    ctx.globalAlpha = 1;
  });
}

// ── Prediction ────────────────────────────────────────────────────────────────
async function runPrediction(upTo) {
  const model = document.getElementById('modelSelect').value;
  const chunk = {};
  const start = Math.max(0, upTo - DISPLAY_SAMP);
  channels.forEach(ch => { chunk[ch] = eegData.data[ch]?.slice(start, upTo) || []; });
  try {
    const resp = await fetch(HC_PAGE_ai_insights__seizure_realtime.k4, {
      method: 'POST',
      body: JSON.stringify({data: chunk, model_variant: model}),
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
    });
    if (!resp.ok) {
      let msg = 'HTTP ' + resp.status;
      try { const b = await resp.json(); if (b.error) msg = b.error; } catch {}
      throw new Error(msg);
    }
    const d = await resp.json();
    if (d.error) throw new Error(d.error);
    updateClassification(d);
    addHistory(d.label, d.confidence);
  } catch(e) {
    showPredError(e.message);
  }
}

function labelType(label) {
  const l = (label || '').toLowerCase();
  if (l.includes('seizure') || l.includes('ictal')) return 'seizure';
  if (l.includes('lpd') || l.includes('discharge')) return 'lpd';
  return 'normal';
}

function shortLabel(label) {
  if (!label) return '?';
  if (label.toLowerCase().includes('lpd')) return 'LPD';
  return label.split(' ')[0];
}

function updateClassification(d) {
  const label = d.label || '?';
  const conf  = d.confidence ?? null;
  const type  = labelType(label);
  const perModel = d.per_model || [];

  const card = document.getElementById('clsCard');
  card.className = 'cls-card ' + type;
  document.getElementById('clsMainLbl').textContent = shortLabel(label);
  document.getElementById('clsWaitTxt').style.display = 'none';

  if (conf !== null) {
    document.getElementById('clsWinner').textContent = shortLabel(label);
    document.getElementById('clsConf').textContent = Math.round(conf * 100) + '%';
    document.getElementById('clsMetaWrap').style.display = 'block';
  }

  // Latency
  const totalMs = perModel.filter(r => r.success).reduce((s, r) => s + (r.inference_ms || 0), 0);
  if (totalMs > 0)
    document.getElementById('clsLatency').textContent = `Latency: ${totalMs} ms (all models)`;

  // Vote badge
  if (d.variant === 'ensemble') {
    document.getElementById('voteBadge').style.display = 'block';
  }

  // Per-model list
  const pmSec  = document.getElementById('pmSection');
  const pmList = document.getElementById('pmList');
  if (perModel.filter(r => r.success).length > 0) {
    pmList.innerHTML = '';
    perModel.forEach(m => {
      if (!m.success) return;
      const mType = labelType(m.label);
      const row = document.createElement('div');
      row.className = 'pm-row';
      const mConf = m.confidence ? Math.round(m.confidence * 100) + '%' : '';
      row.innerHTML = `<span class="pm-name">${esc(VARIANT_NAMES[m.variant] || m.variant)}</span>
        <span class="pm-badge ${mType}">${esc(shortLabel(m.label))}</span>
        <span class="pm-conf">${mConf}</span>`;
      pmList.appendChild(row);
    });
    pmSec.style.display = 'block';
  }
}

function showPredError(msg) {
  const card = document.getElementById('clsCard');
  card.className = 'cls-card waiting';
  document.getElementById('clsMainLbl').textContent = '⚠ Error';
  document.getElementById('clsWaitTxt').style.display = 'block';
  document.getElementById('clsWaitTxt').textContent = msg;
  document.getElementById('clsMetaWrap').style.display = 'none';
}

function resetClassification() {
  document.getElementById('clsCard').className = 'cls-card waiting';
  document.getElementById('clsMainLbl').textContent = 'Waiting…';
  document.getElementById('clsWaitTxt').style.display = 'block';
  document.getElementById('clsWaitTxt').textContent = 'Prediction appears after 10 s of playback';
  document.getElementById('clsMetaWrap').style.display = 'none';
  document.getElementById('pmSection').style.display = 'none';
  document.getElementById('voteBadge').style.display = 'none';
}

function addHistory(label, conf) {
  history.unshift({label, conf});
  if (history.length > 40) history.pop();
  const list = document.getElementById('histList');
  const empty = document.getElementById('histEmpty');
  if (empty) empty.remove();
  document.getElementById('histCount').textContent = history.length;
  const type = labelType(label);
  const row = document.createElement('div');
  row.className = 'hist-row ' + (type === 'seizure' ? 's' : type === 'lpd' ? 'l' : 'n');
  const icon = type === 'seizure' ? '⚡' : type === 'lpd' ? '⚠' : '✅';
  const pct = conf !== null ? Math.round(conf * 100) + '%' : '';
  row.innerHTML = `<span>${icon}</span>
    <span class="hist-lbl">${esc(shortLabel(label))}</span>
    <span class="hist-conf">${pct}</span>`;
  list.insertBefore(row, list.firstChild);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function setStatus(msg) { document.getElementById('statusText').textContent = msg; }
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

window.addEventListener('resize', () => {
  if (eegData) { resizeCanvas(); renderFrame(currentSample); }
});
