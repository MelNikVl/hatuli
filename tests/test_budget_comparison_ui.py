"""Amount-only UI contract; pure templates and a mocked browser, no DB/network."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import textwrap

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest

ROOT = Path(__file__).resolve().parents[1]


def money(value, decimals=0):
    return f"{value:,.{decimals}f}".replace(",", " ").replace(".", ",")


def render(template="_investment_strategies.html", **values):
    env = Environment(loader=FileSystemLoader(ROOT / "bot/templates"),
                      autoescape=select_autoescape(["html"]))
    env.filters["fmt_num"] = money
    return env.get_template(template).render(strategy_form={"budget": "30000000", "complex_id": ""},
                                             strategy_result=None, strategy_error=None,
                                             complex_catalog=None, **values)


def fragment(result):
    env = Environment(loader=FileSystemLoader(ROOT / "bot/templates"),
                      autoescape=select_autoescape(["html"]))
    env.filters["fmt_num"] = money
    return env.get_template("_investment_strategy_results.html").render(strategy_result=result)


class Fields(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.fields = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if tag in ("input", "select", "textarea"):
            self.fields.append((tag, dict(attrs)))


def test_only_amount_is_required_and_full_catalog_is_native_optional_select():
    env = Environment(loader=FileSystemLoader(ROOT / "bot/templates"), autoescape=True)
    env.filters["fmt_num"] = money
    body = env.get_template("_investment_strategies.html").render(
        strategy_form={"budget": "30000000", "complex_id": "9"}, strategy_result=None,
        complex_catalog={"items": [{"id": 4, "name": "Зонтик", "label": "Зонтик",
                                   "aliases": ["Alias"], "is_umbrella": True},
                                  {"id": 9, "name": "Дом <script>", "label": "Зонтик · дом 9",
                                   "parent_name": "Зонтик", "aliases": ["Old house name"]}]})
    fields = Fields(body).fields
    assert [(tag, attrs["name"]) for tag, attrs in fields if "required" in attrs] == [("input", "budget")]
    assert [attrs["name"] for _, attrs in fields if attrs.get("name")] == ["budget", "complex_id"]
    assert 'type="search"' in body and 'data-complex-search-field hidden' in body
    assert 'value="9"' in body and ' selected' in body
    assert 'Old house name' in body and 'Alias' in body
    assert 'Дом &lt;script&gt;' in body and '<script>' not in body
    assert 'По всему каталогу ЖК Hatuli' in body
    assert not any(word in body for word in ("purchase_price", "occupancy", "monthly_enabled", "__exit_value"))


def test_missing_catalog_has_amount_only_and_no_empty_select():
    body = render()
    assert len(Fields(body).fields) == 1
    assert 'data-complex-select' not in body
    assert 'method="post"' in body
    assert 'action="/compare/strategies/calculate#strategies"' in body


@pytest.mark.parametrize("result", [None, {"banks": [], "properties": [], "purchases": []}])
def test_unavailable_results_have_no_public_placeholder(result):
    body = fragment(result)
    assert not body.strip()


def bank(**changes):
    return {"key": "bank:test", "label": "Банк", "product": "Годовой",
            "term_months": 12, "total_profit": 5_000_000, "end_wealth": 35_000_000,
            "effective_pct": 16.67, "nominal_pct": 15.5,
            "source_url": "https://bank.example/terms", "checked_at_label": "19.10.2026",
            "guarantee_cap": 20_000_000, "unguaranteed_amount": 15_000_000,
            "conditions": ["Остаток <минимум>"], **changes}


def property_row():
    return {"id": 1001, "title": "2-комнатная", "complex_name": "ЖК Дом",
            "address": "Астана, улица А, 7", "price": 27_000_000, "rooms": 2, "area": 61,
            "cash_remainder": 3_000_000, "href": "/compare?listing=1001", "url": "https://example.org/a/1001",
            "observed_at_label": "19.10.2026", "rental_estimate": {"monthly_low": 200_000,
                "monthly_high": 250_000, "monthly_median": 225_000, "date_label": "19.10.2026"},
            "annual_gross_ceiling": 2_700_000, "bank_threshold": {"bank": "Банк", "annual_income": 5_000_000,
                "minimum_monthly_net_cash": 416_667}}


def test_bank_result_shows_ready_income_and_keeps_limits_in_disclosure():
    row = bank()
    body = fragment({"budget": 30_000_000, "banks": [row], "best_bank": row,
                     "properties": [], "purchases": [], "guarantee_source": "https://kdif.kz"})
    assert 'data-strategy-result="bank:test"' in body and 'hs-bank-best' in body
    assert '+5 000 000 ₸' in body and '35 000 000 ₸' in body
    assert '16,67% ГЭСВ' in body
    assert 'Лимит гарантии в банке: 20 000 000 ₸' in body
    assert 'Остаток &lt;минимум&gt;' in body
    assert 'Ваш сценарий' not in body and 'стоимость выхода' not in body


def test_property_asking_rent_never_labeled_net_profit_or_future_sale():
    body = fragment({"budget": 30_000_000, "banks": [], "properties": [property_row()], "purchases": []})
    assert 'Астана, улица А, 7' in body
    assert '200 000 ₸–250 000 ₸' in body and '2 700 000 ₸' in body
    assert 'за 12 месяцев без простоя, до расходов' in body
    assert 'после всех расходов нужно 416 667 ₸' in body
    assert 'Рост цены и будущая продажа квартиры не включены' in body
    assert 'Капитал в конце года' not in body and 'Чистый доход' not in body


def test_purchase_without_rental_estimate_only_shows_real_price_address():
    row = property_row()
    del row["rental_estimate"]
    body = fragment({"budget": 30_000_000, "banks": [], "properties": [], "purchases": [row]})
    assert 'data-purchase-result="1001"' in body
    assert '27 000 000 ₸' in body and 'Астана, улица А, 7' in body
    assert 'аренды' not in body and 'недостаточно' not in body
    assert 'Цена покупки — без ремонта и расходов сделки' in body


NODE_PRELUDE = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const handlers = {};
const budget = {value: '30000000'};
const error = {hidden: true, textContent: ''};
const submit = {disabled: false};
const submitLabel = {textContent: 'Рассчитать'};
const result = {innerHTML: 'old', focusCount: 0,
  replaceChildren() {this.innerHTML = '';},
  querySelector() {return this.innerHTML ? {focus: () => this.focusCount++} : null;}};
const search = {value: '', listeners: {}, addEventListener(t, fn) {this.listeners[t] = fn;}};
const searchField = {hidden: true};
const matchCount = {textContent: ''};
const option = (value, text, searchText) => ({value, textContent: text, dataset: {searchText},
  cloneNode() {return option(this.value, this.textContent, this.dataset.searchText);}});
const select = {value: '2', options: [option('', 'Вся Астана', ''), option('1', 'ЖК Первый', 'ЖК Первый Original'),
  option('2', 'ЖК Второй', 'ЖК Второй Oldname'), option('3', 'Ёлка', 'Ёлка')],
  replaceChildren(...nodes) {this.options = nodes;}};
const nodes = {'[data-strategy-error-global]': error, '[data-strategy-submit]': submit,
  '[data-strategy-submit-label]': submitLabel, '[data-complex-select]': select,
  '[data-complex-search]': search, '[data-complex-search-field]': searchField,
  '[data-complex-match-count]': matchCount};
const form = {action: 'https://site.test/compare/strategies/calculate#strategies', busy: false,
  elements: {namedItem: name => name === 'budget' ? budget : null},
  querySelector: key => nodes[key] || null, addEventListener(t, fn) {handlers[t] = fn;},
  setAttribute() {this.busy = true;}, removeAttribute() {this.busy = false;}};
const pending = [];
const sandbox = {document: {querySelector: key => key === '[data-strategy-form]' ? form : result},
  fetch: (url, options) => new Promise((resolve, reject) => pending.push({url, options, resolve, reject})),
  FormData: class {constructor() {return [['budget', budget.value], ['complex_id', select.value]];}},
  URLSearchParams, AbortController, console};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
'''


def node_test(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    subprocess.run([node, "-e", NODE_PRELUDE + textwrap.dedent(script), str(ROOT / "static/hatuli-strategies.js")],
                   check=True, capture_output=True, text=True)


def test_search_aliases_preserves_selected_id_and_never_submits_search():
    node_test(r'''
        assert.equal(searchField.hidden, false);
        search.value = 'original'; search.listeners.input();
        assert.deepEqual(select.options.map(x => x.value), ['', '1', '2']);
        assert.equal(select.value, '2'); assert.equal(matchCount.textContent, 'Найдено: 1');
        search.value = 'елка'; search.listeners.input();
        assert.deepEqual(select.options.map(x => x.value), ['', '2', '3']);
        handlers.input({target: search});
        assert.equal(result.innerHTML, 'old');
        search.value = ''; search.listeners.input();
        assert.equal(select.options.length, 4); assert.equal(matchCount.textContent, '');
    ''')


def test_changed_budget_cancels_old_response_and_post_is_private_amount_only():
    node_test(r'''
        (async () => {
          const first = handlers.submit({preventDefault() {}});
          assert.equal(submit.disabled, true); assert.equal(form.busy, true);
          assert.equal(result.innerHTML, '');
          assert.equal(pending[0].options.cache, 'no-store');
          assert.equal(pending[0].options.credentials, 'same-origin');
          assert.equal(pending[0].options.body.toString(), 'budget=30000000&complex_id=2');
          assert(!pending[0].url.includes('#'));
          budget.value = '40000000'; handlers.input({target: budget});
          assert.equal(pending[0].options.signal.aborted, true);
          pending[0].resolve({ok: true, json: async () => ({html: '<p>OLD</p>'})});
          await first; assert.equal(result.innerHTML, '');
          const next = handlers.submit({preventDefault() {}});
          pending[1].resolve({ok: true, json: async () => ({html: '<p>READY</p>'})});
          await next; assert.equal(result.innerHTML, '<p>READY</p>');
          assert.equal(submit.disabled, false); assert.equal(form.busy, false);
          assert.equal(result.focusCount, 1);
        })().catch(error => {console.error(error); process.exitCode = 1;});
    ''')


def test_server_error_clears_old_results_and_uses_text_content():
    node_test(r'''
        (async () => {
          const task = handlers.submit({preventDefault() {}});
          pending[0].resolve({ok: false, json: async () => ({error: '<script>bad</script>'})});
          await task;
          assert.equal(error.textContent, '<script>bad</script>');
          assert.equal(error.hidden, false); assert.equal(result.innerHTML, '');
          assert.equal(submit.disabled, false);
        })().catch(error => {console.error(error); process.exitCode = 1;});
    ''')
