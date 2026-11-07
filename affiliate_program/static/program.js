/* Progressive enhancements for the affiliate workspace. Forms work without JavaScript. */
(function () {
  'use strict';

  var toast = document.querySelector('[data-toast]');
  var toastTimer;
  function announce(message) {
    if (!toast) return;
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    toastTimer = window.setTimeout(function () { toast.hidden = true; }, 4200);
  }

  function legacyCopy(text) {
    var input = document.createElement('textarea');
    input.value = text;
    input.setAttribute('readonly', '');
    input.style.position = 'fixed';
    input.style.left = '-10000px';
    input.style.top = '0';
    document.body.appendChild(input);
    input.select();
    input.setSelectionRange(0, input.value.length);
    var copied = false;
    try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
    document.body.removeChild(input);
    return copied;
  }

  document.querySelectorAll('[data-copy]').forEach(function (button) {
    button.addEventListener('click', async function () {
      var text = button.getAttribute('data-copy');
      if (!text) return;
      var copied = false;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          copied = true;
        }
      } catch (error) { copied = false; }
      if (!copied) copied = legacyCopy(text);
      if (copied) {
        var label = button.querySelector('span');
        var oldLabel = label ? label.textContent : null;
        if (label) label.textContent = 'Copied';
        announce('Referral link copied. Ready to share.');
        button.focus({ preventScroll: true });
        window.setTimeout(function () { if (label) label.textContent = oldLabel; }, 2200);
      } else {
        var readonly = button.parentElement.querySelector('input[readonly]');
        if (readonly) { readonly.focus(); readonly.select(); }
        announce('Copy unavailable. Select the link and copy it manually.');
      }
    });
  });

  var nav = document.getElementById('workspace-nav');
  var navToggle = document.querySelector('[data-nav-toggle]');
  var previousFocus = null;
  function syncNavAccessibility() {
    if (!nav) return;
    var hidden = window.matchMedia('(max-width: 780px)').matches && !document.body.classList.contains('nav-open');
    nav.inert = hidden;
    if (hidden) nav.setAttribute('aria-hidden', 'true');
    else nav.removeAttribute('aria-hidden');
  }
  function setNav(open) {
    if (!nav || !navToggle) return;
    document.body.classList.toggle('nav-open', open);
    navToggle.setAttribute('aria-expanded', String(open));
    syncNavAccessibility();
    if (open) {
      previousFocus = document.activeElement;
      var close = nav.querySelector('[data-nav-close]');
      if (close) close.focus();
    } else if (previousFocus && window.matchMedia('(max-width: 780px)').matches) {
      previousFocus.focus({ preventScroll: true });
      previousFocus = null;
    }
  }
  if (navToggle) navToggle.addEventListener('click', function () { setNav(!document.body.classList.contains('nav-open')); });
  document.querySelectorAll('[data-nav-close], [data-nav-scrim]').forEach(function (element) {
    element.addEventListener('click', function () { setNav(false); });
  });
  document.addEventListener('keydown', function (event) {
    if (!document.body.classList.contains('nav-open') || !nav) return;
    if (event.key === 'Escape') { event.preventDefault(); setNav(false); }
    if (event.key === 'Tab') {
      var focusable = Array.from(nav.querySelectorAll('a, button, input, select')).filter(function (el) { return !el.disabled && el.offsetParent !== null; });
      if (!focusable.length) return;
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  window.addEventListener('resize', function () {
    if (window.innerWidth > 780 && document.body.classList.contains('nav-open')) setNav(false);
    syncNavAccessibility();
  });
  syncNavAccessibility();

  var navLinks = document.querySelectorAll('[data-nav-link]');
  function activateLink(hash) {
    navLinks.forEach(function (link) {
      var active = link.getAttribute('href') === hash;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
  }
  navLinks.forEach(function (link) {
    link.addEventListener('click', function () {
      activateLink(link.getAttribute('href'));
      if (document.body.classList.contains('nav-open')) setNav(false);
    });
  });
  if (window.location.hash) activateLink(window.location.hash);
  else if (navLinks[0]) navLinks[0].setAttribute('aria-current', 'location');
  if ('IntersectionObserver' in window && navLinks.length) {
    var visibleSections = new Map();
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) { visibleSections.set(entry.target.id, entry.isIntersecting); });
      var visible = Array.from(document.querySelectorAll('.page-section')).find(function (section) { return visibleSections.get(section.id); });
      if (visible) activateLink('#' + visible.id);
    }, { rootMargin: '-5% 0px -62% 0px', threshold: 0 });
    document.querySelectorAll('.page-section').forEach(function (section) { observer.observe(section); });
  }

  document.querySelectorAll('[data-password-toggle]').forEach(function (button) {
    button.addEventListener('click', function () {
      var field = document.getElementById(button.getAttribute('data-password-toggle'));
      if (!field) return;
      var show = field.type === 'password';
      field.type = show ? 'text' : 'password';
      button.setAttribute('aria-pressed', String(show));
      button.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    });
  });

  document.querySelectorAll('[data-open-details]').forEach(function (link) {
    link.addEventListener('click', function () {
      var target = document.getElementById(link.getAttribute('data-open-details'));
      if (target && target.tagName.toLowerCase() === 'details') target.open = true;
    });
  });

  var kind = document.querySelector('[data-payment-kind]');
  var amount = document.querySelector('[data-payment-amount]');
  var calculation = document.querySelector('[data-calculation-note]');
  function updateCalculation() {
    if (!kind || !amount || !calculation) return;
    var membership = kind.value === 'creator_membership';
    var value = Number(amount.value);
    if (!Number.isFinite(value) || value <= 0) {
      calculation.textContent = 'Enter a valid payment amount to preview the commission.';
      return;
    }
    var rate = membership ? 0.5 : 0.03;
    var commission = Math.round(value * rate * 100) / 100;
    calculation.textContent = '$' + value.toFixed(2) + (membership ? ' membership × 50% = $' : ' subscription × 3% = $') + commission.toFixed(2) + ' affiliate commission.';
  }
  if (kind && amount) {
    kind.addEventListener('change', function () {
      var membership = kind.value === 'creator_membership';
      amount.readOnly = membership;
      amount.value = membership ? '54.00' : '20.00';
      updateCalculation();
    });
    amount.addEventListener('input', updateCalculation);
    updateCalculation();
  }

  var currency = document.getElementById('currency');
  if (currency) currency.addEventListener('input', function () { currency.value = currency.value.toUpperCase().replace(/[^A-Z]/g, '').slice(0, 3); });
}());
