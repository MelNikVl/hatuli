/* Website sign-in: one request at a time, with a finite wait and a visible retry. */
(() => {
  'use strict';
  const button = document.getElementById('login-btn');
  const status = document.getElementById('login-status');
  if (!button || !status) return;
  const POLL_DELAY = 2000;
  const REQUEST_TIMEOUT = 12000;
  const MAX_WAIT = 10 * 60 * 1000;
  const MAX_POLLS = 300;
  let generation = 0;
  let timer;
  let controller;

  function cancel() {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
    if (controller) controller.abort();
    controller = undefined;
  }
  function retry(message) {
    cancel();
    button.disabled = false;
    button.removeAttribute('aria-busy');
    status.textContent = message;
  }
  function waiting(link, needsSubscription) {
    const anchor = document.createElement('a');
    anchor.href = link;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    anchor.textContent = 'Открыть Telegram';
    const prompt = needsSubscription
      ? 'Подпишись на канал t.me/hatuliapp, затем открой диалог и нажми Start ещё раз. '
      : 'Открой диалог и нажми Start — сайт обновится после подтверждения. ';
    status.replaceChildren(document.createTextNode(prompt), anchor);
  }
  function safeStart(data) {
    if (!data || typeof data.token !== 'string' || !/^[A-Za-z0-9_-]{16,100}$/.test(data.token)
        || typeof data.deep_link !== 'string') throw new Error('invalid-start');
    const link = new URL(data.deep_link);
    if (link.protocol !== 'https:' || link.hostname !== 't.me' || link.port || link.username || link.password
        || link.hash || !/^\/[A-Za-z0-9_]{5,32}$/.test(link.pathname)
        || link.searchParams.get('start') !== data.token || [...link.searchParams].length !== 1) {
      throw new Error('invalid-start');
    }
    return link.href;
  }
  async function readJSON(url, options, deadline) {
    controller = new AbortController();
    const requestController = controller;
    const timeout = setTimeout(() => requestController.abort(), Math.max(1, Math.min(REQUEST_TIMEOUT, deadline - Date.now())));
    try {
      const response = await fetch(url, {
        ...options, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        referrerPolicy: 'no-referrer', signal: requestController.signal
      });
      if (!response.ok) throw new Error('http-error');
      return await response.json();
    } finally {
      clearTimeout(timeout);
      if (controller === requestController) controller = undefined;
    }
  }
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    cancel();
    const current = ++generation;
    const deadline = Date.now() + MAX_WAIT;
    let polls = 0;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    status.textContent = 'Готовим вход через Telegram…';
    let data, link;
    try {
      data = await readJSON('/api/auth/start', {method: 'POST'}, deadline);
      if (current !== generation) return;
      link = safeStart(data);
      waiting(link, false);
      // A blocked popup still leaves a normal, keyboard-accessible link.
      try { window.open(link, '_blank', 'noopener,noreferrer'); } catch (_) {}
    } catch (_) {
      if (current === generation) retry('Не удалось начать вход. Проверь соединение и нажми «Войти через Telegram» ещё раз.');
      return;
    }
    async function poll() {
      timer = undefined;
      if (current !== generation) return;
      if (Date.now() >= deadline || polls >= MAX_POLLS) {
        retry('Время ожидания истекло. Нажми «Войти через Telegram», чтобы получить новую ссылку.');
        return;
      }
      polls += 1;
      try {
        const result = await readJSON('/api/auth/poll?token=' + encodeURIComponent(data.token), {}, deadline);
        if (current !== generation) return;
        if (Date.now() >= deadline) {
          retry('Время ожидания истекло. Нажми «Войти через Telegram», чтобы получить новую ссылку.');
          return;
        }
        if (result && result.status === 'verified') {
          cancel();
          status.textContent = 'Вход выполнен. Обновляем страницу…';
          window.location.reload();
          return;
        }
        if (result && (result.status === 'expired' || result.status === 'not_found' || result.status === 'consumed')) {
          retry('Ссылка устарела или уже использована. Нажми «Войти через Telegram» ещё раз.');
          return;
        }
        if (!result || !['pending', 'not_subscribed'].includes(result.status)) throw new Error('invalid-status');
        if (result.status === 'not_subscribed') waiting(link, true);
        timer = setTimeout(poll, Math.min(POLL_DELAY, Math.max(1, deadline - Date.now())));
      } catch (_) {
        if (current === generation) retry('Не удалось проверить вход. Проверь соединение и нажми «Войти через Telegram» ещё раз.');
      }
    }
    timer = setTimeout(poll, POLL_DELAY);
  });
  window.addEventListener('pagehide', () => {
    generation += 1;
    cancel();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) retry('Нажми «Войти через Telegram», чтобы начать вход заново.');
  });
})();
