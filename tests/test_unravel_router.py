"""Регрессия review-router для расшивки blob-комплексов (задача
2026-08-12, Gate 2 — см. unravel_blobs.py). Три заданных пользователем
якорных случая: AUSTRIA (мега, голые номера блоков -> review),
Времена Года (именованная фаза "Лето"/малый родитель -> auto),
Family Nest (буквенные блоки -> auto независимо от размера родителя).

Не требует БД — cluster_needs_review() чистая функция по строкам имён
(внутри дёргает _phase_token(), тоже чистую)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unravel_blobs import cluster_needs_review, ROUTER_MIN_PARENT_BLOCK_NUMBERS


AUSTRIA_PARENT = [
    'МЖК "AUSTRIA" (блоки 4, 11)', 'МЖК "AUSTRIA" (блоки 1, 2, 3)',
    'МЖК "AUSTRIA" (блоки 8, 10)', 'МЖК "AUSTRIA" (блоки 5, 7)',
    'МЖК "AUSTRIA" (блок 6)', 'МЖК "AUSTRIA" (блок 9)',
]
VREMENA_GODA_PARENT = [
    'ЖК "Времена года (Лето)" (блоки 1-1, 1-2)', 'ЖК "Времена Года (Лето)" (блоки 2-1, 2-2)',
    'ЖК "Времена Года (Лето)" (блоки  3-1, 3-2)', 'ЖК "Времена Года (Лето) - 5"',
    'ЖК "Времена Года (Лето)" (блок 4)',
]
FAMILY_NEST_PARENT = ['Family Nest', 'Family Nest A', 'Family Nest F']


def test_austria_mega_bare_numbers_routed_to_review():
    cluster = ['МЖК "AUSTRIA" (блок 6)', 'МЖК "AUSTRIA" (блок 9)']
    assert cluster_needs_review(cluster, AUSTRIA_PARENT) is True


def test_vremena_goda_named_phase_stays_auto():
    """'Лето-5' + 'блок 4' — родитель маркирует явно только {1,2,3,4}
    (не 5, у той стороны нет "блок"-маркера) — ниже порога, auto."""
    cluster = ['ЖК "Времена Года (Лето) - 5"', 'ЖК "Времена Года (Лето)" (блок 4)']
    assert cluster_needs_review(cluster, VREMENA_GODA_PARENT) is False


def test_family_nest_letters_always_auto_regardless_of_parent_size():
    cluster = ['Family Nest A', 'Family Nest F']
    assert cluster_needs_review(cluster, FAMILY_NEST_PARENT) is False
    # даже если родителя искусственно раздуть буквами — буквенный кластер
    # всё равно не подпадает под роутер (тот только для голых номеров).
    inflated_parent = FAMILY_NEST_PARENT + [f"Family Nest {c}" for c in "BCDEGH"]
    assert cluster_needs_review(cluster, inflated_parent) is False


def test_queue_phrase_always_auto_regardless_of_parent_size():
    """Явная 'N-я очередь'/'очередь N' несёт собственную уверенность —
    роутер её не трогает, даже у большого родителя."""
    big_parent = [f"ЖК Проект (блок {i})" for i in range(1, 8)] + ["ЖК Проект (3-я очередь)"]
    cluster = ["ЖК Проект (3-я очередь)"]
    assert cluster_needs_review(cluster, big_parent) is False


def test_bare_numeric_cluster_with_small_parent_stays_auto():
    """Голый номер блока, но родитель маленький (< порога) — auto, как
    в первых 5 (Aisar, Salt и т.п. из Gate 2)."""
    parent = ['ЖК "Aisar"', 'ЖК "Aisar 3"']
    cluster = ['ЖК "Aisar 3"']
    assert cluster_needs_review(cluster, parent) is False


def test_threshold_is_a_named_tunable_constant():
    assert ROUTER_MIN_PARENT_BLOCK_NUMBERS == 5
