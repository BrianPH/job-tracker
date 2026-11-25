"""Offline tests for jobtracker/generation_schema.py (issue #10): the pure-code
validator shared by the live API path and the future JSON-handoff path. No
network, no model -- every check is exercised directly against hand-built
TailorResult instances."""
import yaml

from jobtracker.generation_schema import (
    AnalysisResult,
    ApplicationRequirements,
    TailorResult,
    ground_tailor_result,
    validate_tailor_result,
)

CANDIDATE = yaml.safe_load(open("config/candidate.example.yaml", encoding="utf-8"))


def _valid_result(**overrides):
    defaults = dict(
        language="en", profile="A tailored profile paragraph. " * 10,
        experience_order=["examplecorp", "acmesoftware"],
        bullets={"examplecorp": ["examplecorp-pipelines", "examplecorp-cicd"],
                "acmesoftware": ["acmesoftware-webapp"]},
        skills=["Python", "SQL"],
        letter_paragraphs=["Paragraph one is here. " * 25, "Paragraph two is here. " * 25],
    )
    defaults.update(overrides)
    return TailorResult(**defaults)


def test_validate_accepts_well_formed_result():
    errors = validate_tailor_result(_valid_result(), CANDIDATE)
    assert errors == []
    print("PASS test_validate_accepts_well_formed_result")


def test_validate_rejects_unknown_role_id():
    result = _valid_result(experience_order=["examplecorp", "no-such-role"])
    errors = validate_tailor_result(result, CANDIDATE)
    assert any("no-such-role" in e for e in errors)
    print("PASS test_validate_rejects_unknown_role_id")


def test_validate_rejects_unknown_bullet_id():
    result = _valid_result(bullets={"examplecorp": ["no-such-bullet"]})
    errors = validate_tailor_result(result, CANDIDATE)
    assert any("no-such-bullet" in e for e in errors)
    print("PASS test_validate_rejects_unknown_bullet_id")


def test_validate_rejects_bullet_id_from_wrong_role():
    # A real bullet id, but under a role it doesn't belong to.
    result = _valid_result(bullets={"acmesoftware": ["examplecorp-pipelines"]})
    errors = validate_tailor_result(result, CANDIDATE)
    assert any("examplecorp-pipelines" in e for e in errors)
    print("PASS test_validate_rejects_bullet_id_from_wrong_role")


def test_validate_rejects_unknown_skill():
    result = _valid_result(skills=["Rust"])
    errors = validate_tailor_result(result, CANDIDATE)
    assert any("Rust" in e for e in errors)
    print("PASS test_validate_rejects_unknown_skill")


def test_validate_ignores_bad_word_counts():
    # Word counts are a style nit, not an id/shape error -- validate_* no
    # longer blocks on them (ground_tailor_result warns instead).
    result = _valid_result(profile="Too short.", letter_paragraphs=["Also too short."])
    errors = validate_tailor_result(result, CANDIDATE)
    assert not any("word" in e for e in errors)
    print("PASS test_validate_ignores_bad_word_counts")


def test_validate_rejects_empty_letter():
    result = _valid_result(letter_paragraphs=[])
    errors = validate_tailor_result(result, CANDIDATE)
    assert any("letter_paragraphs is empty" in e for e in errors)
    print("PASS test_validate_rejects_empty_letter")


def test_validate_treats_max_tokens_stop_reason_as_blocking():
    errors = validate_tailor_result(_valid_result(), CANDIDATE, stop_reason="max_tokens")
    assert any("truncated" in e for e in errors)
    print("PASS test_validate_treats_max_tokens_stop_reason_as_blocking")


def test_requirement_map_status_is_a_pydantic_literal():
    # status is now Literal["met", "partial", "gap"] -- an invalid value
    # can't be constructed at all, so structured outputs can't emit one
    # either. This replaces the old post-hoc string check in validate_*.
    from pydantic import ValidationError

    from jobtracker.generation_schema import RequirementMapEntry
    try:
        RequirementMapEntry(requirement="Python", status="maybe")
    except ValidationError:
        pass
    else:
        raise AssertionError("expected a ValidationError for an invalid status")
    print("PASS test_requirement_map_status_is_a_pydantic_literal")


def test_validate_rejects_unknown_requirement_map_evidence():
    from jobtracker.generation_schema import RequirementMapEntry
    result = _valid_result()
    result.requirement_map = [
        RequirementMapEntry(requirement="Python", evidence=["no-such-id"], status="met")
    ]
    errors = validate_tailor_result(result, CANDIDATE)
    assert any("no-such-id" in e for e in errors)
    print("PASS test_validate_rejects_unknown_requirement_map_evidence")


def test_validate_accepts_requirement_map_evidence_bullet_or_skill():
    from jobtracker.generation_schema import RequirementMapEntry
    result = _valid_result()
    result.requirement_map = [
        RequirementMapEntry(requirement="Python", evidence=["examplecorp-pipelines", "Python"],
                            status="met")
    ]
    errors = validate_tailor_result(result, CANDIDATE)
    assert errors == []
    print("PASS test_validate_accepts_requirement_map_evidence_bullet_or_skill")


# --------------------------------------------------------------------------- #
# ground_tailor_result -- non-blocking warnings                              #
# --------------------------------------------------------------------------- #
def test_ground_flags_invented_employer_and_year():
    result = _valid_result(
        profile="I worked at Initech for 2019 years building things. " * 2,
        letter_paragraphs=["I previously worked at Initech in 2019. " * 10],
    )
    warnings = ground_tailor_result(result, CANDIDATE)
    assert any("Initech" in w for w in warnings)
    assert any("2019" in w for w in warnings)
    print("PASS test_ground_flags_invented_employer_and_year")


def test_ground_does_not_flag_real_facts():
    # Realistic sentence-initial connectors (not just short pronouns like
    # "I"/"My", which happen to be too short for _FACT_TOKEN_RE to match at
    # all and would hide a stoplist gap) plus facts beyond experience/skills
    # (languages, address) that _facts_word_set must also cover.
    result = _valid_result(
        profile="Having worked at Example Corp with Python and SQL, I bring relevant "
                "experience. " * 4,
        letter_paragraphs=[
            "During my time at Example Corp and Acme Software, I used Python daily. "
            "Additionally, I am a native Dutch speaker based in Utrecht. " * 11
        ],
    )
    warnings = ground_tailor_result(result, CANDIDATE)
    assert warnings == []
    print("PASS test_ground_does_not_flag_real_facts")


def test_ground_ignores_common_structural_words():
    result = _valid_result(
        profile="Dear reader, " + "this is a profile paragraph. " * 10,
        letter_paragraphs=["Dear Example Corp team, " + "Sincerely yours. " * 15],
    )
    warnings = ground_tailor_result(result, CANDIDATE)
    assert not any("Dear" in w or "Sincerely" in w for w in warnings)
    print("PASS test_ground_ignores_common_structural_words")


def test_ground_does_not_mask_invented_fact_via_substring():
    # "Ontwikkel" (invented company) must not be hidden by "Ontwikkelde"
    # (a real word in a bullet) -- facts are matched as whole words, not
    # via substring containment.
    result = _valid_result(
        profile="A tailored profile paragraph. " * 10,
        letter_paragraphs=["Ik werkte bij Ontwikkel BV aan diverse projecten. " * 10],
    )
    warnings = ground_tailor_result(result, CANDIDATE)
    assert any("Ontwikkel" in w for w in warnings)
    print("PASS test_ground_does_not_mask_invented_fact_via_substring")


def test_ground_flags_invented_years_of_experience():
    result = _valid_result(
        profile="A tailored profile paragraph. " * 10,
        letter_paragraphs=["I bring five years of experience and have 7 jaar ervaring. " * 8],
    )
    warnings = ground_tailor_result(result, CANDIDATE)
    assert any("7 jaar" in w for w in warnings)
    print("PASS test_ground_flags_invented_years_of_experience")


def test_ground_warns_on_bad_word_counts():
    result = _valid_result(profile="Too short.", letter_paragraphs=["Also too short."])
    warnings = ground_tailor_result(result, CANDIDATE)
    assert any("profile is" in w for w in warnings)
    assert any("letter_paragraphs total" in w for w in warnings)
    print("PASS test_ground_warns_on_bad_word_counts")


def test_ground_extra_allowed_terms_suppresses_employer_name():
    result = _valid_result(
        profile="A tailored profile paragraph. " * 10,
        letter_paragraphs=["I am excited to join Initech as my next step. " * 10],
    )
    warnings_without = ground_tailor_result(result, CANDIDATE)
    assert any("Initech" in w for w in warnings_without)
    warnings_with = ground_tailor_result(result, CANDIDATE, extra_allowed_terms=["Initech"])
    assert not any("Initech" in w for w in warnings_with)
    print("PASS test_ground_extra_allowed_terms_suppresses_employer_name")


# --------------------------------------------------------------------------- #
# AnalysisResult / ApplicationRequirements shape                             #
# --------------------------------------------------------------------------- #
def test_analysis_result_defaults():
    a = AnalysisResult(language="nl")
    assert a.requirements == [] and a.keywords == []
    assert isinstance(a.application_requirements, ApplicationRequirements)
    assert a.application_requirements.referees_required is False
    print("PASS test_analysis_result_defaults")


if __name__ == "__main__":
    test_validate_accepts_well_formed_result()
    test_validate_rejects_unknown_role_id()
    test_validate_rejects_unknown_bullet_id()
    test_validate_rejects_bullet_id_from_wrong_role()
    test_validate_rejects_unknown_skill()
    test_validate_ignores_bad_word_counts()
    test_validate_rejects_empty_letter()
    test_validate_treats_max_tokens_stop_reason_as_blocking()
    test_requirement_map_status_is_a_pydantic_literal()
    test_validate_rejects_unknown_requirement_map_evidence()
    test_validate_accepts_requirement_map_evidence_bullet_or_skill()
    test_ground_flags_invented_employer_and_year()
    test_ground_does_not_flag_real_facts()
    test_ground_ignores_common_structural_words()
    test_ground_does_not_mask_invented_fact_via_substring()
    test_ground_flags_invented_years_of_experience()
    test_ground_warns_on_bad_word_counts()
    test_ground_extra_allowed_terms_suppresses_employer_name()
    test_analysis_result_defaults()
    print("\nAll generation_schema tests passed.")
