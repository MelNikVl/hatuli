(() => {
  'use strict';
  const root = document.querySelector('.sublease-page');
  if (!root) return;
  const byId = id => document.getElementById(id), form = byId('sublease-form');
  const money = value => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0}).format(value) + ' ₸';
  const pct = value => value === null ? 'Не определяется' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 1}).format(value * 100) + '%';
  let generation = 0, controller, lastQuery = null, page = 1, total = 0, map, markers, invalidField;
  const fieldNames = {district: 'Район', rooms: 'Комнатность', furnishing: 'Мебель',
    rent_month: 'Аренда у собственника', nightly: 'Цена за ночь', occupancy: 'Сценарий загрузки',
    avg_stay_nights: 'Среднее проживание', cleaning_per_turn: 'Уборка при смене гостя',
    consumables_per_night: 'Расходники за занятую ночь', utilities_month: 'Коммуналка и интернет',
    platform_fee_pct: 'Комиссия', tax_pct: 'Налог', furnish_capex: 'Мебель и оснащение',
    capex_months: 'Срок распределения оснащения', deposit_months: 'Возвратный депозит',
    max_rent: 'Максимальная аренда', strategy: 'Стратегия', vacancy_fraction: 'Планируемый простой',
    turnovers_per_year: 'Смен арендатора в год'};

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function status(text) { byId('sublease-status').textContent = text; }
  function showResult(box) {
    if (box.hidden) return;
    const headerHeight = document.querySelector('.pub-header')?.getBoundingClientRect().height || 80;
    box.style.setProperty('--sublease-scroll-offset', `${Math.ceil(headerHeight) + 16}px`);
    box.scrollIntoView({block: 'start', behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
  }
  function clearScanner() {
    lastQuery = null;
    byId('sublease-candidates')?.replaceChildren();
    for (const id of ['sublease-csv', 'sublease-next', 'sublease-previous']) {
      const button = byId(id);
      if (button) button.disabled = true;
    }
    if (byId('sublease-page')) byId('sublease-page').textContent = '';
    if (byId('sublease-inventory')) byId('sublease-inventory').hidden = true;
    markers?.clearLayers();
  }
  function clearFieldError() {
    invalidField?.removeAttribute('aria-invalid');
    invalidField?.removeAttribute('aria-errormessage');
    invalidField = null;
  }
  function invalidate({preserveSegment = false} = {}) {
    generation += 1;
    controller?.abort();
    clearFieldError();
    for (const id of ['sublease-calculation', 'sublease-segment']) {
      if (preserveSegment && id === 'sublease-segment') continue;
      byId(id).hidden = true;
      byId(id).replaceChildren();
    }
    clearScanner();
    status('');
  }
  function values() {
    const result = {};
    for (const [key, value] of new FormData(form)) {
      if (value === '') continue;
      const input = form.elements[key];
      result[key] = input?.dataset.percent !== undefined && Number.isFinite(Number(value)) ? String(Number(value) / 100) : value;
    }
    return result;
  }
  function sourceText(source) {
    if (!source || source.kind === 'manual') return 'Ставки введены вручную.';
    const level = {complex: 'ЖК', district: 'район', hex: 'гекс', гекс: 'гекс'}[source.level] || 'сегмент';
    return `Цены объявлений · ${level} · ${source.n ?? source.sample_count} объявлений за ${source.window_days} дней · обновлено ${new Date(source.updated_at).toLocaleString('ru-RU')}`;
  }
  function errorMessage(data) {
    return data.errors ? Object.entries(data.errors).map(([name, message]) => {
      const input = form.elements[name];
      // API fractions are displayed as percentages in this form.
      if (input?.dataset.percent !== undefined && /^Допустимо от /.test(message)) message = 'Укажите значение от 0 до 100%.';
      return fieldNames[name] ? `${fieldNames[name]}: ${message}` : message;
    }).join(' ') : data.message || 'Не удалось получить расчёт. Проверьте вводные и попробуйте снова.';
  }
  function reportError(data) {
    clearFieldError();
    status(errorMessage(data));
    const input = Object.keys(data.errors || {}).map(name => form.elements[name]).find(field => field && !field.disabled);
    if (input) {
      invalidField = input;
      input.setAttribute('aria-invalid', 'true');
      input.setAttribute('aria-errormessage', 'sublease-status');
      // Scanner controls can be inside a collapsed details element.
      const details = input.closest('details');
      if (details) details.open = true;
      input.focus();
    }
  }
  async function api(url, options = {}) {
    controller?.abort();
    controller = new AbortController();
    const own = ++generation;
    clearFieldError();
    status('Считаем…');
    try {
      const response = await fetch(url, {...options, signal: controller.signal, headers: {'Content-Type': 'application/json', ...(options.headers || {})}});
      const data = await response.json();
      if (own !== generation) return null;
      if (!response.ok) {
        if (data.error === 'narrower_filters_required') clearScanner();
        reportError(data);
        return null;
      }
      status('');
      return data;
    } catch (error) {
      if (error.name !== 'AbortError' && own === generation) status(error.message);
      return null;
    }
  }
  function renderEconomics(result) {
    const box = byId('sublease-calculation');
    box.replaceChildren(element('h2', 'Загрузка для безубыточности'));
    const metric = element('div', result.breakeven_occupancy === null ? 'Расходы не покрываются' : pct(result.breakeven_occupancy), 'sublease-metric');
    metric.append(element('small', result.breakeven_possible ? 'Расходы покрываются при этой сценарной загрузке' : 'При этих условиях расходы не покрываются в пределах 100% загрузки'));
    box.append(metric, element('p', `Стартовый капитал ${money(result.startup_capital)}. Из него возвратный депозит ${money(result.refundable_deposit)}.`));
    const scenarios = element('div', undefined, 'sublease-cards');
    for (const item of result.scenarios || []) {
      const card = element('div', undefined, 'sublease-scenario');
      card.append(element('span', `Загрузка ${pct(item.params_used.occupancy)}`), element('strong', money(item.margin)), element('span', 'маржа за условный месяц'));
      scenarios.append(card);
    }
    box.append(scenarios, element('p', `Ваш сценарий ${pct(result.params_used.occupancy)}: выручка ${money(result.revenue)}, расходы ${money(result.costs)}, маржа ${money(result.margin)}.`), element('p', sourceText(result.index_source), 'sublease-provenance'));
    box.hidden = false;
    showResult(box);
  }
  form.addEventListener('input', invalidate);
  form.addEventListener('change', invalidate);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const snapshot = values();
    const result = await api('/api/rental/calc', {method: 'POST', body: JSON.stringify(snapshot)});
    if (result) renderEconomics(result);
  });
  const capex = form.elements.furnish_capex;
  function updateFurnitureInput() {
    // Preserve the entered budget. Disabled controls are omitted from
    // FormData; only the backend's explicit full-furnishing rule supplies 0.
    capex.disabled = form.elements.furnishing.value === 'full';
  }
  form.elements.furnishing.addEventListener('change', updateFurnitureInput);
  updateFurnitureInput();
  byId('sublease-segment-button').onclick = async () => {
    const result = await api('/api/rental/segment?' + new URLSearchParams(values()));
    if (!result) return;
    const box = byId('sublease-segment');
    box.replaceChildren();
    const present = Object.values(result.indices).some(Boolean);
    box.hidden = !present;
    if (!present) return;
    box.append(element('h2', 'Цены сегмента'));
    function usePrices(prices) {
      // Only the explicitly accepted prices change. Keep this ready segment
      // available so the other rate can be copied without another request.
      invalidate({preserveSegment: true});
      for (const [name, price] of Object.entries(prices)) form.elements[name].value = price;
      status('Цены перенесены в расчёт. Проверьте свои расходы и нажмите «Рассчитать безубыточность».');
    }
    if (result.indices.monthly && result.indices.daily) {
      const both = element('button', 'Использовать обе ставки');
      both.type = 'button';
      both.onclick = () => usePrices({rent_month: result.indices.monthly.median_price, nightly: result.indices.daily.median_price});
      box.append(both);
    }
    for (const [kind, title] of [['monthly', 'Помесячно'], ['daily', 'За сутки'], ['furnished_monthly', 'Помесячно с полной мебелью']]) {
      const index = result.indices[kind];
      if (!index) continue;
      box.append(element('h3', `${title}: ${money(index.median_price)}`), element('p', `Середина выборки: ${money(index.p25_price)}–${money(index.p75_price)}`), element('p', sourceText(index), 'sublease-provenance'));
      if (kind === 'monthly' || kind === 'daily') {
        const button = element('button', kind === 'monthly' ? 'Использовать аренду в расчёте' : 'Использовать цену за ночь');
        button.type = 'button';
        button.onclick = () => usePrices({[kind === 'monthly' ? 'rent_month' : 'nightly']: index.median_price});
        box.append(button);
      }
    }
    showResult(box);
  };
  if (root.dataset.accessLocked === 'true') return;

  function drawMap(items) {
    const container = byId('sublease-map'), located = items.filter(item => item.location);
    container.hidden = !window.L || !located.length;
    if (container.hidden) return;
    if (!map) {
      map = L.map(container).setView([51.128, 71.43], 12);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '© OpenStreetMap', referrerPolicy: 'origin'}).addTo(map);
      markers = L.layerGroup().addTo(map);
    }
    markers.clearLayers();
    const points = [];
    for (const item of located) {
      const point = [item.location.lat, item.location.lon], popup = element('div'), link = element('a', `${item.rooms}к · ${money(item.price)}`);
      link.href = item.source_url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      popup.append(link, element('p', `Безубыточность: ${pct(item.economics.breakeven_occupancy)}`));
      L.marker(point).addTo(markers).bindPopup(popup);
      points.push(point);
    }
    map.invalidateSize();
    map.fitBounds(points, {padding: [20, 20], maxZoom: 15});
  }
  function renderCandidates(data) {
    clearScanner();
    if (!data.items.length) return false;
    const tbody = byId('sublease-candidates');
    const labels = {no_photo: 'нет фото', furnishing_unknown: 'мебель не подтверждена', unusually_low_asking_rent: 'цена более чем на 30% ниже медианы', high_daily_competition: 'предложение посуточных выше P75'};
    for (const item of data.items) {
      const tr = element('tr'), main = element('td'), link = element('a', `${item.rooms === 0 ? 'Студия' : item.rooms + 'к'} · ${item.complex_name || item.district || 'Астана'}`);
      link.href = item.source_url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      main.append(link);
      if (item.address) main.append(element('small', item.address));
      const observedFlags = item.risk_flags.map(flag => labels[flag]).filter(Boolean);
      if (observedFlags.length) main.append(element('small', observedFlags.join(' · ')));
      main.append(element('small', sourceText(item.economics.index_source)));
      tr.append(main, ...[money(item.price), pct(item.economics.breakeven_occupancy), money(item.economics.margin), money(item.economics.startup_capital)].map(value => element('td', value)));
      tbody.append(tr);
    }
    page = data.page; total = data.total;
    byId('sublease-page').textContent = `Страница ${page} · вариантов ${total}`;
    byId('sublease-previous').disabled = page <= 1;
    byId('sublease-next').disabled = page * 25 >= total;
    byId('sublease-csv').disabled = false;
    byId('sublease-candidate-note').textContent = 'Сравнение всех подходящих вариантов по выбранным фильтрам. Цены — из объявлений, загрузка и доход — ваш сценарий.';
    byId('sublease-inventory').hidden = false;
    drawMap(data.items);
    return true;
  }
  async function scan(targetPage, snapshot) {
    const result = await api('/api/rental/scanner?' + new URLSearchParams({...snapshot, page: String(targetPage)}));
    if (result && renderCandidates(result)) {
      lastQuery = {...snapshot};
      showResult(byId('sublease-inventory'));
    }
  }
  byId('sublease-scan-button').onclick = () => scan(1, values());
  byId('sublease-previous').onclick = () => lastQuery && scan(page - 1, lastQuery);
  byId('sublease-next').onclick = () => lastQuery && scan(page + 1, lastQuery);
  byId('sublease-csv').onclick = async () => {
    if (!lastQuery) return;
    const snapshot = {...lastQuery}, own = ++generation;
    controller?.abort();
    controller = new AbortController();
    try {
      const response = await fetch('/api/rental/scanner.csv?' + new URLSearchParams(snapshot), {signal: controller.signal});
      if (own !== generation) return;
      if (!response.ok) {
        let data = {};
        try { data = await response.json(); } catch {}
        if (own !== generation) return;
        if (data.error === 'narrower_filters_required') clearScanner();
        reportError(data);
        return;
      }
      const blob = await response.blob();
      if (own !== generation) return;
      const url = URL.createObjectURL(blob), link = element('a');
      link.href = url; link.download = 'hatuli-rental-scenarios.csv'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (error.name !== 'AbortError' && own === generation) status(error.message);
    }
  };
})();
