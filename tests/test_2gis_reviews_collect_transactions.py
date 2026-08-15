"""Регрессия для бага, найденного при аудите незакоммиченной работы
DeepSeek (задача 2026-08-15): 2gis_reviews_collect.py::main() держало
транзакцию открытой поперёк time.sleep() между ЖК — ветка "geo НЕ
найден" (самая частая на практике) не коммитила вовсе, поэтому при
нескольких подряд промахах транзакция от начальной SELECT росла на
sleep_s (45-60с) КАЖДУЮ такую итерацию. Реально уронило
test_umbrellas_page.py (ALTER TABLE complexes блокировался "idle in
transaction"-соединением этого скрипта).

psycopg2.connect() и все сетевые функции (find_geo_id/fetch_reviews/
classify_llm) замоканы — тест не делает ни одного реального запроса к
БД/2GIS/DeepSeek, проверяет только дисциплину commit/rollback вокруг
цикла по ЖК."""
import importlib
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

mod = importlib.import_module("2gis_reviews_collect")


def _fake_find_geo_id(name):
    return {
        "ЖК Найден": ("999", "ЖК Найден 2GIS"),
        "ЖК Пустой": ("998", "ЖК Пустой 2GIS"),
        # "ЖК НеНайден" отсутствует -> find_geo_id вернёт None -> ветка
        # "geo НЕ найден" (та самая, где раньше не было commit()).
    }.get(name)


def _fake_fetch_reviews(gid):
    return {
        "999": [{"author": "A", "text": "Отличный дом, тихо и чисто", "date": None}],
        "998": [],
    }.get(gid, [])


def _run_collector(complexes):
    """complexes — список (id, name); возвращает (conn_mock, events), где
    events — хронологический список 'commit'/'rollback'/'sleep' в
    порядке фактических вызовов (порядок — то, что тут проверяется)."""
    events = []

    conn = MagicMock()
    conn.commit.side_effect = lambda: events.append("commit")
    conn.rollback.side_effect = lambda: events.append("rollback")
    cur = conn.cursor.return_value
    cur.fetchall.return_value = complexes

    def fake_sleep(_):
        events.append("sleep")

    with patch.object(mod, "psycopg2") as psycopg2_mock, \
         patch.object(mod, "find_geo_id", side_effect=_fake_find_geo_id), \
         patch.object(mod, "fetch_reviews", side_effect=_fake_fetch_reviews), \
         patch.object(mod, "classify_llm", return_value={}), \
         patch.object(mod.time, "sleep", side_effect=fake_sleep), \
         patch.object(sys, "argv", ["2gis_reviews_collect.py", "--limit", "3", "--fast"]):
        psycopg2_mock.connect.return_value = conn
        mod.main()

    return conn, events


def test_commit_after_every_complex_including_geo_not_found():
    """Три ЖК: geo найден+отзыв, geo НЕ найден (баг был именно тут), geo
    найден+пусто. Ровно 4 commit (1 за начальную SELECT + 1 на ЖК) — БЕЗ
    исключений на пути, значит rollback не вызывается вовсе."""
    complexes = [(101, "ЖК Найден"), (102, "ЖК НеНайден"), (103, "ЖК Пустой")]
    conn, events = _run_collector(complexes)

    assert conn.commit.call_count == 1 + len(complexes)
    assert conn.rollback.call_count == 0


def test_no_transaction_spans_a_sleep_call():
    """Голая регрессия: между ЛЮБЫМИ двумя соседними 'sleep' в хронологии
    обязан стоять хотя бы один 'commit'/'rollback' — иначе транзакция
    от предыдущей итерации пережила time.sleep() и потянулась в
    следующую (ровно баг, что уронил test_umbrellas_page.py)."""
    complexes = [(101, "ЖК Найден"), (102, "ЖК НеНайден"), (103, "ЖК ТожеНеНайден"),
                 (104, "ЖК Пустой")]
    conn, events = _run_collector(complexes)

    sleep_positions = [i for i, e in enumerate(events) if e == "sleep"]
    assert len(sleep_positions) == len(complexes)
    for a, b in zip(sleep_positions, sleep_positions[1:]):
        between = events[a + 1:b]
        assert "commit" in between or "rollback" in between, (
            f"нет commit/rollback между двумя sleep -> транзакция пережила sleep: {events}")


def test_exception_in_one_complex_rolls_back_not_commits():
    """Если тело итерации бросит исключение (сейчас все HTTP-функции сами
    глотают ошибки — это защита на будущее) — conn.rollback(), не
    commit() наполовину собранных вставок, и цикл продолжает следующий ЖК."""
    complexes = [(101, "ЖК Сломан"), (102, "ЖК Найден")]

    def boom(name):
        if name == "ЖК Сломан":
            raise RuntimeError("сетевой сбой")
        return _fake_find_geo_id(name)

    events = []
    conn = MagicMock()
    conn.commit.side_effect = lambda: events.append("commit")
    conn.rollback.side_effect = lambda: events.append("rollback")
    cur = conn.cursor.return_value
    cur.fetchall.return_value = complexes

    with patch.object(mod, "psycopg2") as psycopg2_mock, \
         patch.object(mod, "find_geo_id", side_effect=boom), \
         patch.object(mod, "fetch_reviews", side_effect=_fake_fetch_reviews), \
         patch.object(mod, "classify_llm", return_value={}), \
         patch.object(mod.time, "sleep"), \
         patch.object(sys, "argv", ["2gis_reviews_collect.py", "--limit", "2", "--fast"]):
        psycopg2_mock.connect.return_value = conn
        mod.main()

    assert events.count("rollback") == 1
    assert events.count("commit") == 2  # начальная SELECT + успешный "ЖК Найден"
