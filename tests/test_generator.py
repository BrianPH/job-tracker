"""Offline tests for the CV/letter generator (fake LLM caller, temp output dir)."""
import shutil
import tempfile
from pathlib import Path

from jobtracker.generator import (
    DESC_CHARS,
    detect_language,
    generate_application,
    load_candidate,
)


def test_detect_language():
    nl = ("Wij zoeken een ontwikkelaar met ervaring in Python. Je werkzaamheden zijn "
          "divers en je vaardigheden groeien binnen het team en de functie.")
    en = "We are looking for a developer to build NGS pipelines with Python and Docker."
    assert detect_language(nl) == "nl"
    assert detect_language(en) == "en"
    print("PASS test_detect_language")


def test_candidate_config_loads():
    c = load_candidate()
    assert c["personal"]["name"] == "Brian Haubrich"
    assert c["experience"] and c["education"] and c["skills"]
    print("PASS test_candidate_config_loads")


def test_generate_without_llm():
    out = Path(tempfile.mkdtemp(prefix="jt_gen_"))
    try:
        job = {"title": "Bioinformatics Engineer", "company": "TestLab",
               "description": "Develop NGS pipelines in Python."}
        r = generate_application(job, out_dir=out, use_llm=False)
        assert r["cv"].exists() and r["letter"].exists()
        assert r["lang"] == "en" and not r["llm_used"]
        # documents open and contain the candidate name + job title
        import docx
        letter = "\n".join(p.text for p in docx.Document(str(r["letter"])).paragraphs)
        assert "Brian Haubrich" in letter and "Bioinformatics Engineer" in letter
        cv = "\n".join(p.text for p in docx.Document(str(r["cv"])).paragraphs)
        assert "Brian Haubrich" in cv and "Quintor" in cv
        print("PASS test_generate_without_llm")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_description_up_to_desc_chars_reaches_the_prompt_in_full():
    # Previously the letter/summary prompts truncated the description to
    # 2000/1200 chars on top of whatever the adapter already cut it to --
    # requirements near the end of a long posting were silently dropped.
    # A description at or under DESC_CHARS must now survive completely.
    out = Path(tempfile.mkdtemp(prefix="jt_gen_"))
    description = ("Relevant requirement near the end. " * 100)[:DESC_CHARS - 1]
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
    description = "x" * (DESC_CHARS * 2)
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
            assert ("x" * DESC_CHARS) in prompt
            assert ("x" * (DESC_CHARS + 1)) not in prompt
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
        import docx
        letter = "\n".join(p.text for p in docx.Document(str(r["letter"])).paragraphs)
        assert "Ik solliciteer graag." in letter
        assert "Geachte heer of mevrouw," in letter
        print("PASS test_generate_with_fake_llm")
    finally:
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    test_detect_language()
    test_candidate_config_loads()
    test_generate_without_llm()
    test_description_up_to_desc_chars_reaches_the_prompt_in_full()
    test_description_beyond_desc_chars_is_capped()
    test_generate_with_fake_llm()
    print("\nAll generator tests passed.")
