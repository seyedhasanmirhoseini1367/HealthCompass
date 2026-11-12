var HC_PAGE_ai_insights__icu__demo = JSON.parse(document.getElementById('hc-page-data-ai_insights__icu__demo').textContent);
Chart.defaults.color = '#475569';
Chart.defaults.borderColor = '#1e293b';
Chart.defaults.font.family = "'Inter', system-ui, sans-serif";

// ── Data from Django ───────────────────────────────────────────────────────
const VITALS = {
  hr:   HC_PAGE_ai_insights__icu__demo.k1,
  map:  HC_PAGE_ai_insights__icu__demo.k2,
  spo2: HC_PAGE_ai_insights__icu__demo.k3,
  rr:   HC_PAGE_ai_insights__icu__demo.k4,
};
const LABS = {
  creatinine: HC_PAGE_ai_insights__icu__demo.k5,
  lactate:    HC_PAGE_ai_insights__icu__demo.k6,
  wbc:        HC_PAGE_ai_insights__icu__demo.k7,
  platelets:  HC_PAGE_ai_insights__icu__demo.k8,
};

// ── Strip chart constants ─────────────────────────────────────────────────
var HIST_N  = 34;   // visible history steps
var FCST_N  = 8;    // forecast steps ahead
var TICK_MS = 2000; // animation interval ms

// ── Registry: id → entry ──────────────────────────────────────────────────
var strips = {};

// ── Linear regression on value array (uses index as x) ──────────────────
function linReg(ys) {
  var n = ys.length;
  if (n < 2) return {slope: 0, sigma: 0.5};
  var sx=0,sy=0,sxy=0,sx2=0;
  for (var i=0;i<n;i++){sx+=i;sy+=ys[i];sxy+=i*ys[i];sx2+=i*i;}
  var denom = n*sx2 - sx*sx;
  if (Math.abs(denom)<1e-9) return {slope:0, sigma:0.5};
  var slope = (n*sxy - sx*sy)/denom;
  var intercept = (sy - slope*sx)/n;
  var sse = 0;
  for (var i=0;i<n;i++){var r=ys[i]-(slope*i+intercept);sse+=r*r;}
  return {slope:slope, sigma:Math.max(0.1, Math.sqrt(sse/n))};
}

// ── Compute forecast arrays from current buffer ──────────────────────────
function buildForecast(buf, xCtr, cfg) {
  var fit = linReg(buf.slice(-12));
  var lastY = buf[buf.length-1];
  var mid=[], upper=[], lower=[];
  // anchor at current position
  mid.push({x:xCtr, y:lastY});
  upper.push({x:xCtr, y:lastY});
  lower.push({x:xCtr, y:lastY});
  for (var i=1;i<=FCST_N;i++) {
    var y = lastY + fit.slope * i;
    y = Math.max(cfg.min, Math.min(cfg.max, y));
    var band = fit.sigma * (1 + i * 0.22);
    mid.push({x:xCtr+i,   y: parseFloat(y.toFixed(2))});
    upper.push({x:xCtr+i, y: parseFloat(Math.min(cfg.max, y+band).toFixed(2))});
    lower.push({x:xCtr+i, y: parseFloat(Math.max(cfg.min, y-band).toFixed(2))});
  }
  return {mid:mid, upper:upper, lower:lower};
}

// ── Build strip chart ──────────────────────────────────────────────────────
function makeStrip(id, allData, cfg) {
  var ctx = document.getElementById(id);
  if (!ctx) return;

  var values = (allData||[]).map(function(p){return p[1];});
  if (!values.length) {
    ctx.parentElement.innerHTML = '<div class="no-data">No data recorded</div>';
    return;
  }

  // Data-driven mean for mean reversion
  if (cfg.mean == null) {
    cfg.mean = values.reduce(function(s,v){return s+v;},0) / values.length;
  }

  // Initialise buffer: last HIST_N points (pad left if shorter)
  var buf = values.slice(-HIST_N);
  while (buf.length < HIST_N) buf.unshift(buf[0]);
  var xCtr = HIST_N - 1;  // position of most-recent point

  function histPts(b, xc) {
    var xStart = xc - b.length + 1;
    return b.map(function(v,i){return {x:xStart+i, y:v};});
  }

  var fc = buildForecast(buf, xCtr, cfg);

  // Colour helpers
  var col = cfg.color;
  var rr=parseInt(col.slice(1,3),16), gg=parseInt(col.slice(3,5),16), bb=parseInt(col.slice(5,7),16);
  var bandFill  = 'rgba('+rr+','+gg+','+bb+',0.12)';
  var dashColor = 'rgba('+rr+','+gg+','+bb+',0.80)';

  // Threshold annotations
  var ann = {};
  if (cfg.hi!=null) ann.hi = {type:'line',yMin:cfg.hi,yMax:cfg.hi,borderColor:'#ef444455',borderWidth:1,borderDash:[4,3],label:{content:'hi',display:true,position:'end',color:'#ef4444',font:{size:8},backgroundColor:'transparent',padding:2}};
  if (cfg.lo!=null) ann.lo = {type:'line',yMin:cfg.lo,yMax:cfg.lo,borderColor:'#22c55e55',borderWidth:1,borderDash:[4,3],label:{content:'lo',display:true,position:'end',color:'#22c55e',font:{size:8},backgroundColor:'transparent',padding:2}};
  // "now" divider line
  ann.nowLine = {type:'line',xMin:xCtr+0.5,xMax:xCtr+0.5,borderColor:'#334155',borderWidth:1,borderDash:[3,4]};
  // Forecast shaded zone
  ann.fcstZone = {type:'box',xMin:xCtr+0.5,xMax:xCtr+FCST_N+1,yMin:'min',yMax:'max',backgroundColor:'rgba(100,116,139,0.06)',borderColor:'transparent',borderWidth:0};

  var chart = new Chart(ctx, {
    type: 'scatter',
    data: {datasets:[
      // 0 — historical trace
      {
        data: histPts(buf, xCtr),
        borderColor: col,
        backgroundColor: col+'1a',
        showLine:true, tension:0.35,
        pointRadius:0, pointHoverRadius:3,
        borderWidth:2, fill:true,
      },
      // 1 — forecast centre line (dashed)
      {
        _f:true,
        data: fc.mid,
        borderColor: dashColor,
        backgroundColor:'transparent',
        showLine:true, tension:0.35,
        pointRadius:[3].concat(Array(FCST_N).fill(0)),
        pointBackgroundColor: dashColor,
        borderWidth:1.5, borderDash:[5,3],
        fill:false,
      },
      // 2 — forecast upper band
      {
        _f:true,
        data: fc.upper,
        borderColor:'transparent',
        backgroundColor: bandFill,
        showLine:true, tension:0.35,
        pointRadius:0,
        fill:'+1',
      },
      // 3 — forecast lower band
      {
        _f:true,
        data: fc.lower,
        borderColor:'transparent',
        backgroundColor: bandFill,
        showLine:true, tension:0.35,
        pointRadius:0,
        fill:false,
      },
    ]},
    options:{
      responsive:true, maintainAspectRatio:false, animation:false,
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'#1e293b', titleColor:'#94a3b8', bodyColor:'#e2e8f0',
          borderColor:'#334155', borderWidth:1, padding:6,
          filter:function(item){return !item.dataset._f;},
          callbacks:{label:function(c){return c.parsed.y+' '+(cfg.unit||'');}}
        },
        annotation:{annotations:ann}
      },
      scales:{
        x:{type:'linear',
           min:0, max:xCtr+FCST_N+1,
           ticks:{display:false}, grid:{display:false}},
        y:{ticks:{font:{size:8},color:'#334155',maxTicksLimit:5},
           grid:{color:'#1e293b',lineWidth:1}}
      }
    }
  });

  // Initial value display
  if (cfg.valId) {
    var el = document.getElementById(cfg.valId);
    if (el) el.textContent = parseFloat(buf[buf.length-1].toFixed(1));
  }

  strips[id] = {chart:chart, buf:buf, xCtr:xCtr, cfg:cfg};
}

// ── Tick: advance every strip chart by one step ───────────────────────────
function tickAll() {
  Object.keys(strips).forEach(function(id) {
    var e   = strips[id];
    var cfg = e.cfg;
    var last = e.buf[e.buf.length-1];

    // Mean-reverting random walk
    var noise = (Math.random()-0.48) * cfg.noise;
    var rev   = 0.07 * (cfg.mean - last);
    var next  = Math.max(cfg.min, Math.min(cfg.max, last + rev + noise));
    next = parseFloat(next.toFixed(2));

    e.buf.push(next);
    if (e.buf.length > HIST_N) e.buf.shift();
    e.xCtr++;

    var xStart = e.xCtr - HIST_N + 1;
    var fc = buildForecast(e.buf, e.xCtr, cfg);
    var c  = e.chart;

    c.data.datasets[0].data = e.buf.map(function(v,i){return {x:xStart+i, y:v};});
    c.data.datasets[1].data = fc.mid;
    c.data.datasets[2].data = fc.upper;
    c.data.datasets[3].data = fc.lower;

    c.options.scales.x.min = xStart;
    c.options.scales.x.max = e.xCtr + FCST_N + 1;

    var ann = c.options.plugins.annotation.annotations;
    ann.nowLine.xMin = ann.nowLine.xMax = e.xCtr + 0.5;
    ann.fcstZone.xMin = e.xCtr + 0.5;
    ann.fcstZone.xMax = e.xCtr + FCST_N + 1;

    c.update('none');

    if (cfg.valId) {
      var el = document.getElementById(cfg.valId);
      if (el) el.textContent = parseFloat(next.toFixed(1));
    }
  });
}

// ── Create all charts ─────────────────────────────────────────────────────
makeStrip('hrChart',   VITALS.hr,        {color:'#38bdf8', unit:'bpm',    lo:60, hi:100, min:30,  max:200, noise:3.5,  valId:'hrVal'});
makeStrip('mapChart',  VITALS.map,       {color:'#818cf8', unit:'mmHg',   lo:65,         min:40,  max:160, noise:4.0,  valId:'mapVal'});
makeStrip('spo2Chart', VITALS.spo2,      {color:'#34d399', unit:'%',      lo:94,         min:70,  max:100, noise:0.8,  valId:'spo2Val'});
makeStrip('rrChart',   VITALS.rr,        {color:'#c084fc', unit:'br/min', hi:20,         min:8,   max:40,  noise:1.5,  valId:'rrVal'});
makeStrip('creaChart', LABS.creatinine,  {color:'#fbbf24', unit:'mg/dL',  hi:1.2,        min:0.2, max:15,  noise:0.04, valId:'creaVal'});
makeStrip('lacChart',  LABS.lactate,     {color:'#2dd4bf', unit:'mmol/L', hi:2.0,        min:0.3, max:20,  noise:0.06, valId:'lacVal'});
makeStrip('wbcChart',  LABS.wbc,         {color:'#60a5fa', unit:'K/uL',   hi:11,         min:0.5, max:50,  noise:0.25, valId:'wbcVal'});
makeStrip('pltChart',  LABS.platelets,   {color:'#f472b6', unit:'K/uL',   lo:150,        min:10,  max:800, noise:4.0,  valId:'pltVal'});

// ── Start animation ───────────────────────────────────────────────────────
setInterval(tickAll, TICK_MS);
