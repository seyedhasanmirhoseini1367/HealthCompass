var HC_PAGE_ai_insights__population = JSON.parse(document.getElementById('hc-page-data-ai_insights__population').textContent);
const TRENDING    = HC_PAGE_ai_insights__population.k1;
const ALERTS_DATA = HC_PAGE_ai_insights__population.k2;
const REC_TYPES   = HC_PAGE_ai_insights__population.k3;
const UPL_LABELS  = HC_PAGE_ai_insights__population.k4;
const UPL_COUNTS  = HC_PAGE_ai_insights__population.k5;
const RISK_LABELS = HC_PAGE_ai_insights__population.k6;
const RISK_COUNTS = HC_PAGE_ai_insights__population.k7;
const PALETTE = ['#0ea5e9','#6366f1','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#14b8a6'];

const openSections   = new Set();
const chartInstances = {};

function toggleSection(id) {
  const panel = document.getElementById('sp-'+id);
  const card  = document.getElementById('sc-'+id);
  if (!panel) return;
  if (openSections.has(id)) {
    openSections.delete(id); panel.classList.remove('open'); card&&card.classList.remove('open');
  } else {
    openSections.add(id); panel.classList.add('open'); card&&card.classList.add('open');
    initSection(id);
  }
}

function initSection(id) {
  if (chartInstances[id]) return;
  if (id === 'biomarkers') { buildBioList(); return; }
  if (id === 'alerts') {
    document.getElementById('cnt-critical').textContent = ALERTS_DATA.critical||0;
    document.getElementById('cnt-warning').textContent  = ALERTS_DATA.warning||0;
    document.getElementById('cnt-info').textContent     = ALERTS_DATA.info||0;
    const ctx = document.getElementById('alerts-chart');
    const total = (ALERTS_DATA.critical||0)+(ALERTS_DATA.warning||0)+(ALERTS_DATA.info||0);
    if (total > 0) {
      chartInstances[id] = new Chart(ctx,{
        type:'doughnut',
        data:{labels:['Critical','Warning','Info'],datasets:[{data:[ALERTS_DATA.critical,ALERTS_DATA.warning,ALERTS_DATA.info],backgroundColor:['#ef4444','#f59e0b','#3b82f6'],borderWidth:2,borderColor:'#fff'}]},
        options:{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{legend:{position:'bottom',labels:{font:{size:10}}}}}
      });
    } else {
      ctx.parentElement.innerHTML='<div class="no-data-small">No alerts yet</div>';
    }
    return;
  }
  if (id === 'records') {
    if (REC_TYPES.length) {
      chartInstances[id] = new Chart(document.getElementById('rec-type-chart'),{
        type:'doughnut',
        data:{labels:REC_TYPES.map(r=>r.record_type.replace('_',' ')),datasets:[{data:REC_TYPES.map(r=>r.count),backgroundColor:PALETTE,borderWidth:2,borderColor:'#fff'}]},
        options:{responsive:true,maintainAspectRatio:false,cutout:'55%',plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:12}}}}
      });
    }
    return;
  }
  if (id === 'activity') {
    chartInstances[id] = new Chart(document.getElementById('upload-chart'),{
      type:'bar',
      data:{labels:UPL_LABELS.length?UPL_LABELS:['No data'],datasets:[{label:'Uploads',data:UPL_COUNTS.length?UPL_COUNTS:[0],backgroundColor:'#0ea5e920',borderColor:'#0ea5e9',borderWidth:2,borderRadius:5}]},
      options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{display:false},ticks:{font:{size:9}}},y:{grid:{color:'#f1f5f9'},ticks:{stepSize:1,font:{size:9}}}}}
    });
    return;
  }
  if (id === 'risk') {
    const ctx = document.getElementById('risk-chart');
    if (ctx && RISK_COUNTS.some(v=>v>0)) {
      chartInstances[id] = new Chart(ctx,{
        type:'bar',
        data:{labels:RISK_LABELS,datasets:[{label:'Patients',data:RISK_COUNTS,backgroundColor:['#22c55e80','#f59e0b80','#ef444480'],borderColor:['#22c55e','#f59e0b','#ef4444'],borderWidth:2,borderRadius:6}]},
        options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>`${c.parsed.y} patients`}}},scales:{x:{grid:{display:false},ticks:{font:{size:9}}},y:{grid:{color:'#f1f5f9'},ticks:{stepSize:1,font:{size:9}}}}}
      });
    }
  }
}

// Biomarker browser — population monthly averages
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
  container.querySelectorAll('.bio-item').forEach(el=>el.remove());
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
  const pts    = TRENDING[bio];
  const labels = pts.map(p=>p.date);
  const values = pts.map(p=>p.value);
  const unit   = pts[0]?.unit||'';
  const latest = pts[pts.length-1], first=pts[0];
  const delta  = latest.value-first.value;
  const pct    = first.value?((delta/first.value)*100).toFixed(1):0;
  const dir    = delta>0?'up':delta<0?'down':'stable';
  const arrow  = delta>0?'↑':delta<0?'↓':'→';
  const color  = '#0ea5e9';
  const totalPts = pts.reduce((s,p)=>s+(p.count||1),0);

  document.getElementById('bio-placeholder').style.display='none';
  document.getElementById('bio-chart-content').style.display='block';
  document.getElementById('bio-title').textContent=bio+(unit?' · '+unit:'');
  document.getElementById('bio-subtitle').textContent='Population avg · '+pts.length+' months · '+labels[0]+' → '+labels[labels.length-1];
  const pill=document.getElementById('bio-trend-pill');
  pill.className=`trend-pill tp-${dir}`;
  pill.textContent=`${arrow} ${(delta>=0?'+':'')+pct}% overall`;

  document.getElementById('bio-stats').replaceChildren(
    bioStatEl(latest.value, unit, 'Latest avg', '#0c4a6e'),
    bioStatEl(first.value, unit, 'First avg', '#64748b'),
    bioStatEl((delta>=0?'+':'')+pct+'%', '', 'Change', delta>0?'#ef4444':delta<0?'#16a34a':'#6366f1'),
    bioStatEl(totalPts, '', 'Data points', '#8b5cf6'));

  if (bioChartInst){bioChartInst.destroy();bioChartInst=null;}
  bioChartInst=new Chart(document.getElementById('bio-main-chart'),{
    type:'line',
    data:{labels,datasets:[{
      label:'Population avg',
      data:values,
      borderColor:color, backgroundColor:color+'18',
      pointBackgroundColor:color, pointRadius:5, pointHoverRadius:7,
      tension:0.35, fill:true,
    }]},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>`Avg: ${c.parsed.y} ${unit} (${pts[c.dataIndex]?.count||''} pts)`}}},
      scales:{
        x:{grid:{display:false},ticks:{font:{size:10},maxRotation:40}},
        y:{grid:{color:'#f1f5f9'},ticks:{font:{size:10},callback:v=>v+(unit?' '+unit:'')}}
      }
    }
  });
}
