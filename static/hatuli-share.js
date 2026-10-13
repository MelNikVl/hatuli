/* Sharing always follows an explicit click and includes only the public URL. */
(function () {
  'use strict';
  document.querySelectorAll('[data-copy-share]').forEach(function (button) {
    var input = document.getElementById(button.getAttribute('data-copy-share'));
    var container = button.closest('.hp-share');
    var status = container && container.querySelector('[data-copy-share-status]');
    if (!input || !status) return;
    button.hidden = false;
    button.addEventListener('click', async function () {
      status.textContent = '';
      try {
        if (!navigator.clipboard || !window.isSecureContext) throw new Error('clipboard unavailable');
        await navigator.clipboard.writeText(input.value);
        status.textContent = 'Ссылка скопирована.';
      } catch (_) {
        input.focus();
        input.select();
        status.textContent = 'Ссылка выделена. Скопируйте её через меню устройства или Ctrl+C.';
      }
    });
  });
}());
