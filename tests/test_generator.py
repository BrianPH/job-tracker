"""Offline tests for the CV/letter generator (fake LLM caller, temp output dir).

Uses config/candidate.example.yaml throughout (via load_candidate()'s fallback,
or explicitly) rather than a real personal config/candidate.yaml, so these
tests pass in any checkout -- see issue #9's acceptance criteria.
"""
import shutil
import tempfile
from pathlib import Path

import docx

from jobtracker.generator import (
    GEN_DESC_CHARS,
    ROOT,
    build_cv_docx,
    build_letter_docx,
    cv_render_context,
    detect_language,
    generate_application,
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


def test_generate_without_llm():
    out = Path(tempfile.mkdtemp(prefix="jt_gen_"))
    try:
        job = {"title": "Bioinformatics Engineer", "company": "TestLab",
               "description": "Develop NGS pipelines in Python."}
        r = generate_application(job, out_dir=out, use_llm=False)
        assert r["cv"].exists() and r["letter"].exists()
        assert r["lang"] == "en" and not r["llm_used"]
        assert _no_unrendered_tags(r["cv"]) and _no_unrendered_tags(r["letter"])
        # documents open and contain the candidate name + job title, from the
        # example candidate.yaml's own data (not hardcoded in generator.py)
        letter = "\n".join(p.text for p in docx.Document(str(r["letter"])).paragraphs)
        assert "Alex Example" in letter and "Bioinformatics Engineer" in letter
        cv = "\n".join(p.text for p in docx.Document(str(r["cv"])).paragraphs)
        assert "Alex Example" in cv and "Example Corp" in cv
        print("PASS test_generate_without_llm")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_template_letter_body_has_no_dutch_leak_in_english():
    # Regression test: candidate.yaml's `highlights` has no `_en` variant.
    # The LLM-free fallback letter must not inject that Dutch text into an
    # English letter just because it has no other use for it.
    out = Path(tempfile.mkdtemp(prefix="jt_gen_"))
    try:
        job = {"title": "Developer", "company": "TestLab", "description": "Build things."}
        r = generate_application(job, out_dir=out, use_llm=False)
        assert r["lang"] == "en"
        letter = "\n".join(p.text for p in docx.Document(str(r["letter"])).paragraphs)
        assert "eigen tijd" not in letter  # the Dutch highlight text in candidate.example.yaml
        print("PASS test_template_letter_body_has_no_dutch_leak_in_english")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_description_up_to_desc_chars_reaches_the_prompt_in_full():
    # Previously the letter/summary prompts truncated the description to
    # 2000/1200 chars on top of whatever the adapter already cut it to --
    # requirements near the end of a long posting were silently dropped.
    # A description at or under GEN_DESC_CHARS must now survive completely.
    out = Path(tempfile.mkdtemp(prefix="jt_gen_"))
    description = ("Relevant requirement near the end. " * 100)[:GEN_DESC_CHARS - 1]
    seen_descriptions = []

    def fake_caller(system, user):
        seen_descriptions.append(user)
        return "A perfectly good generated paragraph of sufficient length. " * 5

    try:
        job = {"title": "Platform Engineer", "company": "TestLab",
               "description": description}
        generate_application(job, out_dir=out, caller=fake_caller)
        assert len(description) > 2000
        assert len(seen_descriptions) == 2  # one call for the letter, one for the CV summary
        for prompt in seen_descriptions:
            # the full description must appear verbatim -- not just the first
            # ~1200/2000 chars of it, as the old per-prompt truncation did.
            assert description in prompt
        print("PASS test_description_up_to_desc_chars_reaches_the_prompt_in_full")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_description_beyond_desc_chars_is_capped():
    # Not every adapter caps description length the way the generic `html`
    # adapter does (jobtracker/adapters.py caps at 4000 chars) -- JSON-API
    # adapters pass descriptions through uncapped. The generator must apply
    # its own bound so an unusually long posting can't blow a local model's
    # context window or run up Anthropic spend unbounded.
    out = Path(tempfile.mkdtemp(prefix="jt_gen_"))
    description = "x" * (GEN_DESC_CHARS * 2)
    seen_descriptions = []

    def fake_caller(system, user):
        seen_descriptions.append(user)
        return "A perfectly good generated paragraph of sufficient length. " * 5

    try:
        job = {"title": "Platform Engineer", "company": "TestLab",
               "description": description}
        generate_application(job, out_dir=out, caller=fake_caller)
        assert len(seen_descriptions) == 2
        for prompt in seen_descriptions:
            assert description not in prompt
            assert ("x" * GEN_DESC_CHARS) in prompt
            assert ("x" * (GEN_DESC_CHARS + 1)) not in prompt
        print("PASS test_description_beyond_desc_chars_is_capped")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_generate_with_fake_llm():
    out = Path(tempfile.mkdtemp(prefix="jt_gen_"))
    try:
        body = ("Ik solliciteer graag. " * 20).strip()
        summary = "Op maat gemaakte profieltekst voor deze functie. " * 3

        def fake_caller(system, user):
            return body if "motivation letter" in system.lower() else summary

        job = {"title": "Software Ontwikkelaar", "company": "NLBedrijf",
               "description": ("Wij zoeken een ontwikkelaar met ervaring binnen het team. "
                               "Je werkzaamheden en vaardigheden groeien in de functie.")}
        r = generate_application(job, out_dir=out, caller=fake_caller)
        assert r["lang"] == "nl" and r["llm_used"]
        assert r["letter"].name == "Motivatiebrief.docx"
        assert _no_unrendered_tags(r["cv"]) and _no_unrendered_tags(r["letter"])
        letter = "\n".join(p.text for p in docx.Document(str(r["letter"])).paragraphs)
        assert "Ik solliciteer graag." in letter
        assert "Geachte heer of mevrouw," in letter
        print("PASS test_generate_with_fake_llm")
    finally:
        shutil.rmtree(out, ignore_errors=True)


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
    assert "Built and maintained Python data pipelines" in first_role_en["bullets_line"]
    assert "Bouwde en onderhield Python-datapipelines" in first_role_nl["bullets_line"]
    print("PASS test_cv_render_context_maps_bullet_bank_by_language")


def test_letter_render_context_builds_paragraphs_from_body():
    candidate = load_candidate(EXAMPLE_PATH)
    job = {"title": "Data Engineer", "company": "Acme"}
    ctx = letter_render_context(candidate, job, "Para one.\nPara two.\n\nPara three.", "en")
    assert ctx["body_paragraphs"] == ["Para one.", "Para two.", "Para three."]
    assert ctx["salutation"] == "Dear Acme team,"
    assert "Data Engineer" in ctx["subject_line"] and "Acme" in ctx["subject_line"]
    print("PASS test_letter_render_context_builds_paragraphs_from_body")


def test_build_cv_docx_renders_both_languages_without_leftover_tags():
    out = Path(tempfile.mkdtemp(prefix="jt_tpl_"))
    try:
        candidate = load_candidate(EXAMPLE_PATH)
        for lang in ("nl", "en"):
            path = build_cv_docx(candidate, "A profile paragraph.", lang, out / f"cv_{lang}.docx")
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
            path = build_letter_docx(candidate, job, "Body paragraph one.\nBody paragraph two.",
                                     lang, out / f"letter_{lang}.docx")
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
    test_generate_without_llm()
    test_template_letter_body_has_no_dutch_leak_in_english()
    test_description_up_to_desc_chars_reaches_the_prompt_in_full()
    test_description_beyond_desc_chars_is_capped()
    test_generate_with_fake_llm()
    test_cv_render_context_maps_bullet_bank_by_language()
    test_letter_render_context_builds_paragraphs_from_body()
    test_build_cv_docx_renders_both_languages_without_leftover_tags()
    test_build_letter_docx_renders_both_languages_without_leftover_tags()
    print("\nAll generator tests passed.")
