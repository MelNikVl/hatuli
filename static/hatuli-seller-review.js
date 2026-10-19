(function () {
  'use strict';
  if (window.location.hash === '#seller-result' && !document.querySelector('#seller-result')) {
    const message = document.querySelector('.seller-source,.seller-alert');
    if (message) message.scrollIntoView({block:'center'});
  }
  const button = document.querySelector('[data-copy-description]');
  if (!button || !navigator.clipboard) return;
  button.hidden = false;
  button.addEventListener('click', async function () {
    const status = document.querySelector('[data-copy-status]');
    try {
      await navigator.clipboard.writeText(document.querySelector('#seller-draft-text').textContent);
      status.textContent = 'Описание скопировано. Проверьте факты перед публикацией.';
    } catch (_) {
      status.textContent = 'Выделите текст описания и скопируйте его вручную.';
    }
  });
})();
