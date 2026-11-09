/* Site-wide UI behaviour: loading bar, busy submit buttons, scrollable tables.
   Every part is progressive enhancement: with JS off, the site works as before. */
(function () {
  'use strict';

  // -- Loading bar ------------------------------------------------------------
  var bar = document.createElement('div');
  bar.className = 'hc-progress';
  bar.setAttribute('aria-hidden', 'true');
  document.body.appendChild(bar);
  var startTimer = null;

  function startBar() {
    clearTimeout(startTimer);
    // Fast pages never show it; only waits longer than 150 ms do.
    startTimer = setTimeout(function () { bar.classList.add('run'); }, 150);
  }
  function resetAll() {
    clearTimeout(startTimer);
    bar.classList.remove('run');
    document.querySelectorAll('.is-busy').forEach(function (b) {
      b.classList.remove('is-busy');
      b.removeAttribute('aria-busy');
      if (b.dataset.hcWasEnabled) { b.disabled = false; delete b.dataset.hcWasEnabled; }
    });
  }
  // Back/forward cache restores the page as it was left: clear the busy state.
  window.addEventListener('pageshow', resetAll);

  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.target === '_blank' || a.hasAttribute('download') || a.dataset.noProgress !== undefined) return;
    var href = a.getAttribute('href');
    if (!href || href.charAt(0) === '#' || /^(mailto:|tel:|javascript:)/i.test(href)) return;
    var url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    // File downloads (exports) never fire pageshow; do not leave a bar running.
    if (/\/(export|download)\//.test(url.pathname)) return;
    startBar();
  });

  // -- Busy submit buttons ------------------------------------------------------
  // Runs in the bubble phase, after the form's own handlers: a form that
  // submits by fetch() calls preventDefault and is left alone.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (e.defaultPrevented || form.dataset.noBusy !== undefined) return;
    if (form.target === '_blank') return;
    var btn = e.submitter && e.submitter.matches('button, input[type=submit]') ? e.submitter
            : form.querySelector('button[type=submit], button:not([type]), input[type=submit]');
    var action = form.getAttribute('action') || '';
    if (!/\/(export|download)\//.test(action)) startBar();
    if (!btn || btn.dataset.noBusy !== undefined) return;
    // Disable after the browser has captured the submitter's name/value,
    // so the button that was pressed is still sent.
    setTimeout(function () {
      // <input> cannot show a ::after spinner; it is only disabled.
      if (btn.tagName === 'BUTTON') btn.classList.add('is-busy');
      btn.setAttribute('aria-busy', 'true');
      if (!btn.disabled) { btn.dataset.hcWasEnabled = '1'; btn.disabled = true; }
    }, 0);
    // A download response leaves the page in place: release after a while.
    setTimeout(resetAll, 15000);
  });

  // -- Wide tables scroll sideways instead of widening the page ---------------
  function wrapTables() {
    document.querySelectorAll('main table').forEach(function (t) {
      var p = t.parentElement;
      if (!p || p.classList.contains('table-scroll')) return;
      var ox = getComputedStyle(p).overflowX;
      if (ox === 'auto' || ox === 'scroll') return;
      var wrap = document.createElement('div');
      wrap.className = 'table-scroll';
      p.insertBefore(wrap, t);
      wrap.appendChild(t);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wrapTables);
  else wrapTables();
})();
