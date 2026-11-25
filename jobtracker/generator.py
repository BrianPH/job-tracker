"""Phase 6 / issue #9: per-vacancy CV + motivation letter generation (.docx).

Design principle: the LLM never invents facts. Experience, education, skills and
personal details come verbatim from config/candidate.yaml and are rendered
deterministically. candidate.yaml schema: each `experience` entry has a
stable `id` and a list of `bullets`, each `{id, tags, nl, en}` -- a bullet
bank, not flat bullets_nl/bullets_en lists. The analyse/tailor Claude API
pipeline (jobtracker/tailor.py, issue #10) selects/orders bullet ids and
writes the profile paragraph + letter body; it never writes bullet text
itself, keeping the tailored CV verifiable against these facts. Fields with
an `_en` suffix (title_en, period_en, degree_en, ...) are the English
counterpart of the plain field, with the plain field as fallback.

Rendering uses docxtpl against Jinja-tagged .docx templates in
config/templates/ (cv_nl.docx, cv_en.docx, letter_nl.docx, letter_en.docx),
not a layout built in Python -- so the templates can be your own Word design.
This repo ships a minimal placeholder design; swapping in a real one is a
file replacement, no code change.

This module only holds the facts-loading, render-context, and .docx-writing
pieces -- orchestrating the Claude API call (or the JSON handoff on
failure, issue #11/M3b) lives in jobtracker/generation.py, which is what
jobtracker/webapp.py actually calls.

Output: applications/<Company>_<Title>/CV_<name>.docx + letter .docx.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = ROOT / "config" / "candidate.yaml"
PROFILE_PATH = ROOT / "config" / "profile.yaml"
EXAMPLES_DIR = ROOT / "Solliciteren"
OUTPUT_DIR = ROOT / "applications"
TEMPLATES_DIR = ROOT / "config" / "templates"
CV_TEMPLATE_PATHS = {"nl": TEMPLATES_DIR / "cv_nl.docx", "en": TEMPLATES_DIR / "cv_en.docx"}
LETTER_TEMPLATE_PATHS = {"nl": TEMPLATES_DIR / "letter_nl.docx",
                        "en": TEMPLATES_DIR / "letter_en.docx"}

DUTCH_MARKERS = (" en ", " het ", " een ", " wij ", " jij ", " je ", "ervaring",
                 "werkzaamheden", "functie", "vaardigheden", "kennis van", "binnen")


# --------------------------------------------------------------------------- #
# Inputs                                                                       #
# --------------------------------------------------------------------------- #
def load_candidate(path: Path = CANDIDATE_PATH) -> dict:
    if not path.exists():
        path = ROOT / "config" / "candidate.example.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _personal(candidate: dict) -> dict:
    """candidate.get('personal', {}), guarded against a malformed
    candidate.yaml where `personal` is something other than a mapping (e.g.
    a plain string) -- every call site below does .get() on the result."""
    p = candidate.get("personal", {})
    return p if isinstance(p, dict) else {}


def _lang_field(d: dict, field: str, lang: str) -> str:
    """`{field}_en` when rendering English and present, else the plain field."""
    if lang == "en":
        return d.get(f"{field}_en") or d.get(field, "")
    return d.get(field, "")


def _bullet_text(bullet: dict, lang: str) -> str:
    """A bullet ends up VERBATIM in a rendered document -- falling back to
    the other language would silently leak Dutch bullet text into an
    English CV, so a bullet missing the requested language is dropped
    instead rather than substituted."""
    return bullet.get(lang, "")


def load_profile(path: Path = PROFILE_PATH) -> dict:
    if not path.exists():
        path = ROOT / "config" / "profile.example.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def detect_language(text: str) -> str:
    """'nl' or 'en', by counting Dutch marker words in the vacancy text."""
    t = f" {(text or '').lower()} "
    hits = sum(t.count(m) for m in DUTCH_MARKERS)
    return "nl" if hits >= 3 else "en"


def example_letter_text(lang: str, max_chars: int = 2500) -> str:
    """Concatenated example letters from Solliciteren/Motivatie.docx as a style guide."""
    path = EXAMPLES_DIR / "Motivatie.docx"
    if not path.exists():
        return ""
    try:
        import docx
        doc = docx.Document(str(path))
        paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    except Exception:  # noqa: BLE001 - style guide is optional
        return ""
    text = "\n".join(paras)
    if lang == "en":
        # the file holds both Dutch and English letters; prefer the English part
        m = re.search(r"Dear .*", text, re.DOTALL)
        if m:
            text = m.group(0)
    return text[:max_chars]


# --------------------------------------------------------------------------- #
# .docx rendering via docxtpl -- the LAYOUT lives in config/templates/*.docx,  #
# not in this code, so it can be your own Word design (issue #9). Each        #
# render context below is plain data; see config/templates/ for the Jinja     #
# tags each template expects.                                                 #
# --------------------------------------------------------------------------- #
LETTER_LABELS = {
    "nl": {"subject_prefix": "Betreft: sollicitatie ", "salutation": "Geachte heer of mevrouw,",
          "closing": "Met vriendelijke groet,", "date_fmt": "%d-%m-%Y"},
    "en": {"subject_prefix": "Re: Application for ", "closing": "Sincerely,",
          "date_fmt": "%B %d, %Y"},
}


def _education_context(candidate: dict, lang: str) -> list:
    return [{
        "degree": _lang_field(e, "degree", lang),
        "school": _lang_field(e, "school", lang),
        "period": _lang_field(e, "period", lang),
    } for e in candidate.get("education", []) if isinstance(e, dict)]


def _languages_context(candidate: dict, lang: str) -> list:
    return [
        f"{_lang_field(lg, 'name', lang)} ({_lang_field(lg, 'level', lang)})"
        for lg in candidate.get("languages", [])
        if isinstance(lg, dict) and _lang_field(lg, "name", lang)
    ]


def cv_render_context(candidate: dict, summary: str, lang: str) -> dict:
    """Bullets/skills/languages are exposed as LISTS, not pre-joined strings --
    the template owns list-item layout (e.g. a "List Bullet" paragraph style
    per entry), so swapping in a real Word design can change that layout
    without touching this code. Non-dict entries (a malformed candidate.yaml,
    e.g. a plain string where a {name: ...} dict is expected) are skipped
    rather than crashing -- load_candidate()'s only validation is "is this
    YAML", so a typo here shouldn't take down generation entirely."""
    p = _personal(candidate)
    experience = []
    for e in candidate.get("experience", []):
        if not isinstance(e, dict):
            continue
        bullets = [_bullet_text(b, lang) for b in e.get("bullets", []) if isinstance(b, dict)]
        experience.append({
            "title": _lang_field(e, "title", lang),
            "company": e.get("company", ""),
            "period": _lang_field(e, "period", lang),
            "bullets": [b for b in bullets if b],
        })
    skills = [
        f"{s.get('name','')} ({_lang_field(s, 'level', lang)})" if _lang_field(s, "level", lang)
        else s.get("name", "")
        for s in candidate.get("skills", []) if isinstance(s, dict) and s.get("name")
    ]
    return {
        "name": p.get("name", ""), "address": p.get("address", ""),
        "email": p.get("email", ""), "phone": p.get("phone", ""),
        "profile": summary,
        "experience": experience,
        "education": _education_context(candidate, lang),
        "skills": skills,
        "languages": _languages_context(candidate, lang),
    }


def cv_render_context_from_tailor_result(candidate: dict, result, lang: str) -> dict:
    """issue #11/M3b (wiring the #10 pipeline in): same shape as
    cv_render_context, but experience/bullets/skills are SELECTED AND
    ORDERED from a `generation_schema.TailorResult` instead of dumping every
    role/bullet/skill in candidate.yaml -- the point of the analyse/tailor
    pipeline. Education isn't in TailorResult (nothing to select -- a
    candidate normally has few degrees, so every education entry always
    renders), so it reuses the same full list as cv_render_context. An id
    the model names that no longer exists in candidate.yaml (stale/renamed)
    is skipped rather than raising -- `generation_schema.validate_tailor_result`
    is what should have caught that already; this is just defensive."""
    p = _personal(candidate)
    roles_by_id = {r["id"]: r for r in candidate.get("experience", [])
                  if isinstance(r, dict) and r.get("id")}
    skills_by_name = {s.get("name"): s for s in candidate.get("skills", [])
                      if isinstance(s, dict) and s.get("name")}

    experience = []
    for role_id in result.experience_order:
        role = roles_by_id.get(role_id)
        if role is None:
            continue
        bullets_by_id = {b["id"]: b for b in (role.get("bullets") or [])
                         if isinstance(b, dict) and b.get("id")}
        bullets = [_bullet_text(bullets_by_id[bid], lang) for bid in result.bullets.get(role_id, [])
                  if bid in bullets_by_id]
        experience.append({
            "title": _lang_field(role, "title", lang),
            "company": role.get("company", ""),
            "period": _lang_field(role, "period", lang),
            "bullets": [b for b in bullets if b],
        })

    skills = []
    for name in result.skills:
        s = skills_by_name.get(name)
        if s is None:
            continue
        level = _lang_field(s, "level", lang)
        skills.append(f"{name} ({level})" if level else name)

    return {
        "name": p.get("name", ""), "address": p.get("address", ""),
        "email": p.get("email", ""), "phone": p.get("phone", ""),
        "profile": result.profile,
        "experience": experience,
        "education": _education_context(candidate, lang),
        "skills": skills,
        "languages": _languages_context(candidate, lang),
    }


def letter_render_context(candidate: dict, job: dict, body: str, lang: str) -> dict:
    p = _personal(candidate)
    labels = LETTER_LABELS.get(lang, LETTER_LABELS["en"])
    salutation = labels["salutation"] if lang == "nl" else f"Dear {job.get('company','')} team,"
    body_paragraphs = [b.strip() for b in body.split("\n") if b.strip()]
    return {
        "name": p.get("name", ""), "email": p.get("email", ""), "phone": p.get("phone", ""),
        "date_line": datetime.date.today().strftime(labels["date_fmt"]),
        "subject_line": f"{labels['subject_prefix']}{job.get('title','')} — {job.get('company','')}",
        "salutation": salutation,
        "body_paragraphs": body_paragraphs,
        "closing": labels["closing"],
    }


def build_cv_docx(context: dict, lang: str, path: Path) -> Path:
    """Takes an already-built render context (cv_render_context or
    cv_render_context_from_tailor_result, issue #11/M3b) rather than
    building one itself -- decouples "which context" from "how to render
    it" so both the old full-dump context and the new TailorResult-selected
    one go through the same rendering code."""
    from docxtpl import DocxTemplate
    template = CV_TEMPLATE_PATHS.get(lang, CV_TEMPLATE_PATHS["en"])
    doc = DocxTemplate(str(template))
    doc.render(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def build_letter_docx(context: dict, lang: str, path: Path) -> Path:
    """Same split as build_cv_docx: takes a letter_render_context dict rather
    than building one itself."""
    from docxtpl import DocxTemplate
    template = LETTER_TEMPLATE_PATHS.get(lang, LETTER_TEMPLATE_PATHS["en"])
    doc = DocxTemplate(str(template))
    doc.render(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def _safe(s: str, max_len: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:max_len] or "unknown"


def application_paths(job: dict, candidate: dict, lang: str, out_dir: Path = OUTPUT_DIR):
    """The folder + CV/letter filenames generate_application used to build
    inline -- kept as its own function so jobtracker/generation.py's
    tailor-pipeline orchestration (issue #11/M3b) uses the exact same
    naming/folder convention as the old single-call path did."""
    name_slug = _safe(_personal(candidate).get("name", ""), max_len=30)
    folder = out_dir / f"{_safe(job.get('company',''))}_{_safe(job.get('title',''))}"
    cv_path = folder / (f"CV_{name_slug}.docx" if lang == "nl" else f"Resume_{name_slug}.docx")
    letter_path = folder / ("Motivatiebrief.docx" if lang == "nl" else "Cover_Letter.docx")
    return cv_path, letter_path
