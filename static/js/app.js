/*
 * Site-wide behaviour (audit prompt 7). Formerly an inline <script> in
 * base.html; moved here so the Content-Security-Policy can forbid inline script.
 *
 * Page markup declares behaviour with data attributes instead of on*="" handlers:
 *   data-on-click="fn" data-args='[...]'   call window.fn(...args) on click
 *       (also data-on-change, data-on-input, data-on-keydown, data-on-submit);
 *       an argument {"$": "this"|"event"|"value"|"text"} passes the element,
 *       the event, the element's value or its text
 *   data-prevent-default                   preventDefault() after the call
 *   data-href="url"                        navigate on click (table rows)
 *   data-stop-propagation                  stop clicks bubbling to a data-href row
 *   data-click-target="id"                 click another element (file inputs)
 *   data-autosubmit                        submit the form when the value changes
 *   data-copy="text"                       copy to the clipboard, show "Copied"
 *   data-confirm="message"                 on a form: ask before submitting;
 *       with data-submit-form="id" on a button: ask, then submit that form
 */
(function () {
  'use strict';

  // hcIcon() is defined in styles.js, which loads before page scripts.

  // ── Account menu (button + role="menu") ──────────────────────────────────
  function setUserMenu(open) {
    var menu = document.getElementById('userMenu');
    var btn = document.getElementById('navUserBtn');
    if (!menu || !btn) { return; }
    menu.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      var first = menu.querySelector('[role="menuitem"]');
      if (first) { first.focus(); }
    }
  }
  window.toggleUserMenu = function () {
    var menu = document.getElementById('userMenu');
    setUserMenu(menu ? menu.hidden : false);
  };

  window.toggleMobileMenu = function () {
    var menu = document.getElementById('mobileMenu');
    var btn = document.getElementById('hamburgerBtn');
    var open = menu.classList.toggle('open');
    if (btn) { btn.setAttribute('aria-expanded', open ? 'true' : 'false'); }
  };

  // ── Confirmation dialog (native <dialog>) ────────────────────────────────
  var lastFocus = null;
  window.openDeleteModal = function (message, onConfirm, title, confirmLabel) {
    var dlg = document.getElementById('deleteModal');
    document.getElementById('deleteModalTitle').textContent = title || 'Are you sure?';
    document.getElementById('deleteModalMsg').textContent = message || 'This action cannot be undone.';
    var ok = document.getElementById('deleteModalConfirm');
    ok.textContent = confirmLabel || 'Delete';
    ok.onclick = function () { window.closeDeleteModal(); if (onConfirm) { onConfirm(); } };
    lastFocus = document.activeElement;
    if (typeof dlg.showModal === 'function') { dlg.showModal(); } else { dlg.setAttribute('open', ''); }
    var cancel = dlg.querySelector('.btn-secondary');
    if (cancel) { cancel.focus(); }
  };
  window.closeDeleteModal = function () {
    var dlg = document.getElementById('deleteModal');
    if (dlg.open && typeof dlg.close === 'function') { dlg.close(); } else { dlg.removeAttribute('open'); }
  };
  var dialog = document.getElementById('deleteModal');
  if (dialog) {
    dialog.addEventListener('close', function () { if (lastFocus && lastFocus.focus) { lastFocus.focus(); } });
    dialog.addEventListener('click', function (e) { if (e.target === dialog) { window.closeDeleteModal(); } });
  }
  // A confirm() replacement that returns a promise, for page scripts.
  window.hcConfirm = function (message, title, confirmLabel) {
    return new Promise(function (resolve) {
      var settled = false;
      window.openDeleteModal(message, function () { settled = true; resolve(true); }, title, confirmLabel || 'Continue');
      dialog.addEventListener('close', function () { if (!settled) { resolve(false); } }, { once: true });
    });
  };

  // ── Toasts ────────────────────────────────────────────────────────────────
  // Success and info dismiss themselves after 6 s; warnings and errors stay.
  var DURATION = 6000;
  function dismiss(t) {
    if (t.classList.contains('leaving')) { return; }
    t.classList.add('leaving');
    t.addEventListener('animationend', function () { t.remove(); }, { once: true });
    setTimeout(function () { t.remove(); }, 400);
  }
  function wireToast(t, i) {
    t.style.animationDelay = (i * 80) + 'ms';
    t.querySelector('.toast-close').addEventListener('click', function () { dismiss(t); });
    if (t.dataset.autohide !== '1') { return; }
    var left = DURATION, started = null, timer = null;
    var bar = t.querySelector('.toast-timer');
    if (bar) { bar.style.animationDuration = DURATION + 'ms'; }
    function run() { started = Date.now(); timer = setTimeout(function () { dismiss(t); }, left); t.classList.remove('paused'); }
    function pause() { clearTimeout(timer); left -= Date.now() - started; t.classList.add('paused'); }
    t.addEventListener('mouseenter', pause); t.addEventListener('focusin', pause);
    t.addEventListener('mouseleave', run); t.addEventListener('focusout', run);
    run();
  }
  var stack = document.getElementById('toastStack');
  if (stack) { stack.querySelectorAll('.toast').forEach(wireToast); }
  // One toast API for page scripts (replaces alert()).
  window.hcToast = function (message, level) {
    level = level || 'success';
    var s = document.getElementById('toastStack');
    if (!s) {
      s = document.createElement('div');
      s.className = 'toast-stack';
      s.id = 'toastStack';
      document.body.appendChild(s);
    }
    var t = document.createElement('div');
    t.className = 'toast toast-' + level;
    t.setAttribute('role', level === 'error' || level === 'warning' ? 'alert' : 'status');
    t.dataset.autohide = level === 'error' || level === 'warning' ? '0' : '1';
    var icon = { success: 'circle-check', error: 'circle-exclamation', warning: 'triangle-exclamation' }[level] || 'circle-info';
    t.innerHTML = '<span class="toast-icon">' + window.hcIcon(icon) + '</span><div class="toast-body"></div>' +
                  '<button type="button" class="toast-close" aria-label="Dismiss">' + window.hcIcon('xmark') + '</button>' +
                  '<span class="toast-timer" aria-hidden="true"></span>';
    t.querySelector('.toast-body').textContent = message;
    s.appendChild(t);
    wireToast(t, 0);
  };

  // ── Sidebar ───────────────────────────────────────────────────────────────
  window.toggleSidebar = function () {
    var sb = document.getElementById('appSidebar');
    var btn = document.getElementById('sbToggleBtn');
    if (!sb) { return; }
    var collapsed = sb.classList.toggle('collapsed');
    document.body.classList.toggle('sidebar-collapsed', collapsed);
    try { localStorage.setItem('hc_sb', collapsed ? '1' : '0'); } catch (e) { /* private mode */ }
    if (btn) {
      btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      btn.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
    }
  };
  (function () {
    var sb = document.getElementById('appSidebar');
    var btn = document.getElementById('sbToggleBtn');
    if (!sb) { return; }
    var stored = null;
    try { stored = localStorage.getItem('hc_sb'); } catch (e) { /* private mode */ }
    if (stored === '1') {
      sb.classList.add('collapsed');
      document.body.classList.add('sidebar-collapsed');
      if (btn) { btn.setAttribute('aria-expanded', 'false'); btn.setAttribute('aria-label', 'Expand sidebar'); }
    }
    // Longest matching href wins, so /dashboard/settings/ marks Settings.
    var path = location.pathname, best = null;
    document.querySelectorAll('.sb-link').forEach(function (a) {
      var h = a.getAttribute('href');
      if (h && path.indexOf(h) === 0 && (!best || h.length > best.getAttribute('href').length)) { best = a; }
    });
    if (best) { best.classList.add('active'); best.setAttribute('aria-current', 'page'); }
  })();

  // ── Delegated behaviour from data attributes ─────────────────────────────
  function resolveArgs(el, event) {
    var raw = el.getAttribute('data-args');
    if (!raw) { return []; }
    var args;
    try { args = JSON.parse(raw); } catch (e) { return []; }
    return args.map(function (a) {
      if (a && typeof a === 'object' && a.$) {
        return { 'this': el, 'event': event, 'value': el.value, 'text': el.textContent }[a.$];
      }
      return a;
    });
  }
  function dispatch(type, event) {
    var el = event.target.closest && event.target.closest('[data-on-' + type + ']');
    if (!el) { return false; }
    var name = el.getAttribute('data-on-' + type);
    var fn = window[name];
    if (typeof fn !== 'function') {
      if (window.console) { console.error('No handler named ' + name); }
      return true;
    }
    var result = fn.apply(el, resolveArgs(el, event));
    if (el.hasAttribute('data-prevent-default') || result === false) { event.preventDefault(); }
    return true;
  }
  ['change', 'input', 'keydown'].forEach(function (type) {
    document.addEventListener(type, function (e) {
      dispatch(type, e);
      if (type === 'change' && e.target.closest && e.target.closest('[data-autosubmit]') && e.target.form) {
        e.target.form.submit();
      }
    });
  });

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t.closest) { return; }
    if (dispatch('click', e)) { return; }

    var confirmBtn = t.closest('[data-submit-form][data-confirm]');
    if (confirmBtn) {
      e.preventDefault();
      var form = document.getElementById(confirmBtn.getAttribute('data-submit-form'));
      window.openDeleteModal(confirmBtn.getAttribute('data-confirm'), function () { if (form) { form.submit(); } },
                             confirmBtn.getAttribute('data-confirm-title'));
      return;
    }
    var target = t.closest('[data-click-target]');
    if (target) {
      var other = document.getElementById(target.getAttribute('data-click-target'));
      if (other) { other.click(); }
      return;
    }
    var copy = t.closest('[data-copy]');
    if (copy) {
      var text = copy.getAttribute('data-copy');
      var done = function () {
        var label = copy.getAttribute('data-copied-label') || 'Copied';
        var before = copy.innerHTML;
        copy.textContent = label;
        copy.setAttribute('aria-live', 'polite');
        setTimeout(function () { copy.innerHTML = before; }, 2000);
      };
      if (navigator.clipboard) { navigator.clipboard.writeText(text).then(done); } else { done(); }
      return;
    }
    if (t.closest('[data-stop-propagation]')) { return; }
    var row = t.closest('[data-href]');
    if (row && !t.closest('a, button, input, select, textarea, label, form')) {
      window.location.href = row.getAttribute('data-href');
      return;
    }

    // Close the account and mobile menus on an outside click.
    var navUser = document.getElementById('navUserBtn');
    var menu = document.getElementById('userMenu');
    if (menu && !menu.hidden && navUser && !navUser.contains(t) && !menu.contains(t)) { setUserMenu(false); }
    var hb = document.getElementById('hamburgerBtn');
    var mm = document.getElementById('mobileMenu');
    if (mm && hb && !mm.contains(t) && !hb.contains(t)) { mm.classList.remove('open'); }
  });

  // Keyboard: Enter on a data-href row; Escape closes menus.
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      var menu = document.getElementById('userMenu');
      if (menu && !menu.hidden) { setUserMenu(false); document.getElementById('navUserBtn').focus(); }
      var mm = document.getElementById('mobileMenu');
      if (mm) { mm.classList.remove('open'); }
    }
    if (e.key === 'Enter' && e.target.matches && e.target.matches('[data-href]')) {
      window.location.href = e.target.getAttribute('data-href');
    }
  });

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (dispatch('submit', e)) { return; }
    if (form.hasAttribute('data-confirm') && !form.hasAttribute('data-confirmed')) {
      e.preventDefault();
      window.openDeleteModal(form.getAttribute('data-confirm'), function () {
        form.setAttribute('data-confirmed', '');
        if (form.requestSubmit) { form.requestSubmit(); } else { form.submit(); }
      }, form.getAttribute('data-confirm-title') || 'Please confirm', 'Continue');
    }
  });

  // Rows navigable with data-href are reachable by keyboard.
  document.querySelectorAll('[data-href]').forEach(function (row) {
    if (!row.matches('a, button') && !row.hasAttribute('tabindex')) {
      row.setAttribute('tabindex', '0');
      row.setAttribute('role', row.getAttribute('role') || 'link');
    }
  });

  // ── Idle sign-out warning ────────────────────────────────────────────────
  // The session ends SESSION_COOKIE_AGE after the last request. One minute
  // before that, offer to stay signed in; staying pings the server, which
  // moves the expiry forward. Unanswered, the page goes to the login screen.
  (function () {
    var seconds = parseInt(document.body.getAttribute('data-session-seconds') || '0', 10);
    var ping = document.body.getAttribute('data-session-ping');
    var dlg = document.getElementById('idleDialog');
    if (!seconds || !ping || !dlg || seconds < 120) { return; }
    var warnTimer = null, endTimer = null;
    function csrf() {
      var el = document.querySelector('[name=csrfmiddlewaretoken]');
      return el ? el.value : '';
    }
    function schedule() {
      clearTimeout(warnTimer); clearTimeout(endTimer);
      warnTimer = setTimeout(function () {
        if (typeof dlg.showModal === 'function') { dlg.showModal(); } else { dlg.setAttribute('open', ''); }
        document.getElementById('idleStay').focus();
        endTimer = setTimeout(function () { window.location.href = '/accounts/login/?next=' + encodeURIComponent(location.pathname); }, 60000);
      }, (seconds - 60) * 1000);
    }
    document.getElementById('idleStay').addEventListener('click', function () {
      fetch(ping, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': csrf() } })
        .then(function (r) { if (r.ok) { if (dlg.open) { dlg.close(); } schedule(); } else { window.location.reload(); } });
    });
    schedule();
  })();

  if (window.hcApplyInlineStyles) { window.hcApplyInlineStyles(document); }
})();
