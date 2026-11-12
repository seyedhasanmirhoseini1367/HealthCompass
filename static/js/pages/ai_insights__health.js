var HC_PAGE_ai_insights__health = JSON.parse(document.getElementById('hc-page-data-ai_insights__health').textContent);
const TRENDING     = HC_PAGE_ai_insights__health.k1;
const POP_AVG      = HC_PAGE_ai_insights__health.k2;
const POP_TRENDING = HC_PAGE_ai_insights__health.k3;
const ALERTS_DATA = HC_PAGE_ai_insights__health.k4;
const REC_TYPES   = HC_PAGE_ai_insights__health.k5;
const UPL_LABELS  = HC_PAGE_ai_insights__health.k6;
const UPL_COUNTS  = HC_PAGE_ai_insights__health.k7;
const PRED_LABELS = HC_PAGE_ai_insights__health.k8;
const PRED_SCORES = HC_PAGE_ai_insights__health.k9;
const PALETTE = ['#0ea5e9','#6366f1','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#14b8a6'];
function ptColor(pt){ return pt.critical?'#ef4444':pt.abnormal?'#f59e0b':'#22c55e'; }

const openSections   = new Set();
const chartInstances = {};

function toggleSection(id) {
  const panel = document.getElementById('sp-' + id);
  const card  = document.getElementById('sc-' + id);
  if (!panel) return;
  if (openSections.has(id)) {
    openSections.delete(id);
    panel.classList.remove('open');
    card && card.classList.remove('open');
  } else {
    openSections.add(id);
    panel.classList.add('open');
    card && card.classList.add('open');
    initSection(id);
  }
}

function initSection(id) {
  if (chartInstances[id]) return;
  if (id === 'biomarkers') { buildBioList(); return; }
  if (id === 'alerts') {
    document.getElementById('cnt-critical').textContent = ALERTS_DATA.critical || 0;
    document.getElementById('cnt-warning').textContent  = ALERTS_DATA.warning  || 0;
    document.getElementById('cnt-info').textContent     = ALERTS_DATA.info     || 0;
    const ctx = document.getElementById('alerts-chart');
    const total = (ALERTS_DATA.critical||0)+(ALERTS_DATA.warning||0)+(ALERTS_DATA.info||0);
    if (total > 0) {
      chartInstances[id] = new Chart(ctx, {
        type:'doughnut',
        data:{labels:['Critical','Warning','Info'],datasets:[{data:[ALERTS_DATA.critical,ALERTS_DATA.warning,ALERTS_DATA.info],backgroundColor:['#ef4444','#f59e0b','#3b82f6'],borderWidth:2,borderColor:'#fff'}]},
        options:{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{legend:{position:'bottom',labels:{font:{size:10}}}}}
      });
    } else {
      ctx.parentElement.innerHTML='<div class="no-data-small">' + hcIcon('check-circle', 'text-success') + '<br>No alerts yet</div>';
    }
    return;
  }
  if (id === 'records') {
    const ctx = document.getElementById('rec-type-chart');
    if (REC_TYPES.length) {
      chartInstances[id] = new Chart(ctx, {
        type:'doughnut',
        data:{labels:REC_TYPES.map(r=>r.record_type.replace('_',' ')),datasets:[{data:REC_TYPES.map(r=>r.count),backgroundColor:PALETTE,borderWidth:2,borderColor:'#fff'}]},
        options:{responsive:true,maintainAspectRatio:false,cutout:'55%',plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:12}}}}
      });
    }
    return;
  }
  if (id === 'activity') {
    chartInstances[id] = new Chart(document.getElementById('upload-chart'), {
      type:'bar',
      data:{labels:UPL_LABELS.length?UPL_LABELS:['No data'],datasets:[{label:'Uploads',data:UPL_COUNTS.length?UPL_COUNTS:[0],backgroundColor:'#6366f120',borderColor:'#6366f1',borderWidth:2,borderRadius:5}]},
      options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{display:false},ticks:{font:{size:9}}},y:{grid:{color:'#f1f5f9'},ticks:{stepSize:1,font:{size:9}}}}}
    });
    return;
  }
  if (id === 'risk') {
    const ctx = document.getElementById('risk-chart');
    if (ctx && PRED_SCORES.length) {
      chartInstances[id] = new Chart(ctx, {
        type:'line',
        data:{labels:PRED_LABELS,datasets:[{label:'Risk %',data:PRED_SCORES,borderColor:'#ec4899',backgroundColor:'#ec489920',pointBackgroundColor:PRED_SCORES.map(s=>s>=70?'#ef4444':s>=30?'#f59e0b':'#22c55e'),pointRadius:5,tension:0.35,fill:true}]},
        options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>`Risk: ${c.parsed.y}%`}}},scales:{x:{grid:{display:false},ticks:{font:{size:9}}},y:{min:0,max:100,grid:{color:'#f1f5f9'},ticks:{callback:v=>v+'%',font:{size:9}}}}}
      });
    }
  }
}

// biomarker browser
let activeBio    = null;
let bioChartInst = null;
const bioKeys    = Object.keys(TRENDING);

// Built with the DOM, never innerHTML: biomarker names and units come from
// uploaded files and are attacker-controlled (audit SEC-01).
function bioStatEl(value, unit, label, color) {
  const box = document.createElement('div'); box.className = 'bio-stat';
  const num = document.createElement('div'); num.className = 'bsn'; num.style.color = color;
  num.append(String(value));
  if (unit) {
    const u = document.createElement('span');
    u.style.fontSize = '.8rem'; u.style.fontWeight = '400';
    u.textContent = ' ' + unit; num.appendChild(u);
  }
  const lab = document.createElement('div'); lab.className = 'bsl'; lab.textContent = label;
  box.append(num, lab);
  return box;
}
function bioItemContent(item, bio, cls, arrow) {
  const name = document.createElement('span'); name.textContent = bio;
  const ar = document.createElement('span'); ar.className = 'bio-arrow ' + cls; ar.textContent = arrow;
  item.append(name, ar);
}

function buildBioList() {
  const container = document.getElementById('bio-list-container');
  container.querySelectorAll('.bio-item').forEach(el => el.remove());
  bioKeys.forEach(bio => {
    const pts  = TRENDING[bio];
    const last = pts[pts.length-1], prev = pts[pts.length-2];
    const d    = last.value - prev.value;
    const arrow = d>0?'↑':d<0?'↓':'→', cls = d>0?'up':d<0?'down':'stable';
    const item = document.createElement('div');
    item.className='bio-item'; item.dataset.bio=bio;
    bioItemContent(item, bio, cls, arrow);
    item.onclick=()=>selectBio(bio);
    container.appendChild(item);
  });
  if (bioKeys.length>0) selectBio(bioKeys[0]);
}

function filterBioList(q) {
  document.querySelectorAll('.bio-item').forEach(el=>{
    el.style.display=el.dataset.bio.toLowerCase().includes(q.toLowerCase())?'':'none';
  });
}

function selectBio(bio) {
  activeBio=bio;
  document.querySelectorAll('.bio-item').forEach(el=>el.classList.toggle('active',el.dataset.bio===bio));
  const pts=TRENDING[bio], labels=pts.map(p=>p.date), values=pts.map(p=>p.value), colors=pts.map(p=>ptColor(p));
  const unit=pts[0]?.unit||'', latest=pts[pts.length-1], first=pts[0];
  const delta=latest.value-first.value, pct=first.value?((delta/first.value)*100).toFixed(1):0;
  const dir=delta>0?'up':delta<0?'down':'stable', arrow=delta>0?'↑':delta<0?'↓':'→';
  const color=delta>0?'#ef4444':delta<0?'#16a34a':'#6366f1';
  document.getElementById('bio-placeholder').style.display='none';
  document.getElementById('bio-chart-content').style.display='block';
  document.getElementById('bio-title').textContent=bio+(unit?' · '+unit:'');
  document.getElementById('bio-subtitle').textContent=pts.length+' readings · '+labels[0]+' → '+labels[labels.length-1];
  const pill=document.getElementById('bio-trend-pill');
  pill.className=`trend-pill tp-${dir}`;
  pill.textContent=`${arrow} ${(delta>=0?'+':'')+pct}% overall`;

  // Match each personal data point to the nearest population monthly average
  const popSeries = POP_TRENDING[bio] || null;
  function nearestPopValue(dateStr, series) {
    if (!series || !series.length) return null;
    const month = dateStr.substring(0, 7);
    const exact = series.find(p => p.date === month);
    if (exact) return exact.value;
    const t = new Date(dateStr).getTime();
    let best = null, bestDist = Infinity;
    for (const p of series) {
      const dist = Math.abs(new Date(p.date + '-01').getTime() - t);
      if (dist < bestDist) { bestDist = dist; best = p; }
    }
    return best ? best.value : null;
  }
  const popValues = popSeries ? pts.map(p => nearestPopValue(p.date, popSeries)) : null;
  const hasPopTrend = popValues && popValues.some(v => v !== null);

  // Pop avg stat (overall mean, for the stats row)
  const popAvg = POP_AVG[bio] != null ? POP_AVG[bio] : null;
  const sClass=latest.critical?'#dc2626':latest.abnormal?'#d97706':'#16a34a';
  const statEls = [
    bioStatEl(latest.value, unit, 'My latest', sClass),
    bioStatEl(first.value, unit, 'First', '#64748b'),
    bioStatEl((delta>=0?'+':'')+pct+'%', '', 'Total change', color),
  ];
  if (popAvg != null) statEls.push(bioStatEl(popAvg, unit, 'Pop. avg', '#94a3b8'));
  statEls.push(bioStatEl(pts.length, '', 'Readings', '#8b5cf6'));
  document.getElementById('bio-stats').replaceChildren(...statEls);

  // Build datasets — personal + optional population trend line
  const datasets = [{
    label: 'My values',
    data: values,
    borderColor: color, backgroundColor: color+'18',
    pointBackgroundColor: colors, pointBorderColor: colors,
    pointRadius: 5, pointHoverRadius: 7, tension: 0.35, fill: true,
  }];
  if (hasPopTrend) {
    datasets.push({
      label: 'Population trend',
      data: popValues,
      borderColor: '#94a3b8', borderDash: [5, 4], borderWidth: 2,
      backgroundColor: 'transparent',
      pointRadius: 3, pointHoverRadius: 5,
      pointBackgroundColor: '#94a3b8', pointBorderColor: '#94a3b8',
      tension: 0.35, fill: false,
    });
  }

  if (bioChartInst){bioChartInst.destroy();bioChartInst=null;}
  bioChartInst=new Chart(document.getElementById('bio-main-chart'),{
    type:'line',
    data:{labels, datasets},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{display: hasPopTrend, labels:{font:{size:10},boxWidth:14,usePointStyle:true}},
        tooltip:{callbacks:{label:c=>`${c.dataset.label}: ${c.parsed.y} ${unit}`}}
      },
      scales:{
        x:{grid:{display:false},ticks:{font:{size:10},maxRotation:40}},
        y:{grid:{color:'#f1f5f9'},ticks:{font:{size:10},callback:v=>(+v.toFixed(2))+(unit?' '+unit:'')}}
      }
    }
  });
}
