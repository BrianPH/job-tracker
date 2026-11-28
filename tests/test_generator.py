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
from unittest.mock import patch

import docx

from jobtracker import generator as generator_module
from jobtracker.candidate_store import LocalJSONCandidateProfileStore
from jobtracker.generation_schema import TailorResult
from jobtracker.generator import (
    ROOT,
    application_paths,
    build_cv_docx,
    build_letter_docx,
    cv_render_context,
    cv_render_context_from_tailor_result,
    detect_language,
    example_letter_text,
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
    # Pin to the example file -- the bare fallback resolves to the real,
    # git-ignored candidate.yaml on a host that has actually run this app.
    c = load_candidate(EXAMPLE_PATH)
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


# --------------------------------------------------------------------------- #
# candidate_profile store integration (job-tracker-candidate-profile-upload   #
# plan): load_candidate()/example_letter_text() with NO explicit path/file    #
# must prefer the store, then fall back to the file-based path exactly as    #
# before the store existed -- a test calling load_candidate(EXAMPLE_PATH)     #
# (an explicit path) never touches the store at all, so these tests always   #
# call the no-argument form.                                                  #
# --------------------------------------------------------------------------- #
def test_load_candidate_prefers_the_stored_profile_over_the_file():
    tmp = Path(tempfile.mkdtemp(prefix="jt_gen_cand_"))
    try:
        store = LocalJSONCandidateProfileStore(str(tmp / "candidate_profile.json"))
        store.save_candidate_data({"personal": {"name": "Stored Candidate"}})
        with patch.object(generator_module, "_get_candidate_store", lambda: store):
            c = load_candidate()
            assert c["personal"]["name"] == "Stored Candidate"
        print("PASS test_load_candidate_prefers_the_stored_profile_over_the_file")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_candidate_falls_back_to_file_when_store_has_no_resume_yet():
    # The resume and the cover-letter style example are saved as SEPARATE
    # actions -- a row that only ever had save_style_example() called on it
    # must fall through to the file path for candidate_data, same as an
    # entirely empty store.
    tmp = Path(tempfile.mkdtemp(prefix="jt_gen_cand_"))
    try:
        store = LocalJSONCandidateProfileStore(str(tmp / "candidate_profile.json"))
        store.save_style_example("Dear hiring manager, ...")
        missing_path = ROOT / "config" / "candidate.yaml.does-not-exist"
        with patch.object(generator_module, "_get_candidate_store", lambda: store), \
             patch.object(generator_module, "CANDIDATE_PATH", missing_path):
            c = load_candidate()
            assert c["personal"]["name"] == "Alex Example"
        print("PASS test_load_candidate_falls_back_to_file_when_store_has_no_resume_yet")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_candidate_tolerates_a_broken_store():
    # DATABASE_URL set but Postgres unreachable (or any other store failure)
    # must fall back to the file path, not break generation entirely.
    def _boom():
        raise RuntimeError("DATABASE_URL set but unreachable")

    missing_path = ROOT / "config" / "candidate.yaml.does-not-exist"
    with patch.object(generator_module, "_get_candidate_store", _boom), \
         patch.object(generator_module, "CANDIDATE_PATH", missing_path):
        c = load_candidate()
        assert c["personal"]["name"] == "Alex Example"
    print("PASS test_load_candidate_tolerates_a_broken_store")


def test_load_candidate_with_explicit_path_never_touches_the_store():
    # Guards the exact regression tests/test_webapp.py's load_candidate pin
    # documents (a bare `load_candidate()` would otherwise pick up whatever
    # real candidate.yaml happens to exist on the host running the test) --
    # if a future change hoists the store lookup above `if path is None`,
    # this must fail loudly instead of the explicit-path callers silently
    # starting to depend on store state.
    #
    # The assertion is made OUTSIDE _get_candidate_store on purpose: raising
    # from inside it does NOT work as a regression guard, because
    # _stored_candidate_data()'s own `except Exception` would catch that
    # raise (AssertionError is an Exception subclass), log it, and return
    # None as if the store were merely empty -- load_candidate(EXAMPLE_PATH)
    # would still return "Alex Example" and this test would pass even after
    # the regression it exists to catch. Recording the call and asserting
    # on the recording afterward cannot be swallowed that way.
    with patch.object(generator_module, "_get_candidate_store") as mock_get_store:
        c = load_candidate(EXAMPLE_PATH)
        assert c["personal"]["name"] == "Alex Example"
    mock_get_store.assert_not_called()
    print("PASS test_load_candidate_with_explicit_path_never_touches_the_store")


def test_example_letter_text_prefers_the_stored_style_example():
    tmp = Path(tempfile.mkdtemp(prefix="jt_gen_letter_"))
    try:
        store = LocalJSONCandidateProfileStore(str(tmp / "candidate_profile.json"))
        store.save_style_example("Geachte heer/mevrouw,\nStored letter body.")
        with patch.object(generator_module, "_get_candidate_store", lambda: store):
            text = example_letter_text("nl")
            assert "Stored letter body." in text
        print("PASS test_example_letter_text_prefers_the_stored_style_example")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_example_letter_text_falls_back_to_file_when_nothing_stored():
    # EXAMPLES_DIR is pinned to a path that does NOT exist -- Solliciteren/
    # is gitignored and absent in this sandbox, but IS bind-mounted and
    # present on the real Pi deploy (docker-compose.yml), so asserting ""
    # without pinning would pass here and fail there the moment a real
    # Motivatie.docx exists.
    tmp = Path(tempfile.mkdtemp(prefix="jt_gen_letter_"))
    try:
        store = LocalJSONCandidateProfileStore(str(tmp / "candidate_profile.json"))  # empty
        missing_examples_dir = tmp / "no-such-examples-dir"
        with patch.object(generator_module, "_get_candidate_store", lambda: store), \
             patch.object(generator_module, "EXAMPLES_DIR", missing_examples_dir):
            text = example_letter_text("nl")
            assert text == ""
        print("PASS test_example_letter_text_falls_back_to_file_when_nothing_stored")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_example_letter_text_tolerates_a_broken_store():
    def _boom():
        raise RuntimeError("DATABASE_URL set but unreachable")

    tmp = Path(tempfile.mkdtemp(prefix="jt_gen_letter_"))
    try:
        missing_examples_dir = tmp / "no-such-examples-dir"
        with patch.object(generator_module, "_get_candidate_store", _boom), \
             patch.object(generator_module, "EXAMPLES_DIR", missing_examples_dir):
            text = example_letter_text("nl")
            assert text == ""  # must not raise; same empty result as "nothing uploaded"
        print("PASS test_example_letter_text_tolerates_a_broken_store")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


def test_letter_render_context_accepts_a_paragraph_list_without_resplitting():
    # jobtracker/generation.py passes TailorResult.letter_paragraphs (a
    # list) directly rather than joining it into a string -- a paragraph
    # that itself happens to contain a newline must survive as ONE
    # paragraph, not get split into two.
    candidate = load_candidate(EXAMPLE_PATH)
    job = {"title": "Data Engineer", "company": "Acme"}
    paragraphs = ["Para one.", "Para two\nwith an internal newline.", "Para three."]
    ctx = letter_render_context(candidate, job, paragraphs, "en")
    assert ctx["body_paragraphs"] == paragraphs
    print("PASS test_letter_render_context_accepts_a_paragraph_list_without_resplitting")


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
    test_load_candidate_prefers_the_stored_profile_over_the_file()
    test_load_candidate_falls_back_to_file_when_store_has_no_resume_yet()
    test_load_candidate_tolerates_a_broken_store()
    test_load_candidate_with_explicit_path_never_touches_the_store()
    test_example_letter_text_prefers_the_stored_style_example()
    test_example_letter_text_falls_back_to_file_when_nothing_stored()
    test_example_letter_text_tolerates_a_broken_store()
    test_application_paths_names_follow_language()
    test_cv_render_context_maps_bullet_bank_by_language()
    test_bullet_text_does_not_leak_other_language()
    test_cv_render_context_skips_malformed_entries_instead_of_crashing()
    test_cv_render_context_handles_non_dict_personal()
    test_cv_render_context_from_tailor_result_selects_and_orders()
    test_cv_render_context_from_tailor_result_skips_stale_ids()
    test_letter_render_context_builds_paragraphs_from_body()
    test_letter_render_context_accepts_a_paragraph_list_without_resplitting()
    test_letter_render_context_falls_back_to_english_for_unknown_lang()
    test_build_cv_docx_renders_both_languages_without_leftover_tags()
    test_build_letter_docx_renders_both_languages_without_leftover_tags()
    print("\nAll generator tests passed.")
