"""Seller recommendations remain explainable, private and photo-content honest."""
from copy import deepcopy
from datetime import date
import json

import pytest

from bot.core.seller_listing_review import (
    MAX_DESCRIPTION_LENGTH, PHOTO_LABELS, SellerListingReviewError, review_seller_listing,
)

TODAY = date(2026, 10, 13)


def report(payload):
    return review_seller_listing(payload, today=TODAY)


def all_actions(result):
    return result["recommendations"] + result["additional_recommendations"]


def codes(result):
    return {item["code"] for item in all_actions(result)}


def complete(**changes):
    return {
        "title": "2-комнатная квартира с ремонтом",
        "description": "Продаётся квартира. Покупателю остаются кухня и холодильник. Дом сдан в 2020 году. Показываем квартиру по договорённости вечером после работы.",
        "rooms": 2, "area": 60, "price": 32_000_000, "address": "улица Сарайшык, 12/1",
        "complex_name": "Светлый дом", "floor": 5, "floors_total": 12,
        "kitchen_area": 12, "ceiling_height": 2.9, "year_built": 2020,
        "finish_level": "renovated", "utilities_monthly": 18_000, "utilities_period": "сентябрь 2026",
        "included_items": "кухонный гарнитур и холодильник", "viewing_terms": "по договорённости вечером",
        "legal_status": "no_mortgage", "photos_count": 10,
        "photo_coverage": {key: "present" for key in PHOTO_LABELS}, **changes,
    }


def test_empty_input_is_useful_checklist_without_fake_draft():
    result = report({})
    assert result["facts"] == {}
    assert result["suggested_title"] == "Квартира"
    assert result["suggested_description"] == ""
    assert "ESSENTIAL_FACTS" in codes(result)
    assert result["coverage"] == {"known": 0, "required": 7, "photos_content_checked": False}
    assert len(result["recommendations"]) == 5
    assert result["recommendation_count"] == len(all_actions(result))
    assert all(item["evidence"] and item["action"] for item in all_actions(result))


def test_url_is_canonicalized_without_fetching_or_using_it_as_evidence():
    result = report({"url": "http://www.krisha.kz/a/show/1012345678/?utm_source=share#photos"})
    assert result["source_url"] == "https://krisha.kz/a/show/1012345678"
    assert result["facts"] == {} and result["fact_evidence"] == {}
    assert "Ссылка сама по себе" in result["notes"][0]


@pytest.mark.parametrize("url", [
    "https://krisha.kz.evil.example/a/show/1012345678", "http://127.0.0.1/a/show/1012345678",
    "file:///etc/passwd", "javascript:alert(1)", "https://krisha.kz/a/show/1",
    "https://krisha.kz/a/show/1012345678/more", "https://a:b@krisha.kz/a/show/1012345678",
    "https://krisha.kz:8787/a/show/1012345678", "https://krisha.kz/a/show/1012345678\\",
    "https://krisha.kz/a/show/1012345678\nextra", "//krisha.kz/a/show/1012345678",
])
def test_arbitrary_or_credentialed_sources_rejected(url):
    with pytest.raises(SellerListingReviewError, match="Вставьте ссылку"):
        report({"url": url})


def test_good_source_claims_produce_useful_copy_without_promises():
    payload = complete()
    original = deepcopy(payload)
    result = report(payload)
    assert payload == original
    assert result["recommendations"] == []
    assert result["coverage"]["known"] == 7
    assert "2-комнатная квартира, 60 м², улица Сарайшык, 12/1" == result["suggested_title"]
    draft = result["suggested_description"]
    for fact in ("60 м²", "5 из 12", "12 м²", "2,9 м", "2020", "18 000 ₸", "32 000 000 ₸", "холодильник"):
        assert fact in draft
    assert "Продавец указывает" in draft
    for forbidden in ("юридически чистая", "долгов нет", "дешевле рынка", "продадите", "без обременений"):
        assert forbidden not in draft.lower()


def test_conservative_explicit_description_numbers_fill_known_facts():
    result = report({"description": "2-комнатная квартира. Общая площадь: 60,5 м². Кухня: 12 м2. Потолки 2,9 м. Этаж 5/12. Год постройки: 2020."})
    assert result["facts"] == {"rooms": 2, "area": 60.5, "floor": 5, "floors_total": 12,
        "kitchen_area": 12, "ceiling_height": 2.9, "year_built": 2020}
    assert set(result["fact_evidence"].values()) == {"explicit_text"}
    assert "2,9 м" in result["suggested_description"]


def test_kitchen_number_does_not_become_total_area_or_floor():
    result = report({"description": "Площадь кухни 12 м², квартира просторная."})
    assert result["facts"] == {"kitchen_area": 12}
    assert "area" not in result["facts"]
    assert "Кухня" not in result["suggested_title"]


def test_standard_pasted_headline_and_natural_floor_grammar_work():
    result = report({"description": "Продаю 2-комнатную квартиру, 65 м², 4 этаж из 9. Кухня 12 м²."})
    assert result["facts"] == {"rooms": 2, "area": 65, "floor": 4, "floors_total": 9, "kitchen_area": 12}


def test_headline_does_not_treat_adjacent_kitchen_as_total_area():
    result = report({"description": "2-комнатная квартира, кухня 12 м². На 4 из 9 этажей."})
    assert "area" not in result["facts"] and result["facts"]["kitchen_area"] == 12


@pytest.mark.parametrize("text,value", [
    ("Цена: 35 000 000 ₸", 35_000_000), ("Цена 35 млн тенге", 35_000_000),
    ("Стоимость квартиры: 35,5 млн ₸", 35_500_000), ("Цена квартиры 35000000 тг.", 35_000_000),
    ("Цена: 100 000 000 ₸", 100_000_000),
])
def test_only_explicit_apartment_price_in_tenge_is_extracted(text, value):
    result = report({"description": text})
    assert result["facts"]["price"] == value
    assert result["fact_evidence"]["price"] == "explicit_text"
    assert "PERSONAL_DATA" not in codes(result)


@pytest.mark.parametrize("text", [
    "Ремонт стоил 5 млн тенге, коммунальные 20 000 ₸.", "Цена аренды: 300 000 ₸ в месяц.",
    "Стоимость ремонта: 5 млн ₸.", "Цена: 35 000 долларов.", "Пример цена 35 млн тенге.",
])
def test_rent_repairs_foreign_currency_and_price_examples_not_sale_price(text):
    assert "price" not in report({"description": text})["facts"]


def test_explicit_price_conflict_is_a_check_task_and_not_selected_by_guessing():
    result = report({"price": 34_000_000, "description": "Цена: 35 млн тенге."})
    assert "VERIFY_PRICE" in codes(result) and "price" not in result["facts"]
    assert "34 000 000 ₸" not in result["suggested_description"]
    assert "35 000 000 ₸" not in result["suggested_description"]


def test_unsupported_prose_is_not_transformed_into_advertising_facts():
    result = report({"description": "Лучшая квартира! Без долгов, документы чистые. Низкие коммунальные платежи, никто не жил, качественный элитный ЖК, готовы быстро продать."})
    assert result["facts"] == {}
    assert result["suggested_description"] == ""
    assert "VAGUE_CLAIMS" in codes(result)
    assert "legal_status" in {item["key"] for item in result["missing_fields"]}


@pytest.mark.parametrize("description", [
    "Не 2-комнатная квартира. Не кухня 20 м². Не потолки 3 м.",
    "Например кухня 20 м². Пример общая площадь 100 м².",
])
def test_negated_examples_not_converted_into_known_measurements(description):
    result = report({"description": description})
    assert result["facts"] == {}


def test_inconsistent_values_are_prioritized_and_excluded_from_draft():
    result = report({"rooms": 2, "area": 60, "description": "3-комнатная квартира. Общая площадь 70 м². Кухня 12 м²."})
    assert result["facts"] == {"kitchen_area": 12}
    assert {"VERIFY_ROOMS", "VERIFY_AREA"} <= codes(result)
    assert all(item["priority"] == "high" for item in result["recommendations"][:2])
    assert "2-комнатная" not in result["suggested_title"] and "3-комнатная" not in result["suggested_title"]
    assert "70 м²" not in result["suggested_description"] and "60 м²" not in result["suggested_description"]


def test_multiple_description_values_not_resolved_by_guessing():
    result = report({"description": "Общая площадь 60 м². Общая площадь 70 м². Кухня 12 м²."})
    assert "VERIFY_AREA" in codes(result) and "area" not in result["facts"]


def test_repeated_identical_measurement_is_consistent():
    result = report({"area": "60,00", "description": "Общая площадь 60 м². Площадь квартиры: 60,0 м²."})
    assert result["facts"]["area"] == 60
    assert "VERIFY_AREA" not in codes(result)


@pytest.mark.parametrize("field,value", [
    ("rooms", True), ("rooms", "2.5"), ("rooms", 21), ("area", "nan"), ("area", float("inf")),
    ("area", 0), ("price", "32 миллионов"), ("price", -1), ("floor", []), ("ceiling_height", "99"),
    ("kitchen_area", 300), ("year_built", 2040), ("utilities_monthly", {}),
])
def test_invalid_fact_produces_verify_action_instead_of_generated_claim(field, value):
    result = report({field: value})
    assert field not in result["facts"]
    assert "VERIFY_" + field.upper() in codes(result)


def test_cross_field_impossibilities_not_in_draft():
    result = report({"area": 50, "kitchen_area": 70, "floor": 12, "floors_total": 5})
    assert {"KITCHEN_AREA_CONFLICT", "FLOOR_CONFLICT"} <= codes(result)
    assert result["facts"] == {"area": 50}
    assert set(result["fact_evidence"]) == {"area"}


def test_valid_numeric_strings_zero_studio_and_zero_fee_are_kept():
    result = report({"rooms": "0", "area": "35,5", "price": "20 500 000", "utilities_monthly": "0", "utilities_period": "сентябрь"})
    assert result["facts"]["rooms"] == 0 and result["facts"]["utilities_monthly"] == 0
    assert result["suggested_title"] == "Студия, 35,5 м²"
    assert "сентябрь: 0 ₸" in result["suggested_description"]
    assert "низкие" not in result["suggested_description"].lower()


def test_zero_fee_without_period_does_not_become_permanent_cost_claim():
    result = report({"utilities_monthly": 0})
    assert "Коммунальные расходы" not in result["suggested_description"]
    assert "UTILITIES" in codes(result)


@pytest.mark.parametrize("photos", [1, 3, 10, 40, 100])
def test_photo_count_does_not_prove_content_or_photo_quality(photos):
    result = report({"photos_count": photos})
    assert result["coverage"]["photos_content_checked"] is False
    assert all(item["status"] == "unknown" for item in result["photo_checklist"])
    assert "PHOTO_SELF_CHECK" in codes(result) and "PHOTO_COVERAGE" not in codes(result)
    assert not any("плох" in item["title"].lower() for item in all_actions(result))


def test_count_alias_and_no_photos_are_an_actual_finding():
    result = report({"photo_count": "0"})
    assert result["facts"]["photos_count"] == 0
    assert "NO_PHOTOS" in codes(result)


def test_explicit_missing_views_yield_specific_actions_with_self_reported_evidence():
    result = report({"photos_count": 10, "photo_coverage": {"kitchen": "missing", "bathroom": "missing", "rooms": "present"}})
    action = next(item for item in all_actions(result) if item["code"] == "PHOTO_COVERAGE")
    assert "Вы отметили" in action["evidence"] and "санузел" in action["evidence"]
    assert "рабочей поверхности" in action["action"] and "сантехники" in action["action"]
    assert "PHOTO_SELF_CHECK" not in codes(result)
    assert all(item["evidence"] == "seller_self_check" for item in result["photo_checklist"] if item["key"] in {"rooms", "kitchen", "bathroom"})


def test_no_count_with_covered_checklist_does_not_claim_photos_missing():
    result = report({"photo_coverage": {key: "present" for key in PHOTO_LABELS}})
    assert "NO_PHOTOS" not in codes(result) and "PHOTO_SELF_CHECK" not in codes(result)
    assert result["coverage"]["photos_content_checked"] is False


def test_photo_checklist_action_distinguishes_existing_missing_and_unknown_views():
    result = report({"photo_coverage": {"kitchen": "present", "bathroom": "missing"}})
    checklist = {item["key"]: item for item in result["photo_checklist"]}
    assert checklist["kitchen"]["action"].startswith("Проверьте, что")
    assert not checklist["kitchen"]["action"].startswith("Добавьте")
    assert checklist["bathroom"]["action"].startswith("Покажите")
    assert checklist["floorplan"]["action"].startswith("Проверьте, есть ли")
    assert "Если его нет" in checklist["floorplan"]["action"]


def test_photo_count_and_checklist_conflict_is_explained():
    result = report({"photo_count": 0, "photo_coverage": {"kitchen": "present"}})
    assert "PHOTO_COUNT_CONFLICT" in codes(result)


@pytest.mark.parametrize("coverage", [[], {"roof": "present"}, {"kitchen": True}, {"kitchen": "not_here"}])
def test_invalid_photo_state_is_friendly_input_error(coverage):
    with pytest.raises(SellerListingReviewError, match="фотографий"):
        report({"photo_coverage": coverage})


@pytest.mark.parametrize("count", [True, -1, 101, 1.5, "abc"])
def test_invalid_photo_count_is_friendly_input_error(count):
    with pytest.raises(SellerListingReviewError, match="Количество фотографий"):
        report({"photos_count": count})


def test_contact_identifiers_and_html_never_return_in_report_or_draft():
    payload = {"title": "Квартира +7 (777) 123-45-67", "description": "<script>alert('SECRET')</script>ИИН 123456789012. Пишите secret@example.com или @secret_handle.",
        "address": "<b>улица Сарайшык, 12/1</b> +7 777 123 45 67",
        "included_items": "холодильник, связь secret@example.com", "viewing_terms": "вечером https://t.me/secret_handle"}
    result = report(payload)
    encoded = json.dumps(result, ensure_ascii=False)
    assert "PERSONAL_DATA" in codes(result)
    for private in ("777", "123456789012", "secret@example.com", "secret_handle", "SECRET", "<b>", "<script>"):
        assert private not in encoded
    assert result["facts"]["address"] == "улица Сарайшык, 12/1"


def test_ordinary_short_number_groups_and_building_numbers_are_not_contacts():
    result = report({"title": "2 60 5 12", "address": "улица Сарайшык, 12/1"})
    assert "PERSONAL_DATA" not in codes(result)
    assert result["facts"]["address"] == "улица Сарайшык, 12/1"


@pytest.mark.parametrize("field,value", [("description", "x" * (MAX_DESCRIPTION_LENGTH + 1)), ("title", "x" * 201), ("address", "x" * 181), ("description", {}), ("url", [])])
def test_unbounded_or_nonscalar_inputs_rejected(field, value):
    with pytest.raises(SellerListingReviewError):
        report({field: value})


@pytest.mark.parametrize("payload", [None, [], "description"])
def test_payload_shape_is_bounded_mapping(payload):
    with pytest.raises(SellerListingReviewError):
        report(payload)


def test_unknown_extraneous_private_fields_never_projected():
    result = report({"owner_phone": "PRIVATE", "admin_notes": "PRIVATE", "ai_analysis": {"is_clean": True}, "photos": ["private_url"], "cookies": "PRIVATE"})
    assert result["facts"] == {}
    assert "PRIVATE" not in json.dumps(result)


def test_legal_status_is_seller_claim_only_and_unknown_has_checklist():
    result = report({"legal_status": "mortgage"})
    assert "по информации продавца" in result["suggested_description"]
    assert "DOCUMENT_INFORMATION" not in codes(result)
    assert "Документы" in " ".join(result["notes"]) or "документов" in " ".join(result["notes"])
    unknown = report({"legal_status": "unknown"})
    assert "legal_status" not in unknown["facts"] and "DOCUMENT_INFORMATION" in codes(unknown)


def test_illegal_legal_enum_not_interpreted_as_verified_status():
    with pytest.raises(SellerListingReviewError):
        report({"legal_status": "clean"})


def test_photo_and_critical_fact_actions_precede_soft_style_suggestions():
    result = report({"photos_count": 0, "description": "ИДЕАЛЬНАЯ КВАРТИРА ЛУЧШАЯ КВАРТИРА УНИКАЛЬНАЯ КВАРТИРА"})
    order = [item["priority"] for item in all_actions(result)]
    assert order == sorted(order, key={"high": 0, "medium": 1, "low": 2}.get)
    assert "ALL_CAPS" in codes(result) and "VAGUE_CLAIMS" in codes(result)
