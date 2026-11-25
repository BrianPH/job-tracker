"""Offline tests for the facts-loading/render-context/.docx pieces of CV +
motivation letter generation. The actual Claude API orchestration (issue #10)
and JSON handoff (issue #11/M3b) live in jobtracker/tailor.py and
jobtracker/generation.py, tested in tests/test_tailor.py and
tests/test_generation.py respectively.

Uses config/candidate.example.yaml throughout (via load_candidate()'s fallback,
or explicitly) rather than a real personal config/candidate.yaml, so these
tests pass in any checkout -- see issue #9's acceptance criteria.
"""
import shutil
import tempfile
from pathlib import Path

import docx

from jobtracker.generation_schema import TailorResult
from jobtracker.generator import (
    ROOT,
    application_paths,
    build_cv_docx,
    build_letter_docx,
    cv_render_context,
    cv_render_context_from_tailor_result,
    detect_language,
    letter_render_context,
    load_candidate,
)

EXAMPLE_PATH = ROOT / "config" / "candidate.example.yaml"


def _no_unrendered_tags(doc_path: Path) -> bool:
    """True if the rendered .docx has no leftover Jinja syntax -- a template/
    context mismatch (e.g. a renamed context key) typically leaves a literal
    "{{ ... }}" or "{%p ... %}" in the output instead of raising."""
    text = "\n".join(p.text for p in docx.Document(str(doc_path)).paragraphs)
    return "{{" not in text and "{%" not in text


def test_detect_language():
    nl = ("Wij zoeken een ontwikkelaar met ervaring in Python. Je werkzaamheden zijn "
          "divers en je vaardigheden groeien binnen het team en de functie.")
    en = "We are looking for a developer to build NGS pipelines with Python and Docker."
    assert detect_language(nl) == "nl"
    assert detect_language(en) == "en"
    print("PASS test_detect_language")


def test_candidate_config_loads():
    c = load_candidate()
    assert c["personal"]["name"]
    assert c["experience"] and c["education"] and c["skills"]
    # bullet-bank shape: every bullet has a stable id + bilingual text
    for role in c["experience"]:
        assert role.get("id")
        for bullet in role.get("bullets", []):
            assert bullet.get("id")
            assert bullet.get("nl") or bullet.get("en")
    print("PASS test_candidate_config_loads")


def test_load_candidate_falls_back_to_example_when_missing():
    missing = ROOT / "config" / "candidate.yaml.does-not-exist"
    c = load_candidate(missing)
    assert c["personal"]["name"] == "Alex Example"
    print("PASS test_load_candidate_falls_back_to_example_when_missing")


def test_application_paths_names_follow_language():
    candidate = {"personal": {"name": "Alex Example"}}
    cv_nl, letter_nl = application_paths({"title": "Dev", "company": "Acme"}, candidate, "nl")
    cv_en, letter_en = application_paths({"title": "Dev", "company": "Acme"}, candidate, "en")
    assert cv_nl.name == "CV_Alex_Example.docx" and letter_nl.name == "Motivatiebrief.docx"
    assert cv_en.name == "Resume_Alex_Example.docx" and letter_en.name == "Cover_Letter.docx"
    assert cv_nl.parent.name == "Acme_Dev"
    print("PASS test_application_paths_names_follow_language")


# --------------------------------------------------------------------------- #
# issue #9: docxtpl rendering against the new bullet-bank schema              #
# --------------------------------------------------------------------------- #
def test_cv_render_context_maps_bullet_bank_by_language():
    candidate = load_candidate(EXAMPLE_PATH)
    ctx_en = cv_render_context(candidate, "A summary.", "en")
    ctx_nl = cv_render_context(candidate, "Een samenvatting.", "nl")
    first_role_en = ctx_en["experience"][0]
    first_role_nl = ctx_nl["experience"][0]
    assert first_role_en["title"] == "Software Developer"
    assert first_role_nl["title"] == "Software Ontwikkelaar"
    assert any("Built and maintained Python data pipelines" in b
              for b in first_role_en["bullets"])
    assert any("Bouwde en onderhield Python-datapipelines" in b
              for b in first_role_nl["bullets"])
    print("PASS test_cv_render_context_maps_bullet_bank_by_language")


def test_bullet_text_does_not_leak_other_language():
    # Regression test: a bullet missing the requested language must be
    # dropped, not silently rendered in the wrong language.
    candidate = {"experience": [{"id": "x", "company": "Acme", "bullets": [
        {"id": "b1", "nl": "Alleen Nederlandse tekst."},
    ]}]}
    ctx = cv_render_context(candidate, "summary", "en")
    assert ctx["experience"][0]["bullets"] == []
    print("PASS test_bullet_text_does_not_leak_other_language")


def test_cv_render_context_skips_malformed_entries_instead_of_crashing():
    # A malformed candidate.yaml (e.g. a plain string where a {name: ...}
    # dict is expected) must not take down generation entirely.
    candidate = {"experience": "not a list of dicts", "education": [],
                "skills": "Python", "languages": [{"not": "a name field"}]}
    ctx = cv_render_context(candidate, "summary", "en")
    assert ctx["experience"] == [] and ctx["skills"] == [] and ctx["languages"] == []
    print("PASS test_cv_render_context_skips_malformed_entries_instead_of_crashing")


def test_cv_render_context_handles_non_dict_personal():
    candidate = {"personal": "not a dict", "experience": [], "education": [],
                "skills": [], "languages": []}
    ctx = cv_render_context(candidate, "summary", "en")
    assert ctx["name"] == ""
    print("PASS test_cv_render_context_handles_non_dict_personal")


def _tailor_candidate():
    return {
        "personal": {"name": "Alex Example"},
        "experience": [
            {"id": "corp-a", "title": "Developer", "title_en": "Developer", "company": "Corp A",
            "bullets": [{"id": "a1", "en": "Built pipelines."}, {"id": "a2", "en": "Led a team."}]},
            {"id": "corp-b", "title": "Intern", "title_en": "Intern", "company": "Corp B",
            "bullets": [{"id": "b1", "en": "Wrote tests."}]},
        ],
        "education": [{"id": "uni", "degree": "BSc", "school": "Test University"}],
        "skills": [{"name": "Python"}, {"name": "SQL"}],
    }


def test_cv_render_context_from_tailor_result_selects_and_orders():
    candidate = _tailor_candidate()
    result = TailorResult(
        language="en", profile="A tailored profile.",
        experience_order=["corp-b", "corp-a"],  # deliberately reversed vs. candidate.yaml order
        bullets={"corp-a": ["a2"], "corp-b": ["b1"]},  # only a2, not a1
        skills=["SQL"],  # only SQL, not Python
        letter_paragraphs=["Body."],
    )
    ctx = cv_render_context_from_tailor_result(candidate, result, "en")
    assert [e["company"] for e in ctx["experience"]] == ["Corp B", "Corp A"]
    assert ctx["experience"][1]["bullets"] == ["Led a team."]  # a1 excluded
    assert ctx["skills"] == ["SQL"]
    assert ctx["profile"] == "A tailored profile."
    assert ctx["education"] == [{"degree": "BSc", "school": "Test University", "period": ""}]
    print("PASS test_cv_render_context_from_tailor_result_selects_and_orders")


def test_cv_render_context_from_tailor_result_skips_stale_ids():
    # A role/bullet/skill id the model names that no longer exists in
    # candidate.yaml (stale/renamed) must be skipped, not raise -- the
    # validator is what should have caught that already; this is defensive.
    candidate = _tailor_candidate()
    result = TailorResult(
        language="en", profile="A tailored profile.",
        experience_order=["corp-a", "no-such-role"],
        bullets={"corp-a": ["a1", "no-such-bullet"]},
        skills=["Python", "No Such Skill"],
        letter_paragraphs=["Body."],
    )
    ctx = cv_render_context_from_tailor_result(candidate, result, "en")
    assert len(ctx["experience"]) == 1
    assert ctx["experience"][0]["bullets"] == ["Built pipelines."]
    assert ctx["skills"] == ["Python"]
    print("PASS test_cv_render_context_from_tailor_result_skips_stale_ids")


def test_letter_render_context_builds_paragraphs_from_body():
    candidate = load_candidate(EXAMPLE_PATH)
    job = {"title": "Data Engineer", "company": "Acme"}
    ctx = letter_render_context(candidate, job, "Para one.\nPara two.\n\nPara three.", "en")
    assert ctx["body_paragraphs"] == ["Para one.", "Para two.", "Para three."]
    assert ctx["salutation"] == "Dear Acme team,"
    assert "Data Engineer" in ctx["subject_line"] and "Acme" in ctx["subject_line"]
    print("PASS test_letter_render_context_builds_paragraphs_from_body")


def test_letter_render_context_falls_back_to_english_for_unknown_lang():
    candidate = load_candidate(EXAMPLE_PATH)
    job = {"title": "Data Engineer", "company": "Acme"}
    ctx = letter_render_context(candidate, job, "Body.", "de")
    assert ctx["closing"] == "Sincerely,"  # English labels, not a KeyError
    print("PASS test_letter_render_context_falls_back_to_english_for_unknown_lang")


def test_build_cv_docx_renders_both_languages_without_leftover_tags():
    out = Path(tempfile.mkdtemp(prefix="jt_tpl_"))
    try:
        candidate = load_candidate(EXAMPLE_PATH)
        for lang in ("nl", "en"):
            ctx = cv_render_context(candidate, "A profile paragraph.", lang)
            path = build_cv_docx(ctx, lang, out / f"cv_{lang}.docx")
            assert path.exists()
            assert _no_unrendered_tags(path)
            text = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
            assert "Example Corp" in text
            assert "Acme Software" in text  # second experience entry also rendered
        print("PASS test_build_cv_docx_renders_both_languages_without_leftover_tags")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_build_letter_docx_renders_both_languages_without_leftover_tags():
    out = Path(tempfile.mkdtemp(prefix="jt_tpl_"))
    try:
        candidate = load_candidate(EXAMPLE_PATH)
        job = {"title": "Data Engineer", "company": "Acme"}
        for lang in ("nl", "en"):
            ctx = letter_render_context(candidate, job, "Body paragraph one.\nBody paragraph two.", lang)
            path = build_letter_docx(ctx, lang, out / f"letter_{lang}.docx")
            assert path.exists()
            assert _no_unrendered_tags(path)
            text = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
            assert "Body paragraph one." in text and "Body paragraph two." in text
        print("PASS test_build_letter_docx_renders_both_languages_without_leftover_tags")
    finally:
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    test_detect_language()
    test_candidate_config_loads()
    test_load_candidate_falls_back_to_example_when_missing()
    test_application_paths_names_follow_language()
    test_cv_render_context_maps_bullet_bank_by_language()
    test_bullet_text_does_not_leak_other_language()
    test_cv_render_context_skips_malformed_entries_instead_of_crashing()
    test_cv_render_context_handles_non_dict_personal()
    test_cv_render_context_from_tailor_result_selects_and_orders()
    test_cv_render_context_from_tailor_result_skips_stale_ids()
    test_letter_render_context_builds_paragraphs_from_body()
    test_letter_render_context_falls_back_to_english_for_unknown_lang()
    test_build_cv_docx_renders_both_languages_without_leftover_tags()
    test_build_letter_docx_renders_both_languages_without_leftover_tags()
    print("\nAll generator tests passed.")
