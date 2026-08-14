"""Регрессия для Часть 2, п.9 (задача 2026-08-14, "общее hedonic-ядро
для bargain.py и deal_score.py"): константы формально общие (импорт из
bot/core/hedonic_constants.py), не могут разойтись снова, как в живом
инциденте #1014506231 "Landmark" (см. docs/scoring_audit.md)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core import hedonic_constants


def test_deal_score_imports_shared_constants_not_own_copies():
    import bot.core.deal_score as deal_score
    assert deal_score.AREA_BAND_PCT is hedonic_constants.AREA_BAND_PCT
    assert deal_score.MIN_BLDG is hedonic_constants.MIN_BLDG
    assert deal_score.MIN_HEX is hedonic_constants.MIN_HEX
    assert deal_score.MIN_RING is hedonic_constants.MIN_RING
    assert deal_score.W0 is hedonic_constants.W0
    assert deal_score.W1 is hedonic_constants.W1
    assert deal_score.W2 is hedonic_constants.W2


def test_bargain_imports_shared_constants_not_own_copies():
    import bot.core.bargain as bargain
    assert bargain.AREA_BAND_PCT is hedonic_constants.AREA_BAND_PCT
    # Локальные имена — алиасы на общие константы (MIN_SAME_COMPLEX==MIN_BLDG,
    # MIN_COMPARABLES==MIN_RING), не собственные копии значений.
    assert bargain.MIN_SAME_COMPLEX == hedonic_constants.MIN_BLDG
    assert bargain.MIN_COMPARABLES == hedonic_constants.MIN_RING


def test_area_band_values_match_across_modules():
    # Живой инцидент #1014506231 "Landmark": ±15%/порог 3 разошлись между
    # файлами (2 vs 3 в одной из копий) — эта проверка ловит рассинхрон
    # сразу на уровне значений, не только на уровне "это один объект в памяти".
    import bot.core.deal_score as deal_score
    import bot.core.bargain as bargain
    assert deal_score.AREA_BAND_PCT == 0.15
    assert bargain.AREA_BAND_PCT == 0.15
    assert deal_score.MIN_BLDG == bargain.MIN_SAME_COMPLEX == 3
