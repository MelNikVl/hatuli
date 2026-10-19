/* Ready monthly asking-rent scenarios in the existing listing modal. */
(function (root) {
  'use strict';
  const escape = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = v => Math.round(v).toLocaleString('ru-KZ') + ' ₸';
  function ready(v, now = Date.now()) {
    if (!v || v.schema_version !== 1 || v.city_scope !== 'astana' || v.asking_prices !== true || v.scenario !== true) return false;
    const s = v.rent_source, p = v.params_used, r = v.rent_range;
    if (!s || !p || s.policy_version !== '3' || s.window_days !== 30 || !['high','medium','low'].includes(v.confidence)) return false;
    const confidence = s.level === 'ЖК' ? 'high' : s.level === 'район' ? 'medium' : ['город','район (все комн.)','город (все комн.)'].includes(s.level) ? 'low' : null;
    if (v.confidence !== confidence) return false;
    const [oldest,newest,updated,computed] = [s.source_oldest_at,s.source_newest_at,s.updated_at,v.computed_at].map(Date.parse);
    if (![oldest,newest,updated,computed].every(Number.isFinite) || oldest < now-72*3600000 || oldest > newest || newest > updated || updated > computed || computed > now || computed < now-26*3600000) return false;
    if (!Number.isInteger(s.n) || s.n < (s.level === 'ЖК' ? 5 : 10)) return false;
    if (!Array.isArray(r) || r.length !== 2 || !r.every(n => Number.isFinite(n) && n > 0) || r[0] > v.est_rent || r[1] < v.est_rent) return false;
    return v.est_rent >= 50000 && v.est_rent <= 2000000 && v.net_yield_pct >= -100 && v.net_yield_pct <= 100 && [v.net_yield_pct,v.gross_yield_pct,v.est_rent,p.occupancy_share,p.opex_share,p.purchase_cost_multiplier].every(Number.isFinite)
      && p.occupancy_share >= 0 && p.occupancy_share <= 1 && p.opex_share >= 0 && p.opex_share <= 1 && p.purchase_cost_multiplier >= 1 && p.purchase_cost_multiplier <= 2;
  }
  function render(v, now = Date.now()) {
    if (!ready(v, now)) return '';
    const s = v.rent_source, p = v.params_used, low = v.confidence === 'low', b = v.benchmark;
    const benchReady = b && Number.isFinite(b.median_net) && Number.isFinite(b.p90_net) && b.n >= 10
      && b.policy_version === '3' && b.params_used && ['occupancy_share','opex_share','purchase_cost_multiplier'].every(k => b.params_used[k] === p[k])
      && Date.parse(b.computed_at) >= now-26*3600000 && Date.parse(b.computed_at) <= now && Date.parse(b.source_oldest_at) >= now-72*3600000
      && Date.parse(b.source_oldest_at) <= Date.parse(b.source_newest_at) && Date.parse(b.source_newest_at) <= Date.parse(b.computed_at);
    const comps = (Array.isArray(v.rental_comps) ? v.rental_comps : []).filter(c => c && /^\d{5,20}$/.test(String(c.id)) && Number.isFinite(c.price)).slice(0,5);
    return `<section class="rental-economics" style="margin:14px 22px;padding:16px;background:var(--bg);border-radius:12px;border:1px solid var(--border);">
      <div style="font-size:12px;color:var(--muted);">Доходность сдачи · сценарий</div>
      <div style="font-size:25px;font-weight:800;color:${low ? 'var(--muted)' : 'var(--green)'};">${escape(v.net_yield_pct.toFixed(1))}% в год</div>
      <p>После принятых расходов. Аренда: <b>${money(v.rent_range[0])}–${money(v.rent_range[1])}/мес.</b></p>
      <p style="font-size:12px;color:var(--muted);">${escape(s.level)} · ${s.n} предложений · ${escape(s.updated_at.slice(0,10))}.${low ? ' Мало данных по ЖК; использован более широкий срез.' : ''}</p>
      ${benchReady ? `<p>Медиана среза: ${escape(b.median_net.toFixed(1))}%; верхние 10% — от ${escape(b.p90_net.toFixed(1))}%.</p>` : ''}
      <details><summary>Допущения и расчёт</summary><p>До расходов: ${escape(v.gross_yield_pct.toFixed(1))}% в год. Аренда ${(p.occupancy_share*100).toFixed(1)}% времени; расходы ${(p.opex_share*100).toFixed(1)}% плановой аренды; покупка +${((p.purchase_cost_multiplier-1)*100).toFixed(1)}%.</p>${Number.isFinite(v.payback_years) ? `<p>Сценарная окупаемость после расходов: ${escape(v.payback_years)} лет.</p>` : ''}</details>
      <p style="font-size:12px;color:var(--muted);">Основа — цены объявлений. Это не подтверждённый доход и не определение налогового режима.</p>
      ${comps.length ? `<details><summary>Похожие предложения аренды</summary><ul>${comps.map(c => `<li><a href="https://krisha.kz/a/show/${c.id}" target="_blank" rel="noopener noreferrer">${c.address ? escape(c.address)+' · ' : ''}${escape(c.rooms)} комн. · ${escape(c.area)} м² · ${money(c.price)}/мес.</a></li>`).join('')}</ul></details>` : ''}</section>`;
  }
  const api = {ready, render};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.HatuliRentalEconomics = api;
})(typeof window === 'undefined' ? globalThis : window);
