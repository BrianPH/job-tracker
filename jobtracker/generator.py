"""Phase 6 / issue #9: per-vacancy CV + motivation letter generation (.docx).

Design principle: the LLM never invents facts. Experience, education, skills and
personal details come verbatim from config/candidate.yaml and are rendered
deterministically. The LLM only writes two free-text pieces, constrained to
those facts:
  1. a tailored CV profile paragraph, and
  2. the motivation-letter body (style-guided by the examples in Solliciteren/).

candidate.yaml schema: each `experience` entry has a stable `id` and a list of
`bullets`, each `{id, tags, nl, en}` -- a bullet bank, not flat bullets_nl/
bullets_en lists. A future tailoring step selects/orders bullet ids; it never
writes bullet text, keeping the tailored CV verifiable against these facts.
Fields with an `_en` suffix (title_en, period_en, degree_en, ...) are the
English counterpart of the plain field, with the plain field as fallback.

Rendering uses docxtpl against Jinja-tagged .docx templates in
config/templates/ (cv_nl.docx, cv_en.docx, letter_nl.docx, letter_en.docx),
not a layout built in Python -- so the templates can be your own Word design.
This repo ships a minimal placeholder design; swapping in a real one is a
file replacement, no code change.

Language: Dutch or English, auto-detected from the vacancy text (overridable).
Backend: GENERATOR_BACKEND (local Ollama by default; independent of the scorer's
LLM_BACKEND, so switching generation to Anthropic doesn't move scoring off the
free local backend), requested in plain-text mode. Without a configured/reachable
LLM the generator falls back to the static profile summary + a solid template
letter, so it always produces documents.

Output: applications/<Company>_<Title>/CV_<name>.docx + letter .docx.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Callable, Optional

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

# Only the generic `html` adapter caps description length (at 4000 chars,
# jobtracker/adapters.py); JSON-API adapters (ashby, smartrecruiters,
# teamtailor, workday, greenhouse, lever) pass descriptions through
# uncapped. Cap here too, at the same size, so a long posting can't blow a
# local model's context window or run up Anthropic spend unbounded.
GEN_DESC_CHARS = 4000

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


def _bullet_text(bullet: dict, lang: str, strict: bool = True) -> str:
    """Two different consumers need two different policies here:
    - strict=True (default) for anything that ends up VERBATIM in a rendered
      document (cv_render_context, the LLM-free fallback letter): falling
      back to the other language would silently leak Dutch bullet text into
      an English CV, the same risk `highlights` is deliberately excluded
      from in the fallback letter body. A bullet missing the requested
      language is dropped instead.
    - strict=False for _facts_block, which only feeds an LLM prompt, not a
      document -- the model reads the facts and WRITES in the target
      language regardless, so falling back to whichever language a bullet
      actually has avoids losing real achievement detail for a candidate.yaml
      that's only filled in for one language (plausible for a Dutch-market
      tool with an nl-only bullet bank)."""
    if strict:
        return bullet.get(lang, "")
    return bullet.get(lang) or bullet.get("nl") or bullet.get("en") or ""


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
# LLM pieces (facts-constrained)                                               #
# --------------------------------------------------------------------------- #
def _facts_block(candidate: dict, profile: dict, lang: str) -> str:
    p = _personal(candidate)
    exp_lines = []
    for e in candidate.get("experience", []):
        title = _lang_field(e, "title", lang)
        period = _lang_field(e, "period", lang)
        # strict=False: this text only feeds an LLM prompt, not a rendered
        # document -- the model writes in the target language regardless, so
        # an nl-only bullet should still reach it rather than being dropped.
        bullets = [_bullet_text(b, lang, strict=False) for b in e.get("bullets", [])
                  if _bullet_text(b, lang, strict=False)]
        exp_lines.append(f"- {title} @ {e.get('company','')} ({period}): " + " ".join(bullets))
    edu_lines = [
        f"- {_lang_field(e, 'degree', lang)}, {_lang_field(e, 'school', lang)} "
        f"({_lang_field(e, 'period', lang)})"
        for e in candidate.get("education", [])
    ]
    skills = ", ".join(s.get("name", "") for s in candidate.get("skills", []))
    highlights = "\n".join(f"- {h}" for h in candidate.get("highlights", []))
    summary = (profile.get("profile", {}) or {}).get("summary", "")
    return (
        f"Name: {p.get('name','')}\n"
        f"Base summary: {summary}\n"
        f"Skills: {skills}\n"
        f"Experience:\n" + "\n".join(exp_lines) + "\n"
        f"Education:\n" + "\n".join(edu_lines) + "\n"
        f"Additional true facts:\n{highlights}"
    )


LETTER_SYSTEM = (
    "You write the BODY of a motivation letter (cover letter) for a specific vacancy, "
    "on behalf of the candidate whose facts are given.\n"
    "HARD RULES:\n"
    "- Use ONLY the given facts. NEVER invent employers, projects, degrees, skills, "
    "years of experience, or certifications.\n"
    "- Write in {language}. 4 to 6 paragraphs, 250-400 words.\n"
    "- Do NOT include a salutation (Dear .../Geachte ...) or closing (Sincerely/Met "
    "vriendelijke groet) — those are added separately.\n"
    "- Connect the candidate's actual experience and ambitions to what THIS vacancy asks.\n"
    "- Match the tone and structure of the style example, but write fresh text.\n"
    "Output ONLY the letter body text, no headers, no markdown."
)

SUMMARY_SYSTEM = (
    "You write ONE short professional profile paragraph (55-85 words) for the top of a CV, "
    "tailored to a specific vacancy, in {language}.\n"
    "Use ONLY the given facts — never invent skills or experience. Emphasize the parts of "
    "the candidate's background most relevant to the vacancy. "
    "Output ONLY the paragraph, no markdown, no heading."
)


def _lang_name(lang: str) -> str:
    return "Dutch" if lang == "nl" else "English"


def generate_letter_body(job: dict, candidate: dict, profile: dict, lang: str,
                         caller: Optional[Callable[[str, str], str]]) -> str:
    if caller is not None:
        system = LETTER_SYSTEM.replace("{language}", _lang_name(lang))
        user = (
            f"CANDIDATE FACTS:\n{_facts_block(candidate, profile, lang)}\n\n"
            f"VACANCY:\nTitle: {job.get('title','')}\nCompany: {job.get('company','')}\n"
            f"Location: {job.get('location','')}\n"
            f"Description: {(job.get('description') or '')[:GEN_DESC_CHARS]}\n\n"
            f"STYLE EXAMPLE (previous letters by the same candidate):\n"
            f"{example_letter_text(lang)}\n\n"
            f"Write the letter body now, in {_lang_name(lang)}."
        )
        try:
            text = (caller(system, user) or "").strip()
            if len(text) > 200:
                return text
            print(f"  [generator] letter reply too short ({len(text)} chars), falling back to template")
        except Exception as e:  # noqa: BLE001 - fall back to template below
            print(f"  [generator] letter generation failed, falling back to template: {e}")
    return _template_letter_body(job, candidate, lang)


def generate_cv_summary(job: dict, candidate: dict, profile: dict, lang: str,
                        caller: Optional[Callable[[str, str], str]]) -> str:
    base = (profile.get("profile", {}) or {}).get("summary", "").strip()
    if caller is not None:
        system = SUMMARY_SYSTEM.replace("{language}", _lang_name(lang))
        user = (
            f"CANDIDATE FACTS:\n{_facts_block(candidate, profile, lang)}\n\n"
            f"VACANCY:\nTitle: {job.get('title','')}\nCompany: {job.get('company','')}\n"
            f"Description: {(job.get('description') or '')[:GEN_DESC_CHARS]}\n\n"
            f"Write the tailored CV profile paragraph now, in {_lang_name(lang)}."
        )
        try:
            text = (caller(system, user) or "").strip()
            if 100 < len(text) < 900:
                return text
            print(f"  [generator] CV summary reply out of range ({len(text)} chars), falling back to static summary")
        except Exception as e:  # noqa: BLE001
            print(f"  [generator] CV summary generation failed, falling back to static summary: {e}")
    return base


def _template_letter_body(job: dict, candidate: dict, lang: str) -> str:
    """LLM-free fallback: a solid generic body built entirely from candidate.yaml
    facts -- no hardcoded employer/project names, so it stays correct for
    whatever candidate.yaml (or candidate.example.yaml) is actually loaded."""
    title = job.get("title", "")
    company = job.get("company", "")
    roles = candidate.get("experience") or []
    skills = [s.get("name", "") for s in candidate.get("skills", []) if s.get("name")]
    skills_text = ", ".join(skills[:6])
    # candidate.yaml's `highlights` has no `_en` variant (unlike bullets/titles/
    # periods), so it's only safe to use for an nl letter -- don't leak
    # untranslated Dutch text into an English fallback.
    highlight = next(iter(candidate.get("highlights") or []), "") if lang == "nl" else ""

    def role_sentence(role: dict) -> str:
        rtitle = _lang_field(role, "title", lang)
        rcompany = role.get("company", "")
        bullets = role.get("bullets") or []
        bullet = _bullet_text(bullets[0], lang) if bullets else ""
        if not rtitle:
            return ""
        if lang == "nl":
            s = f"Bij {rcompany} werkte ik als {rtitle}." if rcompany else f"Ik werkte als {rtitle}."
        else:
            s = f"At {rcompany}, I worked as {rtitle}." if rcompany else f"I worked as {rtitle}."
        return f"{s} {bullet}".strip()

    role_sentences = [s for s in (role_sentence(r) for r in roles[:2]) if s]

    if lang == "nl":
        opening = f"Met enthousiasme solliciteer ik naar de functie van {title} bij {company}."
        if skills_text:
            opening += f" Mijn ervaring met {skills_text} sluit goed aan bij deze rol."
        closing = "Graag licht ik mijn motivatie en CV toe in een persoonlijk gesprek."
    else:
        opening = f"I am writing to apply for the position of {title} at {company}."
        if skills_text:
            opening += f" My experience with {skills_text} aligns well with this role."
        closing = "I would welcome the opportunity to discuss my motivation and CV in a personal interview."

    paragraphs = [opening, *role_sentences]
    if highlight:
        paragraphs.append(highlight)
    paragraphs.append(closing)
    return "\n\n".join(paragraphs)


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
    education = [{
        "degree": _lang_field(e, "degree", lang),
        "school": _lang_field(e, "school", lang),
        "period": _lang_field(e, "period", lang),
    } for e in candidate.get("education", []) if isinstance(e, dict)]
    skills = [
        f"{s.get('name','')} ({_lang_field(s, 'level', lang)})" if _lang_field(s, "level", lang)
        else s.get("name", "")
        for s in candidate.get("skills", []) if isinstance(s, dict) and s.get("name")
    ]
    languages = [
        f"{_lang_field(lg, 'name', lang)} ({_lang_field(lg, 'level', lang)})"
        for lg in candidate.get("languages", [])
        if isinstance(lg, dict) and _lang_field(lg, "name", lang)
    ]
    return {
        "name": p.get("name", ""), "address": p.get("address", ""),
        "email": p.get("email", ""), "phone": p.get("phone", ""),
        "profile": summary,
        "experience": experience,
        "education": education,
        "skills": skills,
        "languages": languages,
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


def build_cv_docx(candidate: dict, summary: str, lang: str, path: Path) -> Path:
    from docxtpl import DocxTemplate
    template = CV_TEMPLATE_PATHS.get(lang, CV_TEMPLATE_PATHS["en"])
    doc = DocxTemplate(str(template))
    doc.render(cv_render_context(candidate, summary, lang))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def build_letter_docx(candidate: dict, job: dict, body: str, lang: str, path: Path) -> Path:
    from docxtpl import DocxTemplate
    template = LETTER_TEMPLATE_PATHS.get(lang, LETTER_TEMPLATE_PATHS["en"])
    doc = DocxTemplate(str(template))
    doc.render(letter_render_context(candidate, job, body, lang))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def _safe(s: str, max_len: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:max_len] or "unknown"


def generate_application(job: dict, out_dir: Path = OUTPUT_DIR,
                         caller: Optional[Callable[[str, str], str]] = None,
                         lang: Optional[str] = None,
                         use_llm: bool = True) -> dict:
    """Generate a tailored CV + motivation letter for one job (dict with at least
    title/company/description). Returns {'cv': Path, 'letter': Path, 'lang', 'llm_used'}.
    """
    candidate = load_candidate()
    profile = load_profile()
    lang = lang or detect_language(f"{job.get('title','')} {job.get('description','')}")

    if use_llm and caller is None:
        from .scorer import make_caller
        caller = make_caller(json_format=False, temperature=0.4, backend_env="GENERATOR_BACKEND")
    if not use_llm:
        caller = None

    summary = generate_cv_summary(job, candidate, profile, lang, caller)
    body = generate_letter_body(job, candidate, profile, lang, caller)

    name_slug = _safe(_personal(candidate).get("name", ""), max_len=30)
    folder = out_dir / f"{_safe(job.get('company',''))}_{_safe(job.get('title',''))}"
    cv_path = build_cv_docx(candidate, summary, lang,
                            folder / (f"CV_{name_slug}.docx" if lang == "nl"
                                      else f"Resume_{name_slug}.docx"))
    letter_path = build_letter_docx(candidate, job, body, lang,
                                    folder / ("Motivatiebrief.docx" if lang == "nl"
                                              else "Cover_Letter.docx"))
    return {"cv": cv_path, "letter": letter_path, "lang": lang,
            "llm_used": caller is not None}
