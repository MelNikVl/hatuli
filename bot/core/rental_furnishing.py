"""Conservative text signals, distinct from an independently validated label."""
from __future__ import annotations

import json
import re
from bot.core.rental_stage0_identity import RentalStage0MigrationRequired

VERSION = 'furnishing_rules_v1'
LABELS = {'none', 'partial', 'full', 'unknown'}


class RentalStage2MigrationRequired(RentalStage0MigrationRequired):
    def __init__(self):
        RuntimeError.__init__(self, 'Rental stage 2 requires migrations 109_rental_period_complex_hex, 110_rental_index_v2 and 112; no observation history was fabricated')


RULES = {
    'none': (r'без\s+мебели', r'мебел[ьи]\s+нет', r'мебель\s+отсутствует', r'не\s+меблирован\w*', r'unfurnished'),
    'partial': (r'частичн\w*\s+(?:с\s+)?мебл\w*', r'частичн\w*\s+мебел\w*',
                r'мебл\w*\s+частичн\w*', r'только\s+кухонн\w*\s+(?:гарнитур|мебел\w*)', r'partly\s+furnished'),
    'full': (r'полностью\s+(?:с\s+)?мебл\w*', r'полностью\s+(?:с\s+)?мебел\w*',
             r'мебл\w*\s+полностью', r'вся\s+необходимая\s+мебель', r'fully\s+furnished'),
}


def compile_custom_rules(value):
    """Operator regex only, bounded; invalid input never enables a category."""
    try:
        raw = json.loads(value or '{}') if isinstance(value, str) else value
        if not isinstance(raw, dict) or set(raw)-{'none', 'partial', 'full'}:
            return {}
        result = {}
        for label, patterns in raw.items():
            if not isinstance(patterns, list) or len(patterns)>10:
                return {}
            compiled = []
            for pattern in patterns:
                # Deliberately disallow groups/quantifiers in configurable regex:
                # bounded text matching must not create a ReDoS hot path.
                if (not isinstance(pattern, str) or not 1<=len(pattern)<=160
                        or re.search(r'[()*+{}|]', pattern)):
                    return {}
                compiled.append(re.compile(pattern, re.I))
            result[label] = compiled
        return result
    except (ValueError, TypeError, re.error):
        return {}


def classify_furnishing(title=None, description=None, custom_rules=None):
    text = f'{title or ""} {description or ""}'[:10000]
    if re.search(r'\bне\s+(?:полностью|частично|без\s+мебели)|\bнеполностью\b',text,re.I):
        return 'unknown'
    found = {label for label, patterns in RULES.items() if any(re.search(pattern, text, re.I) for pattern in patterns)}
    for label, patterns in (custom_rules or {}).items():
        if label in RULES and any(pattern.search(text) for pattern in patterns):
            found.add(label)
    # Negative and partial statements take precedence only when unambiguous.
    if len(found) != 1:
        return 'unknown'
    return next(iter(found))


def furnishing_validation_ready(values):
    """No furnished index until an operator enables a reviewed real-source audit."""
    if values.get('RENTAL_FURNISHING_INDEX_ENABLED') != '1':
        return False
    try:
        receipt = json.loads(values.get('RENTAL_FURNISHING_VALIDATION_RECEIPT') or '{}')
        return (receipt.get('schema_version') == 1 and receipt.get('classifier_version') == VERSION
                and receipt.get('eligible_for_operator_enable') is True
                and not receipt.get('errors')
                and receipt.get('dataset_kind') == 'manual_real_source'
                and type(receipt.get('unique_reviewed')) is int and receipt['unique_reviewed']>=50
                and type(receipt.get('known_labels_min_support')) is int and receipt['known_labels_min_support']>=5
                and type(receipt.get('accuracy')) in (int, float) and .9<=receipt['accuracy']<=1
                and re.fullmatch(r'[0-9a-f]{64}', receipt.get('dataset_sha256', '')) is not None
                and re.fullmatch(r'[0-9a-f]{64}', receipt.get('rules_sha256', '')) is not None
                and receipt.get('rules_sha256') == rules_sha256(values.get('RENTAL_FURNISHING_RULES_JSON','{}'))
                and isinstance(receipt.get('reviewed_at'), str) and bool(receipt['reviewed_at']))
    except (ValueError, TypeError, AttributeError):
        return False


def rules_sha256(value='{}'):
    import hashlib
    raw = json.loads(value) if isinstance(value, str) else value
    body = json.dumps({'version':VERSION,'base':RULES,'custom':raw},sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def version_tag(value='{}'):
    try:
        return VERSION+':'+rules_sha256(value)
    except (ValueError,TypeError):
        return VERSION+':invalid'


def furnishing_index_row_ready(row,values):
    return (furnishing_validation_ready(values)
            and row.get('furnishing_version')==version_tag(values.get('RENTAL_FURNISHING_RULES_JSON','{}')))
