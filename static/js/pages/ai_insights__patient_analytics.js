var HC_PAGE_ai_insights__patient_analytics = JSON.parse(document.getElementById('hc-page-data-ai_insights__patient_analytics').textContent);
// Escapes values interpolated into innerHTML: biomarker names, units and
// topics come from uploaded files (audit SEC-01).
function esc(v) { return String(v == null ? '' : v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// ── Tab switching ─────────────────────────────────────────────────────────────
document.querySelectorAll('.dual-tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.dual-tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

// ── Data from Django ──────────────────────────────────────────────────────────
const TRENDING    = HC_PAGE_ai_insights__patient_analytics.k1;
const ALERTS_DATA = HC_PAGE_ai_insights__patient_analytics.k2;
const REC_TYPES   = HC_PAGE_ai_insights__patient_analytics.k3;
const UPL_LABELS  = HC_PAGE_ai_insights__patient_analytics.k4;
const UPL_COUNTS  = HC_PAGE_ai_insights__patient_analytics.k5;
const PRED_LABELS = HC_PAGE_ai_insights__patient_analytics.k6;
const PRED_SCORES = HC_PAGE_ai_insights__patient_analytics.k7;

const POP_MODE_DIST    = HC_PAGE_ai_insights__patient_analytics.k8;
const POP_QUERY_LABELS = HC_PAGE_ai_insights__patient_analytics.k9;
const POP_QUERY_COUNTS = HC_PAGE_ai_insights__patient_analytics.k10;
const POP_TOPICS       = HC_PAGE_ai_insights__patient_analytics.k11;
const POP_SAFETY_LABELS= HC_PAGE_ai_insights__patient_analytics.k12;
const POP_SAFETY_COUNTS= HC_PAGE_ai_insights__patient_analytics.k13;

// ── Colour helpers ────────────────────────────────────────────────────────────
function ptColor(pt) {
  if (pt.critical) return '#ef4444';
  if (pt.abnormal) return '#f59e0b';
  return '#22c55e';
}
function bioColor(idx) {
  const palette = ['#0ea5e9','#6366f1','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#14b8a6'];
  return palette[idx % palette.length];
}

// ────────────────────────────────────────────────────────────────────────────
// TAB 1 — Personal charts
// ────────────────────────────────────────────────────────────────────────────

// ── Trend line charts ─────────────────────────────────────────────────────────
const container = document.getElementById('trend-charts-container');
if (container) {
  let idx = 0;
  for (const [biomarker, points] of Object.entries(TRENDING)) {
    const labels  = points.map(p => p.date);
    const values  = points.map(p => p.value);
    const colors  = points.map(p => ptColor(p));
    const unit    = points[0]?.unit || '';
    const latest  = points[points.length - 1];
    const prev    = points[points.length - 2];
    const delta   = latest.value - prev.value;
    const pct     = prev.value ? ((delta / prev.value) * 100).toFixed(1) : 0;
    const isUp    = delta > 0;
    const isDown  = delta < 0;

    const statusClass = latest.critical ? 'critical' : (latest.abnormal ? 'abnormal' : 'normal');
    const trendClass  = isUp ? 'trend-up' : (isDown ? 'trend-down' : 'trend-stable');
    const trendIcon   = isUp ? '&#x2191;' : (isDown ? '&#x2193;' : '&#x2192;');
    const trendText   = isUp
      ? `+${Math.abs(pct)}% since last`
      : (isDown ? `-${Math.abs(pct)}% since last` : 'Stable');

    const card = document.createElement('div');
    card.className = 'chart-card';
    card.innerHTML = `
      <div class="chart-card-header">
        <span class="chart-card-title">${esc(biomarker)}</span>
        <span class="trend-badge ${trendClass}">${trendIcon} ${trendText}</span>
      </div>
      <div class="chart-canvas-wrap">
        <canvas id="tc-${idx}"></canvas>
      </div>
      <div class="chart-meta">
        <span>${points.length} readings</span>
        <span class="val ${statusClass}">${esc(latest.value)} ${esc(unit)}</span>
      </div>`;
    container.appendChild(card);

    new Chart(document.getElementById(`tc-${idx}`), {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: biomarker,
          data: values,
          borderColor: bioColor(idx),
          backgroundColor: bioColor(idx) + '18',
          pointBackgroundColor: colors,
          pointBorderColor: colors,
          pointRadius: 5,
          pointHoverRadius: 7,
          tension: 0.35,
          fill: true,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: ctx => `${ctx.parsed.y} ${unit}` } }
        },
        scales: {
          x: { grid: { display: false }, ticks: { font: { size: 10 } } },
          y: { grid: { color: '#f1f5f9' }, ticks: { font: { size: 10 } } }
        }
      }
    });
    idx++;
  }
}

// ── Alerts counts ─────────────────────────────────────────────────────────────
const cntCritical = document.getElementById('cnt-critical');
const cntWarning  = document.getElementById('cnt-warning');
const cntInfo     = document.getElementById('cnt-info');
if (cntCritical) cntCritical.textContent = ALERTS_DATA.critical || 0;
if (cntWarning)  cntWarning.textContent  = ALERTS_DATA.warning  || 0;
if (cntInfo)     cntInfo.textContent     = ALERTS_DATA.info     || 0;

// ── Alerts donut ──────────────────────────────────────────────────────────────
const alertsCtx = document.getElementById('alerts-chart');
if (alertsCtx && (ALERTS_DATA.critical + ALERTS_DATA.warning + ALERTS_DATA.info) > 0) {
  new Chart(alertsCtx, {
    type: 'doughnut',
    data: {
      labels: ['Critical','Warning','Info'],
      datasets: [{ data: [ALERTS_DATA.critical, ALERTS_DATA.warning, ALERTS_DATA.info],
        backgroundColor: ['#ef4444','#f59e0b','#3b82f6'], borderWidth: 2, borderColor: '#fff' }]
    },
    options: { responsive: true, maintainAspectRatio: false, cutout: '65%',
      plugins: { legend: { position: 'bottom', labels: { font: { size: 11 } } } } }
  });
} else if (alertsCtx) {
  alertsCtx.parentElement.innerHTML = '<div style="text-align:center;padding:1.5rem;color:#475569;font-size:.85rem">' + hcIcon('check-circle', 'text-success') + 'No alerts yet</div>';
}

// ── Record types donut ────────────────────────────────────────────────────────
const recTypeCtx = document.getElementById('records-type-chart');
if (recTypeCtx && REC_TYPES.length) {
  const rtColors = ['#0ea5e9','#6366f1','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899'];
  new Chart(recTypeCtx, {
    type: 'doughnut',
    data: {
      labels: REC_TYPES.map(r => r.record_type.replace('_',' ')),
      datasets: [{ data: REC_TYPES.map(r => r.count), backgroundColor: rtColors, borderWidth: 2, borderColor: '#fff' }]
    },
    options: { responsive: true, maintainAspectRatio: false, cutout: '55%',
      plugins: { legend: { position: 'bottom', labels: { font: { size: 10 }, boxWidth: 12 } } } }
  });
}

// ── Upload activity bar chart ─────────────────────────────────────────────────
const uplCtx = document.getElementById('upload-chart');
if (uplCtx) {
  new Chart(uplCtx, {
    type: 'bar',
    data: {
      labels: UPL_LABELS.length ? UPL_LABELS : ['No data'],
      datasets: [{ label: 'Records uploaded', data: UPL_COUNTS.length ? UPL_COUNTS : [0],
        backgroundColor: '#6366f120', borderColor: '#6366f1', borderWidth: 2, borderRadius: 6 }]
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { grid: { display: false }, ticks: { font: { size: 10 } } },
                y: { grid: { color: '#f1f5f9' }, ticks: { stepSize: 1, font: { size: 10 } } } } }
  });
}

// ── AI risk score line chart ──────────────────────────────────────────────────
const riskCtx = document.getElementById('risk-chart');
if (riskCtx && PRED_SCORES.length) {
  new Chart(riskCtx, {
    type: 'line',
    data: {
      labels: PRED_LABELS,
      datasets: [{ label: 'Risk %', data: PRED_SCORES, borderColor: '#8b5cf6',
        backgroundColor: '#8b5cf620',
        pointBackgroundColor: PRED_SCORES.map(s => s >= 70 ? '#ef4444' : s >= 30 ? '#f59e0b' : '#22c55e'),
        pointRadius: 5, tension: 0.35, fill: true }]
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => `Risk: ${ctx.parsed.y}%` } } },
      scales: { x: { grid: { display: false }, ticks: { font: { size: 10 } } },
                y: { min: 0, max: 100, grid: { color: '#f1f5f9' }, ticks: { callback: v => v+'%', font: { size: 10 } } } } }
  });
}

// ────────────────────────────────────────────────────────────────────────────
// TAB 2 — Population charts
// ────────────────────────────────────────────────────────────────────────────

const MODE_COLORS = {
  personal:   '#7c3aed',
  general:    '#1d4ed8',
  hybrid:     '#065f46',
  trajectory: '#92400e',
  '':         '#64748b',
};
const MODE_BG = {
  personal:   '#ede9fe',
  general:    '#dbeafe',
  hybrid:     '#d1fae5',
  trajectory: '#fef3c7',
  '':         '#f1f5f9',
};
const MODE_LABELS = {
  personal:   'Personal',
  general:    'General Knowledge',
  hybrid:     'Hybrid',
  trajectory: 'Trend Analysis',
  '':         'Unknown',
};

// ── Query mode donut ──────────────────────────────────────────────────────────
const modeCtx = document.getElementById('pop-mode-chart');
if (modeCtx && POP_MODE_DIST.length) {
  const modeLabels = POP_MODE_DIST.map(d => MODE_LABELS[d.query_mode] || d.query_mode || 'Unknown');
  const modeCounts = POP_MODE_DIST.map(d => d.count);
  const modeColors = POP_MODE_DIST.map(d => MODE_COLORS[d.query_mode] || '#64748b');
  const modeBgs    = POP_MODE_DIST.map(d => MODE_BG[d.query_mode] || '#f1f5f9');

  new Chart(modeCtx, {
    type: 'doughnut',
    data: {
      labels: modeLabels,
      datasets: [{ data: modeCounts, backgroundColor: modeColors, borderWidth: 3, borderColor: '#fff' }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '60%',
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${ctx.parsed} queries` } } }
    }
  });

  // Legend pills
  const legend = document.getElementById('pop-mode-legend');
  if (legend) {
    const total = modeCounts.reduce((a, b) => a + b, 0);
    POP_MODE_DIST.forEach((d, i) => {
      const pct = total ? Math.round(d.count / total * 100) : 0;
      const pill = document.createElement('span');
      pill.className = 'mode-pill mode-' + (d.query_mode || 'unknown');
      pill.textContent = `${modeLabels[i]} ${pct}%`;
      legend.appendChild(pill);
    });
  }
} else if (modeCtx) {
  modeCtx.parentElement.innerHTML = '<div style="text-align:center;padding:2rem;color:#475569;font-size:.85rem">No queries yet</div>';
}

// ── Monthly query volume bar chart ────────────────────────────────────────────
const volCtx = document.getElementById('pop-volume-chart');
if (volCtx) {
  new Chart(volCtx, {
    type: 'bar',
    data: {
      labels: POP_QUERY_LABELS.length ? POP_QUERY_LABELS : ['No data'],
      datasets: [{
        label: 'Queries',
        data: POP_QUERY_COUNTS.length ? POP_QUERY_COUNTS : [0],
        backgroundColor: '#6366f130',
        borderColor: '#6366f1',
        borderWidth: 2,
        borderRadius: 6,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { font: { size: 10 } } },
        y: { grid: { color: '#f1f5f9' }, ticks: { stepSize: 1, font: { size: 10 } } }
      }
    }
  });
}

// ── Knowledge base topic bars ─────────────────────────────────────────────────
const topicContainer = document.getElementById('pop-topics-bars');
if (topicContainer && POP_TOPICS.length) {
  const maxCount = Math.max(...POP_TOPICS.map(t => t.count));
  topicContainer.innerHTML = POP_TOPICS.map(t => {
    const pct = Math.round(t.count / maxCount * 100);
    return `<div class="topic-bar-row">
      <span class="topic-bar-label">${esc(t.topic || 'General')}</span>
      <div class="topic-bar-track">
        <div class="topic-bar-fill" style="width:${pct}%"></div>
      </div>
      <span class="topic-bar-count">${esc(t.count)}</span>
    </div>`;
  }).join('');
} else if (topicContainer) {
  topicContainer.innerHTML = '<div style="text-align:center;padding:1.5rem;color:#475569;font-size:.85rem">No topics indexed yet.<br>Run <code>python manage.py load_knowledge_base</code></div>';
}

// ── Safety interventions line chart ──────────────────────────────────────────
const safetyCtx = document.getElementById('pop-safety-chart');
if (safetyCtx) {
  new Chart(safetyCtx, {
    type: 'line',
    data: {
      labels: POP_SAFETY_LABELS.length ? POP_SAFETY_LABELS : ['No data'],
      datasets: [{
        label: 'Safety Interceptions',
        data: POP_SAFETY_COUNTS.length ? POP_SAFETY_COUNTS : [0],
        borderColor: '#ef4444',
        backgroundColor: '#ef444418',
        pointBackgroundColor: '#ef4444',
        pointRadius: 5,
        tension: 0.35,
        fill: true,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { font: { size: 10 } } },
        y: { grid: { color: '#f1f5f9' }, ticks: { stepSize: 1, font: { size: 10 } } }
      }
    }
  });
}
