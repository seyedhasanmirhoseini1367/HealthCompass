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
  // -- Drag-and-drop file fields ------------------------------------------------
  // Turns a plain <input type=file> into a drop area. The real input stays in
  // the form (visually hidden, still focusable), so what is submitted does not
  // change. Inputs that are hidden, opted out, or already inside a page's own
  // drop area are left alone.
  function humanSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1048576) return Math.round(n / 1024) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }
  function enhanceFileInputs() {
    document.querySelectorAll('main input[type=file]').forEach(function (input) {
      if (input.hidden || input.dataset.noDropzone !== undefined || input.closest('.hc-drop, .drop-zone, .file-drop')) return;
      if (getComputedStyle(input).display === 'none') return;
      var drop = document.createElement('label');
      drop.className = 'hc-drop';
      if (input.id) drop.htmlFor = input.id;
      var hint = input.accept ? input.accept.split(',').map(function (s) { return s.trim().replace(/^\./, '').toUpperCase(); })
                                     .filter(function (s) { return s && s.indexOf('/') === -1; }).join(', ') : '';
      drop.innerHTML =
        hcIcon('cloud-arrow-up', 'hc-drop-icon') +
        '<span class="hc-drop-main"><strong>Drag a file here</strong> or <u>browse</u></span>' +
        (hint ? '<span class="hc-drop-hint"></span>' : '') +
        '<span class="hc-drop-file" aria-live="polite"></span>';
      if (hint) drop.querySelector('.hc-drop-hint').textContent = hint;
      input.parentNode.insertBefore(drop, input);
      drop.appendChild(input);
      input.classList.add('hc-drop-input');

      var fileLine = drop.querySelector('.hc-drop-file');
      function show() {
        var f = input.files;
        if (!f || !f.length) { fileLine.textContent = ''; drop.classList.remove('has-file'); return; }
        fileLine.textContent = f.length === 1 ? f[0].name + ' · ' + humanSize(f[0].size)
                                              : f.length + ' files selected';
        drop.classList.add('has-file');
      }
      input.addEventListener('change', show);
      ['dragenter', 'dragover'].forEach(function (t) {
        drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.add('over'); });
      });
      ['dragleave', 'dragend', 'drop'].forEach(function (t) {
        drop.addEventListener(t, function () { drop.classList.remove('over'); });
      });
      drop.addEventListener('drop', function (e) {
        e.preventDefault();
        var files = e.dataTransfer && e.dataTransfer.files;
        if (!files || !files.length) return;
        if (!input.multiple && files.length > 1) {
          fileLine.textContent = 'Drop one file at a time.';
          return;
        }
        input.files = files;
        input.dispatchEvent(new Event('change', { bubbles: true }));
      });
      show();
    });
  }

  function init() { wrapTables(); enhanceFileInputs(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

/* -- Password fields: a show/hide toggle (audit polish) ---------------------- */
(function () {
  'use strict';
  document.querySelectorAll('input[type="password"]').forEach(function (input) {
    if (input.dataset.noToggle !== undefined || input.nextElementSibling && input.nextElementSibling.classList.contains('pw-toggle')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pw-toggle';
    btn.textContent = 'Show';
    btn.setAttribute('aria-label', 'Show password');
    btn.setAttribute('aria-pressed', 'false');
    if (input.id) btn.setAttribute('aria-controls', input.id);
    btn.addEventListener('click', function () {
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.textContent = show ? 'Hide' : 'Show';
      btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      btn.setAttribute('aria-pressed', show ? 'true' : 'false');
    });
    input.insertAdjacentElement('afterend', btn);
  });
})();

