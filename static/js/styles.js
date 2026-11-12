/*
 * Inline styles without inline style attributes (audit SEC-10).
 *
 * An enforcing Content-Security-Policy blocks style="" attributes, but not
 * styles set through the CSSOM. Elements that need exact inline semantics
 * (template-computed values, elements scripts restyle by id) carry
 * data-inline-style; this copies it to element.style. Loaded right after
 * <main>, before page scripts, so they see the same element.style as before.
 * app.js runs it again for anything after this point and for content added later.
 */
(function () {
  'use strict';
  function applyInlineStyles(root) {
    (root || document).querySelectorAll('[data-inline-style]').forEach(function (el) {
      if (el.hasAttribute('data-inline-style-applied')) { return; }
      el.style.cssText += ';' + el.getAttribute('data-inline-style');
      el.setAttribute('data-inline-style-applied', '');
    });
  }
  window.hcApplyInlineStyles = applyInlineStyles;
  // Icon markup from the SVG sprite, for page scripts that build HTML. Here
  // rather than in app.js because page scripts run before app.js.
  var ICONS = document.body.getAttribute('data-icons') || '/static/icons.svg';
  window.hcIcon = function (name, extraClass) {
    return '<svg class="icon' + (extraClass ? ' ' + extraClass : '') + '" aria-hidden="true" focusable="false">' +
           '<use href="' + ICONS + '#i-' + name + '"></use></svg>';
  };
  applyInlineStyles(document);
})();
