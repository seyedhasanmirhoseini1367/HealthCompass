var HC_PAGE_ai_insights__seizure_analysis = JSON.parse(document.getElementById('hc-page-data-ai_insights__seizure_analysis').textContent);
const fileInput  = document.getElementById('fileInput');
const dropZone   = document.getElementById('dropZone');
const runBtn     = document.getElementById('runBtn');
const fileSelected = document.getElementById('fileSelected');
const fileName   = document.getElementById('fileName');

fileInput.addEventListener('change', () => {
  if (fileInput.files.length) {
    fileName.textContent = fileInput.files[0].name;
    fileSelected.style.display = 'flex';
    runBtn.disabled = false;
  } else {
    fileSelected.style.display = 'none';
    runBtn.disabled = true;
  }
});

['dragover','dragleave','drop'].forEach(ev => {
  dropZone.addEventListener(ev, e => {
    e.preventDefault();
    dropZone.classList.toggle('dragover', ev === 'dragover');
    if (ev === 'drop') {
      fileInput.files = e.dataTransfer.files;
      fileInput.dispatchEvent(new Event('change'));
    }
  });
});

document.getElementById('uploadForm').addEventListener('submit', async function(e) {
  e.preventDefault();
  clearError();
  showProgress();

  let pct = 5;
  const progressBar = document.getElementById('progressBar');
  const progressMsg = document.getElementById('progressMsg');
  progressBar.style.width = pct + '%';

  const messages = [
    [15, 'Uploading signal file…'],
    [35, 'Preprocessing EEG channels…'],
    [55, 'Running CNN and LSTM models…'],
    [70, 'Running LSTM and Transformer models…'],
    [85, 'Computing ensemble vote…'],
    [95, 'Finalising results…'],
  ];
  let mi = 0;
  const ticker = setInterval(() => {
    if (mi < messages.length) {
      pct = messages[mi][0];
      progressMsg.textContent = messages[mi][1];
      progressBar.style.width = pct + '%';
      mi++;
    }
  }, 1800);

  const fd = new FormData(this);
  try {
    const resp = await fetch(HC_PAGE_ai_insights__seizure_analysis.k1, {
      method: 'POST',
      body: fd,
      headers: {'X-CSRFToken': fd.get('csrfmiddlewaretoken')},
    });
    const data = await resp.json();
    clearInterval(ticker);
    progressBar.style.width = '100%';
    progressMsg.textContent = 'Complete!';

    await new Promise(r => setTimeout(r, 400));
    hideProgress();

    if (!data.success) {
      showError(data.error || 'Analysis failed. Please try again.');
      return;
    }
    renderResults(data);
  } catch (err) {
    clearInterval(ticker);
    hideProgress();
    showError('Network error: ' + err.message);
  }
});

function showProgress() {
  document.getElementById('progressWrap').style.display = 'block';
  document.getElementById('progressBar').style.width = '5%';
  runBtn.disabled = true;
  document.getElementById('resultsSection').classList.remove('show');
}
function hideProgress() {
  document.getElementById('progressWrap').style.display = 'none';
  runBtn.disabled = false;
}
function showError(msg) {
  const b = document.getElementById('errorBanner');
  document.getElementById('errorMsg').textContent = msg;
  b.classList.add('show');
  b.scrollIntoView({behavior:'smooth', block:'center'});
}
function clearError() {
  document.getElementById('errorBanner').classList.remove('show');
}

function renderResults(data) {
  // Ensemble
  document.getElementById('ensembleLabel').textContent = data.ensemble_label || '—';

  const votesRow = document.getElementById('votesRow');
  votesRow.innerHTML = '';
  if (data.ensemble_votes) {
    for (const [label, count] of Object.entries(data.ensemble_votes)) {
      const chip = document.createElement('span');
      chip.className = 'vote-chip' + (label === data.ensemble_label ? ' winner' : '');
      chip.textContent = `${label}: ${count}`;
      votesRow.appendChild(chip);
    }
  }

  // Per-model cards
  const grid = document.getElementById('modelsGrid');
  grid.innerHTML = '';
  (data.results || []).forEach(r => {
    grid.appendChild(buildCard(r));
  });

  // AI interpretation
  const interpSection = document.getElementById('aiInterpSection');
  const interpText    = document.getElementById('aiInterpText');
  if (data.ai_interpretation) {
    interpText.textContent = data.ai_interpretation;
    interpSection.style.display = 'block';
  } else {
    interpSection.style.display = 'none';
  }

  const section = document.getElementById('resultsSection');
  section.classList.add('show');
  section.scrollIntoView({behavior:'smooth', block:'start'});
}

function buildCard(r) {
  const div = document.createElement('div');
  div.className = 'model-result-card';

  if (!r.success) {
    div.innerHTML = `
      <div class="mrc-top">
        <div>
          <div class="mrc-title">${esc(r.project_title || r.project_id)}</div>
          <div class="mrc-variant">${esc(r.model_variant || '')}</div>
        </div>
      </div>
      <div class="mrc-error">${hcIcon('exclamation-circle')} ${esc(r.error || 'Model failed')}</div>`;
    return div;
  }

  const conf     = r.confidence != null ? Math.round(r.confidence * 100) : null;
  const confClass = conf == null ? '' : conf >= 70 ? 'conf-high' : conf >= 45 ? 'conf-med' : 'conf-low';
  const isSeizure = (r.prediction_label || '').toLowerCase().includes('seizure')
                 || (r.prediction_label || '').toLowerCase().includes('ictal');
  const icon = isSeizure ? '⚡' : '✅';

  div.innerHTML = `
    <div class="mrc-top">
      <div>
        <div class="mrc-title">${esc(r.project_title || r.project_id)}</div>
        <div class="mrc-variant">${esc(r.model_variant || '')}</div>
      </div>
      ${conf != null ? `<span class="conf-badge ${confClass}">${conf}%</span>` : ''}
    </div>
    <div class="pred-row">
      <span class="pred-icon">${icon}</span>
      <div>
        <div class="pred-label">${esc(r.prediction_label || String(r.prediction))}</div>
        <div class="pred-class">Class ${esc(r.prediction)}</div>
      </div>
    </div>
    ${conf != null ? `<div class="conf-bar-outer"><div class="conf-bar-inner" style="width:${conf}%"></div></div>` : ''}
    ${r.inference_ms != null ? `<div class="infer-ms">${hcIcon('clock')} ${Math.round(r.inference_ms)} ms</div>` : ''}`;
  return div;
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
