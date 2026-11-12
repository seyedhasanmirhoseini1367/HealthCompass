const allCards = Array.from(document.querySelectorAll('.model-card'));
let activeCat  = '';

function setCat(btn, val) {
  activeCat = val;
  document.querySelectorAll('.cat-chip').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  applyFilters();
}

function applyFilters() {
  const q    = (document.getElementById('modelSearch')?.value || '').toLowerCase().trim();
  const sort = document.getElementById('sortSelect')?.value;
  let visible = allCards.filter(c => {
    if (activeCat && c.dataset.cat !== activeCat) return false;
    if (q && !c.dataset.name.includes(q) && !c.dataset.desc.includes(q)) return false;
    return true;
  });
  visible.sort((a,b) => {
    if (sort==='runs')   return parseInt(b.dataset.runs)-parseInt(a.dataset.runs);
    if (sort==='name')   return a.dataset.name.localeCompare(b.dataset.name);
    if (sort==='newest') return parseInt(b.dataset.date)-parseInt(a.dataset.date);
    return 0;
  });
  const grid = document.getElementById('modelGrid');
  allCards.forEach(c => c.style.display='none');
  visible.forEach(c => { c.style.display='flex'; grid.appendChild(c); });
  document.getElementById('noResults').style.display = visible.length===0 ? 'block' : 'none';
}

function resetFilters() {
  activeCat = '';
  document.getElementById('modelSearch').value = '';
  document.querySelectorAll('.cat-chip').forEach(b => b.classList.remove('active'));
  document.querySelector('.cat-chip[data-cat=""]').classList.add('active');
  applyFilters();
}

document.addEventListener('DOMContentLoaded', () => applyFilters());
