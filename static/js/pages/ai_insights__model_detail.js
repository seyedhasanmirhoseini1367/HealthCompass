var HC_PAGE_ai_insights__model_detail = JSON.parse(document.getElementById('hc-page-data-ai_insights__model_detail').textContent);
function fileSelected(input) {
  const name = document.getElementById('fileName');
  if (input.files[0]) {
    name.textContent = '✓ ' + input.files[0].name;
    name.style.display = 'block';
    document.getElementById('fileDrop').style.borderColor = 'var(--primary)';
    document.getElementById('fileDrop').style.background = '#f0f9ff';
  }
}

['dragenter','dragover'].forEach(e => {
  document.getElementById('fileDrop')?.addEventListener(e, ev => {
    ev.preventDefault();
    document.getElementById('fileDrop').classList.add('dragover');
  });
});
['dragleave','drop'].forEach(e => {
  document.getElementById('fileDrop')?.addEventListener(e, ev => {
    ev.preventDefault();
    document.getElementById('fileDrop').classList.remove('dragover');
    if (e === 'drop' && ev.dataTransfer.files[0]) {
      const fi = document.getElementById('fileInput');
      fi.files = ev.dataTransfer.files;
      fileSelected(fi);
    }
  });
});

// ── Use from Records ──────────────────────────────────────────────────────────
let _recordsLoaded = false;

function switchInputTab(tab) {
  const btnManual  = document.getElementById('tab-manual');
  const btnRecords = document.getElementById('tab-records');
  const panel      = document.getElementById('from-records-panel');
  if (!btnManual) return;
  if (tab === 'manual') {
    btnManual.style.cssText  += ';background:white;color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.1)';
    btnRecords.style.cssText += ';background:transparent;color:var(--muted);box-shadow:none';
    panel.style.display = 'none';
  } else {
    btnRecords.style.cssText += ';background:white;color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.1)';
    btnManual.style.cssText  += ';background:transparent;color:var(--muted);box-shadow:none';
    panel.style.display = 'block';
    if (!_recordsLoaded) _loadRecordList();
  }
}

async function _loadRecordList() {
  const sel = document.getElementById('record-select');
  try {
    const resp = await fetch(HC_PAGE_ai_insights__model_detail.k1);
    const data = await resp.json();
    if (data.records && data.records.length) {
      sel.innerHTML = '<option value="">— select a record —</option>';
      data.records.forEach(r => {
        const opt = document.createElement('option');
        opt.value = r.id;
        opt.textContent = r.title + (r.record_date ? ' (' + r.record_date + ')' : '');
        sel.appendChild(opt);
      });
    } else {
      sel.innerHTML = '<option value="">No lab records found — upload one first</option>';
    }
    _recordsLoaded = true;
  } catch (_) {
    sel.innerHTML = '<option value="">Error loading records</option>';
  }
}

async function loadRecordValues() {
  const sel = document.getElementById('record-select');
  const msg = document.getElementById('record-load-msg');
  if (!sel.value) { msg.style.color = '#ef4444'; msg.textContent = 'Please select a record first.'; return; }
  msg.style.color = 'var(--muted)'; msg.textContent = 'Loading…';
  try {
    const resp = await fetch('/insights/ajax/record-labs/' + sel.value + '/');
    const data = await resp.json();
    const labs = data.lab_values || [];
    let filled = 0;
    document.querySelectorAll('#runForm input[type=number]').forEach(inp => {
      const fn = inp.name.toLowerCase().replace(/[^a-z0-9]/g, '');
      const match = labs.find(lv => {
        const ln = (lv.parameter_name || '').toLowerCase().replace(/[^a-z0-9]/g, '');
        return ln === fn || ln.includes(fn) || fn.includes(ln);
      });
      if (match) {
        const n = parseFloat(match.value);
        if (!isNaN(n)) { inp.value = n; filled++; }
      }
    });
    if (filled > 0) {
      msg.style.color = '#16a34a';
      msg.textContent = '✓ Filled ' + filled + ' field' + (filled > 1 ? 's' : '') + ' from "' + data.title + '"';
    } else {
      msg.style.color = '#b45309';
      msg.textContent = '⚠ No matching fields found in this record. Try entering values manually.';
    }
  } catch (_) {
    msg.style.color = '#ef4444'; msg.textContent = 'Error loading record values.';
  }
}

document.getElementById('runForm')?.addEventListener('submit', async function(e) {
  e.preventDefault();
  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  btn.innerHTML = hcIcon('spinner', 'icon-spin') + ' Running…';
  try {
    const resp = await fetch(this.action, {
      method: 'POST',
      headers: {'X-Requested-With': 'XMLHttpRequest'},
      body: new FormData(this),
    });
    const data = await resp.json();
    const box = document.getElementById('resultBox');
    box.style.display = 'block';
    if (data.success) {
      const risk = data.result.risk_score;
      const pct  = risk !== null ? parseFloat((risk * 100).toFixed(1)) : null;
      document.getElementById('resultScore').textContent = pct !== null ? pct + '%' : '✓';
      document.getElementById('resultLabel').textContent = data.result.label || '';
      document.getElementById('riskFill').style.width    = (pct ?? 50) + '%';
      document.getElementById('resultNote').textContent  = data.result.demo ? '(Demo — no model file)' : '';
      const riskColor   = risk >= 0.75 ? '#fef2f2' : risk >= 0.45 ? '#fffbeb' : '#f0fdf4';
      const borderColor = risk >= 0.75 ? '#fca5a5' : risk >= 0.45 ? '#fde68a' : '#bbf7d0';
      box.style.background  = riskColor;
      box.style.borderColor = borderColor;
      if (data.interpretation) {
        document.getElementById('interpretationBox').style.display = 'block';
        document.getElementById('interpretationText').textContent  = data.interpretation;
      }
      if (data.prediction_id) {
        const link = document.getElementById('detailLink');
        link.href         = '/insights/prediction/' + data.prediction_id + '/';
        link.style.display = 'inline';
      }
    } else {
      document.getElementById('resultScore').textContent = '✗';
      document.getElementById('resultLabel').textContent = data.error || 'Run failed';
      box.style.background  = '#fef2f2';
      box.style.borderColor = '#fca5a5';
    }
  } catch(err) {
    hcToast('Network error: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = hcIcon('play') + ' Run Prediction';
  }
});
