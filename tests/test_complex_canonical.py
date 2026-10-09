"""Канонический ЖК (задача 2026-10-03): обрывки текста, дубли по ссылке Крыши."""
import pytest

from bot.core import complex_canonical as cc


def c(id_, name, url=None, **kw):
    return {"id": id_, "name": name, "krisha_url": url, "parent_complex_id": kw.get("parent"),
            "is_umbrella": kw.get("umbrella", False), "housing_class": kw.get("klass"),
            "is_newbuild": kw.get("newbuild", False)}


def test_slug():
    assert cc.krisha_slug("https://krisha.kz/complex/show/nur-sultan/zere/") == "zere"
    assert cc.krisha_slug("https://krisha.kz/complex/show/astana/Zere?x=1") == "zere"
    assert cc.krisha_slug(None) is None and cc.krisha_slug("https://krisha.kz/a/show/1") is None


def test_manual_redirect_and_target_survive_auto_reranking():
    url = 'https://krisha.kz/complex/show/astana/club/'
    rows = [c(1, 'Club', url), c(2, 'ЖК Club', url, newbuild=True), c(3, 'Клаб')]
    overrides = [{'complex_id': 3, 'canonical_id': 1}]
    out = cc.compute_canonical(rows, {2: 1000}, manual_overrides=overrides)
    assert out[1] == (None, None)
    assert out[2] == (1, 'krisha_slug')
    assert out[3] == (1, 'manual_review')


def test_manual_standalone_pin_is_not_reclassified_as_junk():
    assert cc.compute_canonical([c(1, 'На Улице')], {},
        manual_overrides=[{'complex_id': 1, 'canonical_id': None}])[1] == (None, None)


def test_manual_invalid_maps_fail_before_mutation():
    rows = [c(1, 'Club'), c(2, 'Клаб'), c(3, 'Club 2', parent=1)]
    for overrides in [
        [{'complex_id': 1, 'canonical_id': 2}, {'complex_id': 2, 'canonical_id': 1}],
        [{'complex_id': 1, 'canonical_id': 99}],
        [{'complex_id': 3, 'canonical_id': 1}],
    ]:
        with pytest.raises(ValueError):
            cc.compute_canonical(rows, {}, manual_overrides=overrides)
    with pytest.raises(ValueError, match='separate relation'):
        cc.compute_canonical(rows, {}, [{'complex_id_a': 1, 'complex_id_b': 2,
            'relation_type': 'separate_neighbor_complex'}], [{'complex_id': 2, 'canonical_id': 1}])


def test_street_qualified_fragment_cannot_fall_back_to_short_brand():
    rows = [c(1, 'Nova City'), c(2, 'Nova City на Рыскулбекова'),
            c(3, 'Nova City На Рыскулбекова От Од')]
    assert cc.compute_canonical(rows, {})[3] == (None, 'junk_unmatched')


def test_junk_detection():
    for n in ["Sandi Qala 2 Продается 3-Комнат", "Бизнес Класса", "На 188-Ой Улице", "- Чистый И Ухоженный Подъезд",
              "Arena Towers От Надёжного Застр", "Комфорт+", "Премиум"]:
        assert cc.is_junk_name(n), n
    for n in ["Central Park", "Jetisu Kerbez Comfort", "Нурсая-2", "Rio De Janeiro", "Baizaman", "GreenLine. Garden"]:
        assert not cc.is_junk_name(n), n


def test_trusted_names_survive_glued_text():
    glued = "ЖК Horizonрядом с «Барыс Ареной», по"
    assert cc.is_junk_name(glued) and not cc.is_junk_name(glued, trusted=True)
    assert cc.is_junk_name("На Ул", trusted=True)          # явная фраза — обрывок даже у «доверенной» записи


def test_name_prefix_beats_wrong_slug():
    rows = [c(1, "Arena Towers"), c(2, "Tandau", "https://krisha.kz/complex/show/astana/tandau/"),
            c(3, "Arena Towers От Надёжного Застр", "https://krisha.kz/complex/show/astana/tandau/")]
    out = cc.compute_canonical(rows, {})
    assert out[3] == (1, "name_prefix")
    assert out[2] == (None, None)


def test_slug_merges_name_variants_only():
    u = "https://krisha.kz/complex/show/astana/garden/"
    rows = [c(1, "GreenLine. Garden", u, newbuild=True), c(2, "Бигвилль GreenLine. Garden", u),
            c(3, "Landmark Gold", "https://krisha.kz/complex/show/astana/x/"), c(4, "Нурсая 2", "https://krisha.kz/complex/show/astana/x/")]
    out = cc.compute_canonical(rows, {2: 100, 1: 5})
    assert out[2] == (1, "krisha_slug")                     # канон — новостройка, а не та, где больше объявлений
    assert out[3] == (None, None) and out[4] == (None, None)  # разные имена с общим slug — не сливаем


def test_house_under_umbrella_not_merged():
    u = "https://krisha.kz/complex/show/astana/qaiyndy/"
    rows = [c(1, "Qaiyndy", u, umbrella=True), c(2, "Qaiyndy 3", u, parent=1)]
    assert cc.compute_canonical(rows, {})[2] == (None, None)


def test_junk_without_real_complex_is_dropped():
    out = cc.compute_canonical([c(1, "На Улице Култегин"), c(2, "Central Park")], {})
    assert out[1] == (None, "junk_unmatched")


def test_names_related():
    assert cc.names_related("Dariya от NAK", "Dariya")
    assert cc.names_related("Бигвилль UIA.DARYN", "UIA.DARYN")
    assert cc.names_related("Tamasha Город Астана", "TAMASHA")
    assert not cc.names_related("Нурсая 2", "Landmark Gold")


@pytest.mark.parametrize("a,b", [
    ("Arena", "Arena Towers"),
    ("Нурсая", "Нурсая 2"),
    ("Нурсая", "Нурсая-2"),
    ("Qaiyndy 3", "Qaiyndy 30"),
    ("GreenLine", "GreenLine. Garden"),
    ("Garden", "Central Garden"),
])
def test_common_prefix_or_substring_does_not_prove_duplicate(a, b):
    u = "https://krisha.kz/complex/show/astana/shared/"
    assert not cc.names_related(a, b)
    assert cc.compute_canonical([c(1, a, u), c(2, b, u)], {}) == {
        1: (None, None), 2: (None, None),
    }


@pytest.mark.parametrize("base,first,second", [
    ("Dariya", "Dariya от NAK 1 очередь", "Dariya от NAK 2 очередь"),
    ("City", "City от BI Group Garden", "City от BI Group Headliner"),
])
def test_developer_marker_does_not_strip_phase_or_subproject(base, first, second):
    u = "https://krisha.kz/complex/show/astana/shared/"
    # Trusted newbuild names must retain their full phase/product suffixes.
    rows = [c(1, base, u, newbuild=True), c(2, first, u, newbuild=True),
            c(3, second, u, newbuild=True)]
    assert not cc.names_related(base, first)
    assert not cc.names_related(first, second)
    assert cc.compute_canonical(rows, {}) == {
        1: (None, None), 2: (None, None), 3: (None, None),
    }


def test_unknown_developer_tail_needs_review():
    assert not cc.names_related("City от Northern Crown", "City")


def test_shared_slug_merges_variants_within_separate_families():
    u = "https://krisha.kz/complex/show/astana/shared/"
    rows = [c(1, "GreenLine. Garden", u, umbrella=True),
            c(2, "Бигвилль GreenLine. Garden", u),
            c(3, "GreenLine. Headliner", u, newbuild=True),
            c(4, "Bigville GreenLine. Headliner", u)]
    out = cc.compute_canonical(rows, {})
    assert out == {1: (None, None), 2: (1, "krisha_slug"),
                   3: (None, None), 4: (3, "krisha_slug")}


def test_all_junk_slug_group_has_no_junk_canonical_target():
    u = "https://krisha.kz/complex/show/astana/shared/"
    rows = [c(1, "Продается Квартира", u), c(2, "Имеется Тёплый Паркинг", u)]
    assert cc.compute_canonical(rows, {1: 100}) == {
        1: (None, "junk_unmatched"), 2: (None, "junk_unmatched"),
    }


def test_description_only_junk_needs_unambiguous_slug():
    u = "https://krisha.kz/complex/show/astana/shared/"
    rows = [c(1, "Нурсая", u), c(2, "Нурсая 2", u), c(3, "Имеется Тёплый Паркинг", u)]
    assert cc.compute_canonical(rows, {})[3] == (None, "junk_unmatched")
    assert cc.compute_canonical([rows[0], rows[2]], {})[3] == (1, "krisha_slug")


@pytest.mark.parametrize("name", ["Нурсая 2 Продается Квартира", "Нурсая-2 Продается Квартира"])
def test_junk_phase_does_not_collapse_into_base_project_even_with_same_slug(name):
    u = "https://krisha.kz/complex/show/astana/nursaya/"
    rows = [c(1, "Нурсая", u), c(2, name, u)]
    assert cc.compute_canonical(rows, {})[2] == (None, "junk_unmatched")


def test_junk_phase_matches_complete_phase_name():
    rows = [c(1, "Sandi Qala"), c(2, "Sandi Qala 2"),
            c(3, "Sandi Qala 2 Продается 3-Комнат")]
    assert cc.compute_canonical(rows, {})[3] == (2, "name_prefix")


def test_ambiguous_longest_prefix_does_not_fall_back_to_shorter_name():
    rows = [c(1, "Sandi Qala"), c(2, "Sandi Qala 2"), c(3, "Sandi Qala 2"),
            c(4, "Sandi Qala 2 Продается 3-Комнат")]
    assert cc.compute_canonical(rows, {})[4] == (None, "junk_unmatched")
    assert cc.compute_canonical(list(reversed(rows)), {3: 100})[4] == (None, "junk_unmatched")


def test_same_name_prefix_is_resolved_only_after_proven_slug_duplicates():
    u = "https://krisha.kz/complex/show/astana/arena-towers/"
    rows = [c(1, "Arena Towers", u, newbuild=True), c(2, "Arena Towers", u),
            c(3, "Arena Towers Продается Квартира")]
    out = cc.compute_canonical(rows, {2: 100})
    assert out[2] == (1, "krisha_slug")
    assert out[3] == (1, "name_prefix")


def test_house_remains_separate_when_parent_has_different_or_missing_slug():
    u = "https://krisha.kz/complex/show/astana/qaiyndy/"
    rows = [c(1, "Qaiyndy", umbrella=True), c(2, "Qaiyndy", u, parent=1), c(3, "Qaiyndy", u)]
    assert cc.compute_canonical(rows, {})[2] == (None, None)


@pytest.mark.parametrize("relation_type", ["sibling_phase", "same_umbrella_project", "separate_neighbor_complex"])
def test_negative_review_blocks_exact_slug_variants(relation_type):
    u = "https://krisha.kz/complex/show/astana/shared/"
    rows = [c(1, "Garden", u, umbrella=True), c(2, "Бигвилль Garden", u)]
    relations = [{"complex_id_a": 1, "complex_id_b": 2, "relation_type": relation_type}]
    assert cc.compute_canonical(rows, {}, relations) == {1: (None, None), 2: (None, None)}


def test_negative_review_blocks_merge_through_common_slug_canonical():
    u = "https://krisha.kz/complex/show/astana/shared/"
    rows = [c(1, "Garden", u, umbrella=True), c(2, "Бигвилль Garden", u),
            c(3, "Garden Город Астана", u, newbuild=True), c(4, "Имеется Тёплый Паркинг", u)]
    relations = [{"complex_id_a": 2, "complex_id_b": 3, "relation_type": "sibling_phase"}]
    out = cc.compute_canonical(rows, {}, reviewed_relations=relations)
    # 3 joins 1 by rank; 2 cannot also join 1 because this would indirectly join 2/3.
    assert out[3] == (1, "krisha_slug")
    assert out[2] == (None, None)
    assert out[4] == (None, "junk_unmatched")
    assert cc.compute_canonical(list(reversed(rows)), {}, relations) == out


def test_negative_review_on_prefix_alias_blocks_later_slug_chain():
    u = "https://krisha.kz/complex/show/astana/shared/"
    rows = [c(1, "Arena Towers", u), c(2, "Arena Towers Продается Квартира"),
            c(3, "Бигвилль Arena Towers", u, umbrella=True)]
    relations = [{"complex_id_a": 2, "complex_id_b": 3, "relation_type": "separate_neighbor_complex"}]
    out = cc.compute_canonical(rows, {}, relations)
    assert out[2] == (1, "name_prefix")
    assert out[1] == (None, None) and out[3] == (None, None)


def test_negative_review_blocks_name_prefix():
    rows = [c(1, "Arena Towers"), c(2, "Arena Towers Продается Квартира")]
    relations = [{"complex_id_a": 1, "complex_id_b": 2, "relation_type": "same_umbrella_project"}]
    assert cc.compute_canonical(rows, {}, relations)[2] == (None, "junk_unmatched")


@pytest.mark.parametrize("relation_type", ["duplicate_same_complex", "renamed_same_complex"])
def test_positive_review_waits_for_manual_alias_workflow(relation_type):
    rows = [c(1, "Sardar Compass"), c(2, "Бурабай")]
    relations = [{"complex_id_a": 1, "complex_id_b": 2, "relation_type": relation_type}]
    assert cc.compute_canonical(rows, {}, relations) == {1: (None, None), 2: (None, None)}
