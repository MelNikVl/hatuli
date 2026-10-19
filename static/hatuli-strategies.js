/* Amount-only comparison. The POST endpoint also renders a complete no-JS page. */
(() => {
  'use strict';
  const form = document.querySelector('[data-strategy-form]');
  if (!form) return;
  const result = document.querySelector('[data-strategy-results]');
  const error = form.querySelector('[data-strategy-error-global]');
  const submit = form.querySelector('[data-strategy-submit]');
  const submitLabel = form.querySelector('[data-strategy-submit-label]');
  const complexSelect = form.querySelector('[data-complex-select]');
  const complexSearch = form.querySelector('[data-complex-search]');
  if (complexSelect && complexSearch) {
    const searchField = form.querySelector('[data-complex-search-field]');
    const matchCount = form.querySelector('[data-complex-match-count]');
    const allOptions = [...complexSelect.options].map(option => option.cloneNode(true));
    const normalize = value => value.normalize('NFKC').toLocaleLowerCase('ru').replace(/ё/g, 'е').trim();
    searchField.hidden = false;
    complexSearch.addEventListener('input', () => {
      const query = normalize(complexSearch.value);
      const selected = complexSelect.value;
      const matches = allOptions.filter(option => option.value && normalize(option.dataset.searchText || option.textContent).includes(query));
      const visible = allOptions.filter(option => !option.value || option.value === selected || matches.includes(option));
      complexSelect.replaceChildren(...visible.map(option => option.cloneNode(true)));
      complexSelect.value = selected;
      matchCount.textContent = query ? `Найдено: ${matches.length}` : '';
    });
  }
  if (typeof fetch !== 'function') return;
  let sequence = 0;
  let controller;
  const idle = () => {
    submit.disabled = false;
    submitLabel.textContent = 'Рассчитать';
    form.removeAttribute('aria-busy');
  };
  const clearResult = event => {
    // Searching the catalogue does not change the chosen complex or budget.
    if (event.target !== form.elements.namedItem('budget') && event.target !== complexSelect) return;
    sequence += 1;
    if (controller) controller.abort();
    result.replaceChildren();
    error.hidden = true;
    error.textContent = '';
    idle();
  };
  form.addEventListener('input', clearResult);
  form.addEventListener('change', clearResult);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const current = ++sequence;
    if (controller) controller.abort();
    controller = new AbortController();
    submit.disabled = true;
    submitLabel.textContent = 'Считаем…';
    form.setAttribute('aria-busy', 'true');
    error.hidden = true;
    error.textContent = '';
    // Numbers from the previous request must not survive a changed calculation.
    result.replaceChildren();
    try {
      const response = await fetch(form.action.split('#')[0], {
        method: 'POST', body: new URLSearchParams(new FormData(form)),
        headers: {Accept: 'application/json'}, credentials: 'same-origin',
        cache: 'no-store', signal: controller.signal
      });
      const data = await response.json();
      if (current !== sequence) return;
      if (!response.ok || data.error) throw new Error(data.error || 'Проверьте сумму и выбранный ЖК.');
      if (typeof data.html !== 'string') throw new Error('Не удалось получить расчёт. Попробуйте ещё раз.');
      // This fragment is rendered by auto-escaping Jinja on our own endpoint.
      result.innerHTML = data.html;
      const ready = result.querySelector('[data-strategy-ready]');
      if (ready) ready.focus({preventScroll: true});
    } catch (failure) {
      if (current !== sequence || failure.name === 'AbortError') return;
      error.textContent = failure instanceof TypeError ? 'Не удалось получить расчёт. Попробуйте ещё раз.' : failure.message;
      error.hidden = false;
      result.replaceChildren();
    } finally {
      if (current === sequence) idle();
    }
  });
})();
