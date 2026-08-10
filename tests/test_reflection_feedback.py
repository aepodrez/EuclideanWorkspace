import reflection_feedback as feedback


def test_every_mapper_field_has_an_authoritative_semantic_candidate():
    missing = []
    for field, definition in feedback.mapper.COMPUSTAT_FIELDS.items():
        candidates = set(feedback.TAG_RE.findall(definition))
        candidates.update(feedback.FIELD_TAG_ALIASES.get(field, set()))
        if not any(feedback._definition_supports_tag(field, tag) for tag in candidates):
            missing.append(field)

    assert missing == []
    assert len(feedback.mapper.COMPUSTAT_FIELDS) == 107


def test_only_compustat_validated_pairs_are_promotable():
    assert feedback._is_promotable("at", "Assets")
    assert feedback._is_promotable("drc", "DeferredRevenueCurrent")
    assert not feedback._is_promotable("csho", "CommonStockSharesIssued")
    assert not feedback._is_promotable(
        "intan", "FiniteLivedIntangibleAssetsNet"
    )
    assert not feedback._is_promotable(
        "sale", "RevenueFromContractWithCustomerExcludingAssessedTax"
    )


def test_reflections_can_propose_candidates_across_the_schema():
    text = """## Accuracy Issues
- `at`: unused Assets should map to this field.
- `sale`: unused Revenues should map to this field.
- `ffo`: unused FundsFromOperations should map to this field.
## Prompt Improvements
- none
"""

    assert feedback._candidate_pairs(text) == {
        ("at", "Assets"),
        ("sale", "Revenues"),
        ("ffo", "FundsFromOperations"),
    }


def test_semantic_gate_rejects_known_category_errors():
    assert not feedback._definition_supports_tag("txp", "IncomeTaxesPaidNet")
    assert not feedback._definition_supports_tag("pstk", "StockholdersEquity")
    assert not feedback._definition_supports_tag(
        "dltt_finlease", "OperatingLeaseLiabilityNoncurrent"
    )
    assert not feedback._definition_supports_tag("am", "Assets")
    assert not feedback._definition_supports_tag("xacc", "Liabilities")


def test_one_word_sec_concepts_are_scoped_to_their_exact_fields():
    assert feedback._definition_supports_tag("at", "Assets")
    assert feedback._definition_supports_tag("lt", "Liabilities")
    assert feedback._definition_supports_tag("sale", "Revenues")


def test_production_mapper_guard_must_retain_candidate():
    facts = {"EarningsPerShareBasic": 1.25}

    assert not feedback._survives_mapper_guards("at", "EarningsPerShareBasic", facts)
