var HC_PAGE_medical_records__upload_scan = JSON.parse(document.getElementById('hc-page-data-medical_records__upload_scan').textContent);
const OCR_URL  = HC_PAGE_medical_records__upload_scan.k1;
const CSRF     = HC_PAGE_medical_records__upload_scan.k2;
let capturedBlob = null;

// ── File input (mobile camera / desktop file picker) ──────────────────────
document.getElementById('fileInput').addEventListener('change', function() {
  const file = this.files[0];
  if (!file) return;
  capturedBlob = file;
  showPreview(URL.createObjectURL(file));
});

function showPreview(src) {
  document.getElementById('cameraArea').style.display = 'none';
  document.getElementById('preview-wrap').style.display = 'block';
  document.getElementById('preview-img').src = src;
  setStatus('');
}

function resetCapture() {
  capturedBlob = null;
  document.getElementById('cameraArea').style.display = 'flex';
  document.getElementById('preview-wrap').style.display = 'none';
  document.getElementById('fileInput').value = '';
  setStatus('');
}

function esc(v) { return String(v == null ? '' : v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function setStatus(msg, loading=false) {
  const el = document.getElementById('ocrStatus');
  el.innerHTML = loading
    ? `<div class="spinner"></div> ${msg}`
    : msg;
}

// ── OCR extraction ────────────────────────────────────────────────────────
document.getElementById('extractBtn').addEventListener('click', async function() {
  if (!capturedBlob) { setStatus('No image captured yet.'); return; }

  setStatus('Sending to Gemini Vision…', true);
  this.disabled = true;

  try {
    const fd = new FormData();
    fd.append('image', capturedBlob, 'scan.jpg');
    fd.append('csrfmiddlewaretoken', CSRF);

    const res  = await fetch(OCR_URL, { method: 'POST', body: fd });
    const data = await res.json();

    if (data.error) {
      setStatus(hcIcon('exclamation-triangle', 'text-danger') + ' ' + esc(data.error));
    } else {
      document.getElementById('rawText').value = data.text;
      setStatus(hcIcon('check-circle', 'text-success') + ' Text extracted — review and save.');
      document.getElementById('rawText').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  } catch(e) {
    setStatus(hcIcon('exclamation-triangle', 'text-danger') + ' Network error: ' + esc(e.message));
  } finally {
    this.disabled = false;
  }
});

// ── Webcam ────────────────────────────────────────────────────────────────
let webcamStream = null;

async function openWebcam() {
  document.getElementById('webcamModal').classList.add('open');
  try {
    webcamStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
    document.getElementById('webcamVideo').srcObject = webcamStream;
  } catch(e) {
    hcToast('Could not access the camera: ' + e.message, 'error');
    closeWebcam();
  }
}

function closeWebcam() {
  if (webcamStream) { webcamStream.getTracks().forEach(t => t.stop()); webcamStream = null; }
  document.getElementById('webcamModal').classList.remove('open');
}

function captureWebcam() {
  const video  = document.getElementById('webcamVideo');
  const canvas = document.getElementById('webcamCanvas');
  canvas.width  = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0);
  canvas.toBlob(blob => {
    capturedBlob = blob;
    showPreview(canvas.toDataURL('image/jpeg'));
    closeWebcam();
  }, 'image/jpeg', 0.92);
}
