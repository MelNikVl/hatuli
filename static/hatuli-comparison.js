/* The server owns the financial model. This file only submits inputs and displays it. */
(function () {
  'use strict';
  var page = document.querySelector('.mc-page');
  if (!page) return;
  var form = page.querySelector('[data-mc-calculator]');
  var result = page.querySelector('[data-mc-result]');
  var submit = page.querySelector('[data-mc-submit]');
  var error = page.querySelector('[data-mc-error]');
  var mode = page.querySelector('#mc-mode');
  var moneyFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
  var percentFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
  var names = {
    price: 'Цена квартиры', repair_cost: 'Ремонт', buy_cost: 'Расходы при покупке',
    monthly_rent: 'Аренда', annual_owner_cost: 'Расходы владельца', vacancy_months: 'Месяцы без арендатора',
    annual_growth_pct: 'Рост цены', wait_months: 'Срок до готовности', sell_cost_pct: 'Расходы при продаже', mode: 'Цель покупки'
  };
  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function money(value) { return finite(value) ? moneyFormat.format(value) + ' ₸' : '—'; }
  function percent(value) { return finite(value) ? percentFormat.format(value) + '%' : '—'; }
  function node(tag, className, text) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function updateMode() {
    if (!mode) return;
    var renting = mode.value === 'rent_out';
    var label = page.querySelector('[data-mc-rent-label]');
    var help = page.querySelector('[data-mc-rent-help]');
    var vacancy = page.querySelector('[data-mc-vacancy-field]');
    if (label) label.textContent = renting ? 'Арендный доход в месяц' : 'Аренда жилья в месяц';
    if (help) help.textContent = renting ? 'До вычета расходов и месяцев без арендатора.' : 'Сколько платите за жильё, пока ждёте покупки.';
    if (vacancy) vacancy.hidden = !renting;
  }
  if (mode) mode.addEventListener('change', updateMode);
  updateMode();

  function balanceRow(label, amount, caption, kind) {
    var row = node('div', 'mc-balance-row');
    row.dataset.kind = kind;
    var heading = node('div', 'mc-balance-caption');
    heading.append(node('span', '', label), node('strong', '', money(amount)));
    var bar = node('div', 'mc-balance-bar');
    bar.setAttribute('aria-hidden', 'true');
    var fill = node('span');
    fill.dataset.mcBalance = String(amount);
    bar.append(fill);
    row.append(heading, bar, node('small', '', caption));
    return row;
  }
  function resizeBars() {
    var bars = Array.from(page.querySelectorAll('[data-mc-balance]'));
    var maximum = Math.max.apply(null, bars.map(function (bar) { return Math.max(0, Number(bar.dataset.mcBalance) || 0); }).concat([1]));
    bars.forEach(function (bar) {
      var value = Number(bar.dataset.mcBalance) || 0;
      bar.style.width = Math.max(0, Math.min(100, value / maximum * 100)) + '%';
    });
  }
  function updateBankCards(banks) {
    page.querySelectorAll('[data-mc-bank-outcome]').forEach(function (outcome) {
      outcome.replaceChildren();
      outcome.hidden = true;
    });
    banks.forEach(function (bank) {
      var card = Array.from(page.querySelectorAll('[data-mc-bank]')).find(function (candidate) {
        return candidate.dataset.mcBank === String(bank.key || bank.bank);
      });
      if (!card) return;
      var outcome = card.querySelector('[data-mc-bank-outcome]');
      if (!outcome) return;
      outcome.hidden = false;
      outcome.append(node('span', '', 'Через год после расходов'), node('strong', '', money(bank.deposit_cash_after_rent)));
      if (finite(bank.property_advantage)) {
        var advantage = bank.property_advantage;
        var text = advantage === 0 ? 'В вашем сценарии результат равен квартире.' : advantage > 0
          ? 'Квартира выше на ' + money(advantage) + ' в вашем сценарии.'
          : 'Вклад выше на ' + money(-advantage) + ' в вашем сценарии.';
        outcome.append(node('span', 'mc-bank-meta', text));
      }
      if (finite(bank.break_even_growth_pct)) {
        outcome.append(node('span', 'mc-bank-meta', 'Порог роста цены квартиры: ' + percent(bank.break_even_growth_pct) + ' за год.'));
      }
      if (finite(bank.future_purchase_surplus)) {
        var surplus = bank.future_purchase_surplus;
        outcome.append(node('span', 'mc-bank-meta', surplus >= 0
          ? 'После покупки через год останется ' + money(surplus) + '.'
          : 'Для такой же покупки через год не хватит ' + money(-surplus) + '.'));
      }
      if (finite(bank.unguaranteed_amount) && bank.unguaranteed_amount > 0) {
        outcome.append(node('span', 'mc-bank-meta', 'Сверх лимита гарантии: ' + money(bank.unguaranteed_amount) + '.'));
      }
      (bank.warnings || []).forEach(function (warning) { outcome.append(node('span', 'mc-bank-meta', String(warning))); });
    });
  }
  function eligibleBanks(calculation) {
    return (calculation.banks || []).filter(function (bank) {
      return bank.eligible !== false && finite(bank.deposit_cash_after_rent);
    });
  }
  function renderResult(calculation) {
    if (!result || !calculation || !finite(calculation.initial_capital) || !calculation.property || !finite(calculation.property.wealth)) {
      throw new Error('Не удалось получить полный расчёт. Попробуйте ещё раз.');
    }
    var banks = eligibleBanks(calculation);
    var assumptions = calculation.assumptions || {};
    var renting = assumptions.mode === 'rent_out';
    var capital = node('dl', 'mc-capital');
    capital.append(node('dt', '', 'Одинаковая стартовая сумма'), node('dd', '', money(calculation.initial_capital)));
    var fragment = document.createDocumentFragment();
    fragment.append(capital, node('p', 'mc-eyebrow', 'Через 12 месяцев · ваш сценарий'), node('h3', '', 'Сколько останется после расходов'));
    fragment.append(node('p', 'mc-result-intro', 'Стоимость квартиры показана с учётом ваших расходов и ' + (renting ? 'арендного дохода' : 'аренды до готовности') + '. Вклад — после ' + (renting ? '12 месяцев.' : 'аренды жилья на время ожидания.')));
    fragment.append(balanceRow('Квартира', calculation.property.wealth, 'Стоимость жилья' + (renting ? ' + доход от аренды' : '') + ' − расходы', 'property'));
    banks.forEach(function (bank) {
      fragment.append(balanceRow(bank.bank + ' · вклад', bank.deposit_cash_after_rent,
        (renting ? 'Через 12 месяцев' : 'После аренды на время ожидания') + ' · ' + bank.product, 'bank'));
    });
    var verdict = node('div', 'mc-scenario-verdict');
    if (banks.length) {
      var strongest = banks.slice().sort(function (a, b) { return b.deposit_cash_after_rent - a.deposit_cash_after_rent; })[0];
      var difference = calculation.property.wealth - strongest.deposit_cash_after_rent;
      verdict.append(node('strong', '', difference === 0 ? 'Квартира и самый доходный подходящий вклад равны в этом сценарии.'
        : difference > 0 ? 'Квартира выше на ' + money(difference) + ' в этом сценарии.'
        : 'Самый доходный подходящий вклад выше на ' + money(-difference) + '.'));
      var explanation = 'Сравнение с ' + strongest.bank + ' · ' + strongest.product + '.';
      if (finite(strongest.break_even_growth_pct)) explanation += ' Для равного результата квартире нужен рост цены ' + percent(strongest.break_even_growth_pct) + ' за год.';
      explanation += ' Измените рост, ремонт или аренду и проверьте другой сценарий.';
      verdict.append(node('p', '', explanation));
    } else {
      verdict.append(node('strong', '', 'Для сравнения нужен подтверждённый вклад на 12 месяцев'), node('p', '', 'Нет свежей подходящей ставки или не подтверждены условия. Доходность на другой срок не переносим на год.'));
    }
    fragment.append(verdict);
    if (calculation.warnings && calculation.warnings.length) {
      var details = node('details', 'mc-result-details');
      var list = node('ul');
      details.append(node('summary', '', 'Что учтено в сценарии'));
      calculation.warnings.forEach(function (warning) { list.append(node('li', '', String(warning))); });
      details.append(list);
      fragment.append(details);
    }
    result.replaceChildren(fragment);
    updateBankCards(banks);
    resizeBars();
  }
  function errorMessage(data) {
    if (typeof data.error === 'string') return data.error;
    if (typeof data.detail === 'string') return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map(function (item) {
        var field = (item.loc || []).slice(-1)[0];
        return (names[field] || 'Параметр') + ': проверьте значение и допустимый диапазон.';
      }).filter(function (value, index, all) { return all.indexOf(value) === index; }).join(' ');
    }
    return 'Не удалось получить расчёт. Попробуйте ещё раз.';
  }
  if (form && result && window.fetch) {
    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (!form.reportValidity()) return;
      if (error) error.hidden = true;
      var payload = {};
      var data = new FormData(form);
      Object.keys(names).forEach(function (name) { payload[name] = name === 'mode' ? String(data.get(name)) : Number(data.get(name)); });
      if (payload.mode !== 'rent_out') payload.vacancy_months = 0;
      var original = submit ? submit.textContent : '';
      if (submit) { submit.disabled = true; submit.textContent = 'Считаем…'; }
      result.setAttribute('aria-busy', 'true');
      try {
        var response = await fetch('/api/public/compare-investment', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), credentials: 'same-origin'
        });
        var calculation = await response.json();
        if (!response.ok) throw new Error(errorMessage(calculation));
        renderResult(calculation);
        result.setAttribute('tabindex', '-1');
        result.focus({ preventScroll: true });
        if (window.matchMedia('(max-width: 720px)').matches) result.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
      } catch (failure) {
        if (error) { error.textContent = failure instanceof Error ? failure.message : 'Не удалось получить расчёт. Попробуйте ещё раз.'; error.hidden = false; }
      } finally {
        result.removeAttribute('aria-busy');
        if (submit) { submit.disabled = false; submit.textContent = original; }
      }
    });
  }
  var initial = document.querySelector('#mc-initial-calculation');
  if (initial) {
    try {
      var calculation = JSON.parse(initial.textContent);
      renderResult(calculation);
    } catch (_) { resizeBars(); }
  } else { resizeBars(); }
})();
