var HC_PAGE_medical_records__list_1 = JSON.parse(document.getElementById('hc-page-data-medical_records__list-1').textContent);
/* ── URLs & CSRF ─────────────────────────────────────────────────────────── */
var SCAN_OCR_URL = HC_PAGE_medical_records__list_1.k1;

function getCsrf() {
  var el = document.querySelector('#addRecordModal [name=csrfmiddlewaretoken]');
  return el ? el.value : '';
}

/* ── Panel titles ────────────────────────────────────────────────────────── */
var PANEL_TITLES = {
  scan:     hcIcon('camera', 'req-pink') + ' Scan Paper',
  pdf:      hcIcon('file-pdf', 'req-teal') + ' PDF / Document',
  text:     hcIcon('paste', 'req-amber') + ' Paste Text',
  kanta:    hcIcon('file-code', 'req-sky') + ' Kanta XML',
  wearable: hcIcon('clock', 'req-purple') + ' Wearable Data',
};

/* ── Modal open/close ────────────────────────────────────────────────────── */
function openAddModal() {
  showOptions();
  document.getElementById('addRecordModal').classList.add('open');
  document.body.style.overflow = 'hidden';
}
function closeAddModal() {
  document.getElementById('addRecordModal').classList.remove('open');
  document.body.style.overflow = '';
}
function closeIfBackdrop(e) {
  if (e.target === document.getElementById('addRecordModal')) closeAddModal();
}
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeAddModal();
});

/* ── Panel navigation ────────────────────────────────────────────────────── */
/*
 * Visibility is toggled through `d-none`, never through inline display.
 *
 * These three elements are hidden in the markup with `class="d-none"`, and
 * `.d-none` is `display:none !important`. An inline `style.display` cannot beat
 * `!important`, so setting one here was a no-op: picking an option swapped the
 * title but left every panel hidden, and the back arrow never appeared. The
 * modal looked like it had lost its contents.
 *
 * Each class carries its own display (`.modal-options` flex, `.modal-back`
 * inline-flex, `.upload-panel` block), so removing the class restores the right
 * one without this function needing to know which.
 */
function showOptions() {
  document.querySelectorAll('.upload-panel').forEach(function(p){ p.classList.add('d-none'); });
  document.getElementById('panel-options').classList.remove('d-none');
  document.getElementById('modalBack').classList.add('d-none');
  document.getElementById('modalTitle').innerHTML =
    hcIcon('plus-circle', 'text-primary') + ' Add Record';
  document.getElementById('modalBox').classList.remove('wide');
}
function showPanel(name) {
  var panel = document.getElementById('panel-' + name);
  if (!panel) { return; }   // an unknown name must not blank the modal

  document.getElementById('panel-options').classList.add('d-none');
  document.querySelectorAll('.upload-panel').forEach(function(p){ p.classList.add('d-none'); });
  panel.classList.remove('d-none');
  document.getElementById('modalBack').classList.remove('d-none');
  document.getElementById('modalTitle').innerHTML = PANEL_TITLES[name] || name;
  document.getElementById('modalBox').classList.add('wide');
}

/* ── File chosen label ───────────────────────────────────────────────────── */
function setChosen(labelId, input) {
  var el = document.getElementById(labelId);
  if (el && input.files[0]) {
    el.innerHTML = '<span class="file-chosen">' + esc(input.files[0].name) + '</span>';
  }
}

/* ── Form submission ─────────────────────────────────────────────────────── */
document.querySelectorAll('.upload-panel form').forEach(function(form) {
  form.addEventListener('submit', function(e) {
    e.preventDefault();
    var btn = form.querySelector('[type=submit]');
    var url = btn.dataset.url;
    submitUploadForm(form, url, btn);
  });
});

async function submitUploadForm(form, url, btn) {
  var origHtml = btn.innerHTML;
  var errBanner = form.querySelector('.form-error-banner');
  if (errBanner) { errBanner.style.display = 'none'; }

  btn.disabled = true;
  btn.innerHTML = '<span class="up-spin"></span> Saving…';

  try {
    var fd = new FormData(form);
    var res = await fetch(url, {
      method: 'POST',
      body: fd,
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    });
    var data = await res.json();

    if (data.success) {
      closeAddModal();
      form.reset();
      resetAllFileLabels();
      if (data.reload) {
        showToast(data.message || 'Records imported!', data.flagged ? 'warn' : 'ok');
        setTimeout(function(){ location.reload(); }, 1200);
      } else if (data.record) {
        prependRecord(data.record);
        showToast(data.message || 'Record saved!', data.flagged ? 'warn' : 'ok');
        updateRecordCount(1);
      }
    } else {
      showFormError(errBanner, data.error || 'Upload failed.');
    }
  } catch(e) {
    showFormError(errBanner, 'Network error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }
}

function showFormError(banner, msg) {
  if (!banner) { hcToast(msg, 'error'); return; }
  banner.textContent = msg;
  banner.style.display = 'block';
  banner.scrollIntoView({ behavior:'smooth', block:'nearest' });
}

function resetAllFileLabels() {
  var defaults = {
    'pdf-chosen-name':      'Click to choose PDF',
    'kanta-chosen-name':    'Click to choose XML file',
    'wearable-chosen-name': 'Click to choose CSV file',
  };
  Object.keys(defaults).forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.innerHTML = defaults[id];
  });
}

/* ── Scan capture ────────────────────────────────────────────────────────── */
var modalScanBlob = null;

document.getElementById('modal-scan-input').addEventListener('change', function() {
  var file = this.files[0];
  if (!file) return;
  modalScanBlob = file;
  document.getElementById('modal-scan-preview').src = URL.createObjectURL(file);
  // Same `d-none` rule as the panels: the preview is hidden by an !important
  // class, so an inline display could never reveal it — you took a photo and
  // the modal showed you nothing.
  document.getElementById('scan-capture-wrap').classList.add('d-none');
  document.getElementById('scan-preview-wrap').classList.remove('d-none');
  setScanStatus('');
});

function resetScanCapture() {
  modalScanBlob = null;
  document.getElementById('modal-scan-input').value = '';
  document.getElementById('scan-capture-wrap').classList.remove('d-none');
  document.getElementById('scan-preview-wrap').classList.add('d-none');
  document.getElementById('modal-scan-text').value = '';
  setScanStatus('');
}

function setScanStatus(msg, loading) {
  var el = document.getElementById('modal-scan-status');
  el.innerHTML = loading ? '<span class="up-spin up-spin-pink"></span> ' + msg : msg;
}

document.getElementById('modal-extract-btn').addEventListener('click', async function() {
  if (!modalScanBlob) { setScanStatus('No image captured yet.'); return; }
  this.disabled = true;
  setScanStatus('Sending to Gemini Vision…', true);
  try {
    var fd = new FormData();
    fd.append('image', modalScanBlob, 'scan.jpg');
    fd.append('csrfmiddlewaretoken', getCsrf());
    var res  = await fetch(SCAN_OCR_URL, { method:'POST', body:fd });
    var data = await res.json();
    if (data.error) {
      setScanStatus(hcIcon('triangle-exclamation', 'text-danger') + ' ' + esc(data.error));
    } else {
      document.getElementById('modal-scan-text').value = data.text;
      setScanStatus(hcIcon('check-circle', 'text-success') + ' Text extracted — review and save.');
    }
  } catch(e) {
    setScanStatus('Error: ' + esc(e.message));
  } finally {
    this.disabled = false;
  }
});

/* ── DOM: prepend new record row ─────────────────────────────────────────── */
var TYPE_EMOJI = {
  lab_result:'🧪', prescription:'💊', diagnosis:'🏥',
  vaccination:'💉', imaging:'🩻', wearable:'⌚', discharge:'📄',
};

function prependRecord(rec) {
  var emoji = TYPE_EMOJI[rec.record_type]  || '📁';
  var csrf  = document.querySelector('[name=csrfmiddlewaretoken]').value;
  var dateSpan  = rec.record_date ? '<span>' + esc(rec.record_date) + '</span>' : '';
  var flagBadge = rec.is_flagged  ? '<span class="rec-badge warn">⚠️ Abnormal</span>' : '';
  var rowCls    = rec.is_flagged  ? 'rec-row flagged' : 'rec-row';

  // Data attributes, not inline handlers: the CSP forbids those (audit SEC-10).
  var html = '<div class="' + rowCls + '" data-href="' + esc(rec.detail_url) + '">'
    + '<div class="rec-icon-sm ri-' + esc(rec.record_type) + '">' + emoji + '</div>'
    + '<div class="rec-info">'
      + '<div class="rec-title">' + esc(rec.title) + '</div>'
      + '<div class="rec-meta-row">'
        + '<span class="rec-badge">' + esc(rec.record_type_display) + '</span>'
        + '<span class="rec-badge">' + esc(rec.source_display) + '</span>'
        + dateSpan
        + '<span>Uploaded ' + esc(rec.uploaded_at) + '</span>'
        + flagBadge
      + '</div>'
    + '</div>'
    + '<div class="rec-row-actions" data-stop-propagation>'
      + '<a href="' + esc(rec.detail_url) + '" class="row-btn view">View</a>'
      + '<form id="del-rec-' + rec.pk + '" method="post" action="' + esc(rec.delete_url) + '" class="d-inline">'
        + '<input type="hidden" name="csrfmiddlewaretoken" value="' + esc(csrf) + '">'
        + '<button type="button" class="row-btn delete"'
          + ' data-confirm="Delete this record permanently?" data-confirm-title="Delete Record"'
          + ' data-submit-form="del-rec-' + esc(rec.pk) + '">'
          + 'Delete'
        + '</button>'
      + '</form>'
    + '</div>'
  + '</div>';

  var list = document.querySelector('.record-list');
  if (list) {
    list.insertAdjacentHTML('afterbegin', html);
    var empty = document.querySelector('.empty-state');
    if (empty) empty.remove();
  } else {
    var listDiv = document.createElement('div');
    listDiv.className = 'record-list';
    listDiv.innerHTML = html;
    var main  = document.querySelector('.records-main');
    var empty = main ? main.querySelector('.empty-state') : null;
    if (empty) main.replaceChild(listDiv, empty);
    else if (main) main.appendChild(listDiv);
  }
}

function esc(str) {
  return String(str)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

/* ── Count update ────────────────────────────────────────────────────────── */
function updateRecordCount(delta) {
  var el = document.querySelector('.toolbar-count .count');
  if (!el) return;
  var n   = (parseInt(el.textContent) || 0) + delta;
  var wrap = el.closest('.toolbar-count');
  wrap.innerHTML = '<span class="count">' + esc(n) + '</span> record' + (n !== 1 ? 's' : '');
}

/* ── Toast ───────────────────────────────────────────────────────────────── */
function showToast(msg, type) {
  // The site-wide toast (static/js/app.js), not a second implementation.
  hcToast(msg, type === 'warn' ? 'warning' : 'success');
}
