var HC_PAGE_dashboard__monitoring = JSON.parse(document.getElementById('hc-page-data-dashboard__monitoring').textContent);
// ── Tab switching ──────────────────────────────────────────────────────────
function switchTab(name, btn) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
}

// ── Query trend chart ──────────────────────────────────────────────────────
const labels = HC_PAGE_dashboard__monitoring.k1;
const data   = HC_PAGE_dashboard__monitoring.k2;

const ctx = document.getElementById('queryChart');
if (ctx) {
  new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: 'Queries',
        data: data,
        borderColor: '#0ea5e9',
        backgroundColor: 'rgba(14,165,233,.08)',
        borderWidth: 2,
        pointRadius: 3,
        pointBackgroundColor: '#0ea5e9',
        fill: true,
        tension: 0.35,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#0f172a',
          titleFont: { size: 11 },
          bodyFont: { size: 12 },
          callbacks: {
            title: (items) => items[0].label,
            label: (item) => ` ${item.raw} queries`,
          }
        }
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { font: { size: 10 }, color: '#94a3b8' }
        },
        y: {
          beginAtZero: true,
          ticks: {
            stepSize: 1,
            font: { size: 10 },
            color: '#94a3b8',
            callback: (v) => Number.isInteger(v) ? v : ''
          },
          grid: { color: '#f1f5f9' }
        }
      }
    }
  });
}
