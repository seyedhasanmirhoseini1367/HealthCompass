/*
 * Processing status for records that are still being parsed (audit ARCH-02).
 * Any element with data-status-url and a data-processing-status of "queued" or
 * "processing" is polled every 3 seconds; when the record is ready the page
 * reloads so the parsed content appears, and a failure shows the message.
 */
(function () {
  'use strict';
  var PENDING = { queued: true, processing: true };

  function poll(el) {
    var tries = 0;
    function tick() {
      tries += 1;
      fetch(el.getAttribute('data-status-url'), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) { return; }
          el.setAttribute('data-processing-status', data.status);
          el.textContent = data.status_display || data.status;
          if (data.status === 'ready') { window.location.reload(); return; }
          if (data.status === 'failed') {
            el.classList.add('warn');
            if (data.error) { el.title = data.error; }
            return;
          }
          if (tries < 200) { window.setTimeout(tick, 3000); }
        })
        .catch(function () { if (tries < 200) { window.setTimeout(tick, 6000); } });
    }
    window.setTimeout(tick, 3000);
  }

  document.querySelectorAll('[data-status-url]').forEach(function (el) {
    if (PENDING[el.getAttribute('data-processing-status')]) { poll(el); }
  });
})();
