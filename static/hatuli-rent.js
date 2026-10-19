/* Rental tools: explicit user scenarios and a private, consented inquiry. */
(() => {
  'use strict';
  const form = document.querySelector('#rent-scenario-form');
  if (!form) return;
  const money = n => new Intl.NumberFormat('ru-KZ', {maximumFractionDigits: 0}).format(Number(n)) + ' ₸';
  const number = n => new Intl.NumberFormat('ru-KZ', {maximumFractionDigits: 1}).format(Number(n));
  const setText = (selector, text) => { const node = document.querySelector(selector); if (node) node.textContent = text; };
  let report = null;
  let reportText = '';
  let generation = 0;
  let pending = null;
  const errorBox = document.querySelector('#rent-scenario-error');
  const resultBox = document.querySelector('#rent-scenario-result');
  const invalidateScenario = () => { generation++; pending?.abort(); resultBox.hidden = true; report = null; reportText = ''; };
  form.addEventListener('input', invalidateScenario);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const button = form.querySelector('[type=submit]');
    const inputs = Object.fromEntries(new FormData(form));
    const currentGeneration = ++generation;
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    button.disabled = true; errorBox.hidden = true; resultBox.hidden = true;
    try {
      const response = await fetch(form.action, {method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json'}, body: JSON.stringify(inputs), credentials: 'same-origin', signal: controller.signal});
      const data = await response.json();
      if (currentGeneration !== generation) return;
      if (!response.ok) throw new Error(data.errors ? Object.values(data.errors).join(' ') : 'Не удалось посчитать. Проверьте поля и попробуйте ещё раз.');
      report = data;
      const a = Number(data.scenario_a.net_horizon), b = Number(data.scenario_b.net_horizon);
      const delta = Number(data.difference);
      const title = delta > 0 ? `Ожидание даёт на ${money(delta)} больше` : delta < 0 ? `Сдать сейчас — на ${money(-delta)} больше` : 'Доход обоих вариантов одинаковый';
      setText('#rent-result-title', title); setText('[data-net-a]', money(a)); setText('[data-net-b]', money(b));
      const scale = Math.max(a, b, 1);
      document.querySelector('[data-bar-a]').style.width = `${Math.max(0, a) / scale * 100}%`;
      document.querySelector('[data-bar-b]').style.width = `${Math.max(0, b) / scale * 100}%`;
      const breakEven = data.max_whole_waiting_days;
      const breakText = breakEven == null ? 'При этой ставке Б ожидание не увеличит доход по сравнению с вариантом А.' : `Можно ждать не больше ${number(breakEven)} полных дн. — тогда доход Б не ниже А. При большем ожидании вариант А даст больше за выбранный период.`;
      setText('[data-break-even]', breakText);
      setText('[data-forgone]', `За время ожидания не получено аренды по ставке А: ${money(data.forgone_rent)}.`);
      setText('[data-deposit]', `Возвратный депозит ${money(data.refundable_deposit)} не включён в доход.`);
      reportText = ['Hatuli · Сравнение аренды', new Date().toLocaleDateString('ru-KZ'), '', `Ставка А: ${money(inputs.monthly_rent_a)} / мес., без ожидания`, `Ставка Б: ${money(inputs.monthly_rent_b)} / мес., ожидание ${inputs.waiting_days} дн.`, `Период: ${inputs.horizon_months} мес. по 30 дней`, `Расходы владельца: ${money(inputs.monthly_owner_expenses)} / мес.`, `Разовые расходы: ${money(inputs.oneoff_expenses)}`, `Возвратный депозит (не доход): ${money(inputs.refundable_deposit)}`, '', `Доход А после указанных расходов: ${money(a)}`, `Доход Б после указанных расходов: ${money(b)}`, title, breakText, '', 'Сценарий по вашим условиям, не прогноз фактической ставки и срока сдачи.', 'Расходы владельца начислены за весь период; после заселения предполагается непрерывная аренда.', 'https://hatuli.ai-groundtruth.com/rent'].join('\n');
      resultBox.hidden = false;
    } catch (error) { if (error.name !== 'AbortError' && currentGeneration === generation) { errorBox.textContent = error.message || 'Расчёт недоступен. Попробуйте позже.'; errorBox.hidden = false; } }
    finally { if (pending === controller) { pending = null; button.disabled = false; } }
  });
  document.querySelector('#rent-download').addEventListener('click', () => {
    if (!report || !reportText) return;
    const url = URL.createObjectURL(new Blob(['\ufeff' + reportText], {type: 'text/plain;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = 'hatuli-rent-scenario.txt'; document.body.append(link); link.click(); link.remove(); URL.revokeObjectURL(url);
  });
  let examples = [];
  try { examples = JSON.parse(document.querySelector('#rent-examples')?.textContent || '[]'); } catch (_) { /* optional evidence stays hidden */ }
  const chooser = document.querySelector('#rent-example');
  if (chooser) {
    chooser.addEventListener('change', () => {
      const example = examples.find(item => String(item.key) === chooser.value);
      document.querySelector('#rent-example-result').hidden = !example;
      if (!example) return;
      setText('[data-market-range]', `${money(example.monthly_low)}–${money(example.monthly_high)} / мес.`);
      setText('[data-market-basis]', `${example.complex_name} · ${example.title}. Медиана ${money(example.monthly_median)}. ${example.sample_count} независимых предложений в этом ЖК, та же комнатность, площадь ±15%. Наблюдение: ${example.date_label}. Диапазон — средние 50% ставок.`);
    });
    document.querySelector('#rent-use-market').addEventListener('click', () => {
      const example = examples.find(item => String(item.key) === chooser.value);
      if (!example) return;
      document.querySelector('#rent-a').value = example.monthly_median;
      invalidateScenario(); document.querySelector('#rent-a').focus();
    });
  }
  document.querySelectorAll('[data-request-kind]').forEach(link => link.addEventListener('click', () => { document.querySelector('#rent-kind').value = link.dataset.requestKind; }));
  const requestForm = document.querySelector('#rent-request-form');
  requestForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (!requestForm.reportValidity()) return;
    const button = requestForm.querySelector('[type=submit]'), status = document.querySelector('#rent-request-status');
    button.disabled = true; status.hidden = true;
    try {
      const fields = Object.fromEntries(new FormData(requestForm));
      const response = await fetch(requestForm.action, {method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json'}, credentials: 'same-origin', body: JSON.stringify(fields)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.errors ? Object.values(data.errors).join(' ') : data.message || (response.status === 403 ? 'Обновите страницу: срок действия формы истёк.' : 'Не удалось принять запрос. Попробуйте позже.'));
      status.textContent = `Запрос принят. Номер: ${data.reference}. Команда рассмотрит задачу и ответит по указанному контакту.`;
      requestForm.querySelector('[name=brief]').value = ''; requestForm.querySelector('[name=consent]').checked = false;
    } catch (error) { status.textContent = error.message || 'Не удалось отправить запрос.'; }
    finally { status.hidden = false; button.disabled = false; }
  });
})();
