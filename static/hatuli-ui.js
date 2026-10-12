/* Shared navigation and appearance; map/application state stays in its own code. */
(function () {
  'use strict';
  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }
  function updateThemeButton() {
    const button = document.getElementById('hatuli-theme-toggle');
    if (!button) return;
    const label = currentTheme() === 'light' ? 'Включить тёмную тему' : 'Включить светлую тему';
    button.setAttribute('aria-label', label);
    button.title = label;
    button.setAttribute('aria-pressed', String(currentTheme() === 'dark'));
  }
  window.toggleHatuliTheme = function () {
    const next = currentTheme() === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('hatuli-theme', next); } catch (_) { /* Storage may be disabled. */ }
    updateThemeButton();
  };
  const header = document.querySelector('.pub-header');
  const menu = document.getElementById('hatuli-menu-toggle');
  if (header && menu) {
    header.setAttribute('data-enhanced', 'true');
    function setOpen(open) {
      header.classList.toggle('is-open', open);
      menu.setAttribute('aria-expanded', String(open));
    }
    menu.addEventListener('click', function () { setOpen(!header.classList.contains('is-open')); });
    header.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && header.classList.contains('is-open')) {
        setOpen(false); menu.focus();
      }
    });
    const measure = function () {
      document.documentElement.style.setProperty('--hdr-h', header.offsetHeight + 'px');
    };
    if (window.ResizeObserver) new ResizeObserver(measure).observe(header);
    measure();
  }
  updateThemeButton();
})();
