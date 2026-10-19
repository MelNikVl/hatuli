/* Progressive enhancement only: the same endpoint renders a full HTML fallback. */
(() => {
  'use strict';
  const form = document.querySelector('[data-strategy-form]');
  if (!form) return;
  const result = document.querySelector('[data-strategy-results]');
  const error = form.querySelector('[data-strategy-error-global]');
  const submit = form.querySelector('[data-strategy-submit]');
  const options = [...form.querySelectorAll('[data-strategy]')];
  const syncOption = option => {
    const enabled = option.querySelector('input[type="checkbox"]').checked;
    const fields = option.querySelector('[data-strategy-fields]');
    fields.hidden = !enabled;
    option.querySelectorAll('[data-strategy-input]').forEach(input => {
      input.disabled = !enabled;
      // Server diagnostics handle missing values; avoid native validation
      // trapping keyboard focus inside a collapsed cost group.
      input.required = false;
    });
  };
  options.forEach(option => {
    syncOption(option);
    option.querySelector('input[type="checkbox"]').addEventListener('change', () => syncOption(option));
  });
  let sequence = 0;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const current = ++sequence;
    submit.disabled = true;
    form.setAttribute('aria-busy', 'true');
    error.hidden = true;
    error.textContent = '';
    form.querySelectorAll('[data-strategy-error]').forEach(node => { node.textContent = ''; });
    form.querySelectorAll('[aria-invalid]').forEach(node => { node.removeAttribute('aria-invalid'); node.removeAttribute('aria-describedby'); });
    try {
      const response = await fetch(form.action.split('#')[0], {
        method: 'POST', body: new URLSearchParams(new FormData(form)),
        headers: {Accept: 'application/json'}, credentials: 'same-origin'
      });
      const data = await response.json();
      if (current !== sequence) return;
      if (!response.ok || data.error) throw new Error(data.error || 'Проверьте введённые условия.');
      // This fragment is rendered by auto-escaping Jinja on our own endpoint.
      result.innerHTML = data.html;
      (data.result.diagnostics || []).forEach(diagnostic => {
        const node = [...form.querySelectorAll('[data-strategy-error]')]
          .find(item => item.dataset.strategyError === diagnostic.key);
        if (node) node.textContent = diagnostic.message;
        (diagnostic.fields || []).forEach(field => {
          const input = form.elements.namedItem(`${diagnostic.key}__${field}`);
          if (input && node) {
            input.setAttribute('aria-invalid', 'true');
            input.setAttribute('aria-describedby', node.id);
            const details = input.closest('details');
            if (details) details.open = true;
          }
        });
      });
      const ready = result.querySelector('[data-strategy-ready]');
      if (ready) ready.focus({preventScroll: true});
    } catch (failure) {
      if (current !== sequence) return;
      error.textContent = failure instanceof TypeError ? 'Не удалось получить расчёт. Попробуйте ещё раз.' : failure.message;
      error.hidden = false;
      // Never leave old results beside a failed or changed scenario.
      result.replaceChildren();
    } finally {
      if (current === sequence) { submit.disabled = false; form.removeAttribute('aria-busy'); }
    }
  });
  form.addEventListener('input', () => {
    // Ready numbers belong to the submitted inputs only.
    sequence += 1;
    result.replaceChildren();
    error.hidden = true;
    error.textContent = '';
    submit.disabled = false;
    form.removeAttribute('aria-busy');
  });
})();
