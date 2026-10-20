(async function () {
  'use strict';
  const link = document.querySelector('[data-hatuli-session-link]');
  if (!link) return;
  try {
    const response = await fetch('/api/auth/status', {
      credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      referrerPolicy: 'no-referrer'
    });
    if (!response.ok) return;
    const result = await response.json();
    if (result.authenticated !== true) return;
    link.textContent = 'Кабинет';
    link.className = 'pub-user-chip';
  } catch (_) {
    // Keep the working cabinet link; a failed status request grants no access.
  }
})();
