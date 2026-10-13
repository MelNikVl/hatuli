(async function () {
  'use strict';
  const token = new URLSearchParams(window.location.hash.slice(1)).get('token');
  // Remove the one-use credential before requests, navigation or page history.
  window.history.replaceState(null, '', window.location.pathname);
  const title = document.getElementById('telegram-login-title');
  const status = document.getElementById('telegram-login-status');
  const retry = document.getElementById('telegram-login-retry');
  function failed(message) {
    title.textContent = 'Повтори вход';
    status.textContent = message;
    retry.hidden = false;
  }
  if (!token || !/^[A-Za-z0-9_-]{16,100}$/.test(token)) {
    failed('Открой свежую ссылку из бота. Ссылка для входа действует 10 минут и используется один раз.');
    return;
  }
  try {
    const response = await fetch('/api/auth/complete', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      referrerPolicy: 'no-referrer',
      headers: {'Content-Type': 'application/json', 'X-Hatuli-Login': 'telegram'},
      body: JSON.stringify({token})
    });
    const result = await response.json();
    if (response.ok && result.status === 'verified') {
      status.textContent = 'Вход выполнен. Открываем сайт…';
      window.location.replace('/map');
      return;
    }
    failed(response.status === 401
      ? 'Ссылка уже использована, устарела или доступ закрыт. Начни вход ещё раз.'
      : 'Не удалось подтвердить вход. Попробуй ещё раз.');
  } catch (_) {
    failed('Не удалось связаться с Hatuli. Проверь соединение и начни вход ещё раз.');
  }
})();
