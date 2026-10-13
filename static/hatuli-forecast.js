/* Display only: predictions, gates and intervals are calculated by the server. */
(function () {
  'use strict';
  var moneyFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
  var percentFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
  var unavailable = {
    insufficient_history: 'Пока недостаточно истории',
    insufficient_analogs: 'Пока недостаточно похожих квартир',
    quality_gate_failed: 'Точность пока недостаточна',
    stale_model: 'Ждём свежий расчёт',
    unavailable: 'Прогноз пока недоступен'
  };
  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function count(value) { return Number.isInteger(value) && value >= 0 ? value : null; }
  function money(value) { return finite(value) && value > 0 ? moneyFormat.format(value) + ' ₸' : '—'; }
  function pct(value) { return finite(value) && value >= 0 ? percentFormat.format(value) + '%' : null; }
  function node(tag, className, text) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function date(value) {
    if (typeof value !== 'string') return null;
    var match = /^(\d{4})-(\d{2})-(\d{2})(?:$|T)/.exec(value);
    if (!match) return null;
    var parsed = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
    if (parsed.getUTCFullYear() !== Number(match[1]) || parsed.getUTCMonth() + 1 !== Number(match[2]) || parsed.getUTCDate() !== Number(match[3])) return null;
    return match[3] + '.' + match[2] + '.' + match[1];
  }
  function days(number) {
    var mod100 = number % 100, mod10 = number % 10;
    return number + ' ' + (mod10 === 1 && mod100 !== 11 ? 'день' : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14) ? 'дня' : 'дней');
  }
  function intervalValid(data) {
    return data && finite(data.estimate) && data.estimate > 0 && finite(data.low) && data.low > 0 &&
      finite(data.high) && data.low <= data.estimate && data.estimate <= data.high;
  }
  function description(title, data, ready, fallback) {
    var block = node('div', 'pf-value');
    block.append(node('dt', '', title));
    var value = node('dd');
    if (ready) {
      value.append(node('strong', 'pf-amount', money(data.estimate)));
      value.append(node('span', 'pf-range', 'Расчётный диапазон: ' + money(data.low) + ' – ' + money(data.high)));
    } else {
      value.append(node('strong', 'pf-unavailable', unavailable[data.status] || fallback));
      if (typeof data.reason === 'string' && data.reason) value.append(node('p', 'pf-reason', data.reason));
    }
    block.append(value);
    return block;
  }
  function render(payload, options) {
    options = options || {};
    payload = payload && typeof payload === 'object' ? payload : {};
    var forecasts = Array.isArray(payload.forecasts) ? payload.forecasts.filter(function (item) { return item && typeof item === 'object'; }) : [];
    if (!forecasts.length) forecasts = [payload.forecast && typeof payload.forecast === 'object' ? payload.forecast : payload];
    var market = payload.market_now && typeof payload.market_now === 'object' ? payload.market_now : null;
    function ready(forecast) {
      var validation = forecast.validation && typeof forecast.validation === 'object' ? forecast.validation : {};
      return forecast.status === 'ready' && intervalValid(forecast) && count(forecast.horizon_days) > 0 &&
        count(forecast.historical_analog_count) > 0 && count(validation.sample_size) > 0 && pct(validation.mape_pct) !== null &&
        date(forecast.as_of || payload.as_of) && date(forecast.target_date);
    }
    var availableForecasts = forecasts.filter(ready);
    var missingForecasts = forecasts.filter(function (forecast) { return !ready(forecast); });
    var section = node('section', 'hatuli-forecast' + (options.compact ? ' hatuli-forecast--compact' : '') + (options.modal ? ' hatuli-forecast--modal' : ''));
    section.setAttribute('aria-label', 'Оценка цены и прогноз');
    section.dataset.forecastStatus = availableForecasts.length ? 'ready' : missingForecasts[0].status === 'ready' ? 'unavailable' : missingForecasts[0].status || 'unavailable';
    var heading = node('div', 'pf-heading');
    heading.append(node('h4', '', 'Что будет с ценой?'));
    var asOfValue = payload.as_of || forecasts[0].as_of;
    var asOf = date(asOfValue);
    if (asOf) {
      var time = node('time', 'pf-date', 'Расчёт от ' + asOf);
      time.dateTime = asOfValue.slice(0, 10);
      heading.append(time);
    }
    section.append(heading);
    var values = node('dl', 'pf-values');
    if (market) {
      var marketReady = market.status === 'ready' && intervalValid(market) && count(market.analog_count) > 0;
      var marketBlock = description('Оценка рынка сегодня', market, marketReady, 'Пока недостаточно аналогов');
      if (marketReady) {
        marketBlock.append(node('p', 'pf-caption', 'Похожие предложения: ' + market.analog_count + (typeof market.basis === 'string' && market.basis ? ' · ' + market.basis : '')));
      }
      values.append(marketBlock);
    }
    availableForecasts.forEach(function (forecast) {
      var forecastTitle = 'Цена предложения через ' + days(forecast.horizon_days);
      var forecastBlock = description(forecastTitle, forecast, true, unavailable.unavailable);
      forecastBlock.append(node('p', 'pf-caption', 'К ' + date(forecast.target_date) + ' · похожих квартир в истории: ' + forecast.historical_analog_count));
      values.append(forecastBlock);
    });
    if (!availableForecasts.length) values.append(description('Прогноз цены предложения', missingForecasts[0], false, unavailable.unavailable));
    section.append(values);
    if (availableForecasts.length && missingForecasts.length) {
      var labels = missingForecasts.map(function (forecast) { return count(forecast.horizon_days) > 0 ? days(forecast.horizon_days) : null; }).filter(Boolean);
      if (labels.length) section.append(node('p', 'pf-horizons', 'На ' + labels.join(', ') + ' надёжного прогноза пока нет.'));
    }
    var detail = node('details', 'pf-method');
    detail.append(node('summary', '', 'На чём основан расчёт'));
    var body = node('div', 'pf-method-body');
    var methodology = payload.methodology_label || forecasts[0].methodology_label;
    if (typeof methodology === 'string' && methodology) body.append(node('p', '', methodology));
    availableForecasts.forEach(function (forecast) {
      var validation = forecast.validation;
      body.append(node('p', '', 'Горизонт ' + days(forecast.horizon_days) + ': проверка на прошлых периодах, ' + validation.sample_size + ' наблюдений. Средняя процентная ошибка: ' + pct(validation.mape_pct) + '.'));
      if (pct(validation.baseline_mape_pct) !== null) body.append(node('p', '', 'Ошибка простого ориентира «цена не изменится»: ' + pct(validation.baseline_mape_pct) + '.'));
      if (finite(validation.coverage_pct) && validation.coverage_pct >= 0 && validation.coverage_pct <= 100) body.append(node('p', '', 'В расчётный диапазон попали ' + pct(validation.coverage_pct) + ' наблюдений на проверке.'));
    });
    if (availableForecasts.length) {
      body.append(node('p', '', 'Прогноз относится к наблюдаемой цене объявления, если квартира останется в продаже. У снятого объявления цена сделки неизвестна.'));
    }
    var unsupported = missingForecasts.concat(Array.isArray(payload.unsupported_horizons) ? payload.unsupported_horizons : []);
    unsupported.forEach(function (item) {
      if (!item || !count(item.horizon_days) || item.status === 'ready') return;
      var text = 'На ' + days(item.horizon_days) + ': ' + (unavailable[item.status] || unavailable.unavailable).toLowerCase() + '.';
      if (typeof item.reason === 'string' && item.reason) text += ' ' + item.reason;
      body.append(node('p', '', text));
    });
    var limitations = Array.isArray(payload.limitations) ? payload.limitations : [];
    var safeLimitations = limitations.filter(function (text) { return typeof text === 'string' && text; });
    if (safeLimitations.length) {
      var list = node('ul');
      safeLimitations.forEach(function (text) { list.append(node('li', '', text)); });
      body.append(list);
    }
    if (!body.childNodes.length) body.append(node('p', '', 'Для прогноза нужны похожие квартиры в прошлых периодах и проверка точности на последующих наблюдениях.'));
    detail.append(body);
    section.append(detail, node('p', 'pf-footnote', 'Цены предложений. Это не цена сделки и не прогноз доходности за год.'));
    return section;
  }
  function mount(container, payload, options) {
    if (!container || typeof container.replaceChildren !== 'function') return;
    container.replaceChildren(render(payload, options));
  }
  window.HatuliPriceForecast = Object.freeze({ render: render, mount: mount });
})();
