"""Канонический ЖК (задача 2026-10-03): обрывки текста, дубли по ссылке Крыши."""
from bot.core import complex_canonical as cc


def c(id_, name, url=None, **kw):
    return {"id": id_, "name": name, "krisha_url": url, "parent_complex_id": kw.get("parent"),
            "is_umbrella": kw.get("umbrella", False), "housing_class": kw.get("klass"),
            "is_newbuild": kw.get("newbuild", False)}


def test_slug():
    assert cc.krisha_slug("https://krisha.kz/complex/show/nur-sultan/zere/") == "zere"
    assert cc.krisha_slug("https://krisha.kz/complex/show/astana/Zere?x=1") == "zere"
    assert cc.krisha_slug(None) is None and cc.krisha_slug("https://krisha.kz/a/show/1") is None


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
    assert not cc.names_related("Нурсая 2", "Landmark Gold")
