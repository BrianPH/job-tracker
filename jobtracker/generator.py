"""Phase 6: per-vacancy CV + motivation letter generation (.docx).

Design principle: the LLM never invents facts. Experience, education, skills and
personal details come verbatim from config/candidate.yaml and are rendered
deterministically. The LLM only writes two free-text pieces, constrained to
those facts:
  1. a tailored CV profile paragraph, and
  2. the motivation-letter body (style-guided by the examples in Solliciteren/).

Language: Dutch or English, auto-detected from the vacancy text (overridable).
Backend: GENERATOR_BACKEND (local Ollama by default; independent of the scorer's
LLM_BACKEND, so switching generation to Anthropic doesn't move scoring off the
free local backend), requested in plain-text mode. Without a configured/reachable
LLM the generator falls back to the static profile summary + a solid template
letter, so it always produces documents.

Output: applications/<Company>_<Title>/CV_Brian_Haubrich.docx + letter .docx.
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
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


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
    p = candidate.get("personal", {})
    exp_lines = []
    for e in candidate.get("experience", []):
        title = e.get("title" if lang == "nl" else "title_en") or e.get("title", "")
        period = e.get("period" if lang == "nl" else "period_en") or e.get("period", "")
        bullets = e.get(f"bullets_{lang}") or e.get("bullets_nl") or []
        exp_lines.append(f"- {title} @ {e.get('company','')} ({period}): " + " ".join(bullets))
    edu_lines = [
        f"- {e.get('degree' if lang == 'nl' else 'degree_en') or e.get('degree','')}, "
        f"{e.get('school' if lang == 'nl' else 'school_en') or e.get('school','')} "
        f"({e.get('period' if lang == 'nl' else 'period_en') or e.get('period','')})"
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
    """LLM-free fallback: a solid generic body built from real facts."""
    name_company = job.get("company", "")
    title = job.get("title", "")
    if lang == "nl":
        return (
            f"Met enthousiasme solliciteer ik naar de functie van {title} bij {name_company}. "
            "Als informatica-afgestudeerde met ervaring in softwareontwikkeling met Python en "
            "C#/.NET, SQL-databases (PostgreSQL, SQL Server), Docker, Azure/AWS en CI/CD-pipelines, "
            "sluit deze rol goed aan bij mijn achtergrond en ambities.\n\n"
            "Tijdens mijn stages bij Quintor en Depot Software heb ik een sterke basis gelegd in "
            "modern software-ontwerp, versiebeheer en agile werken. Momenteel werk ik als Project "
            "Manager bij Ridder Growing Solutions, waar ik technische projecten coördineer en "
            "requirements afstem met uiteenlopende stakeholders.\n\n"
            "Mijn langetermijnambitie ligt op het snijvlak van IT en life sciences, met de intentie "
            "om een master bio-informatica te volgen. Ik verdiep mij al zelfstandig in algoritmische "
            "bio-informatica, zoals Hamming-afstanden voor DNA-foutcorrectie en RNA-structuuranalyse.\n\n"
            "Graag licht ik mijn motivatie en CV toe in een persoonlijk gesprek."
        )
    return (
        f"I am writing to apply for the position of {title} at {name_company}. As a Computer "
        "Science graduate with hands-on experience in Python and C#/.NET development, SQL databases "
        "(PostgreSQL, SQL Server), Docker, Azure/AWS and CI/CD pipelines, this role aligns well "
        "with my background and ambitions.\n\n"
        "During my internships at Quintor and Depot Software I built a strong foundation in modern "
        "software design, version control and agile development. I currently work as a Project "
        "Manager at Ridder Growing Solutions, coordinating technical projects and aligning "
        "requirements with diverse stakeholders.\n\n"
        "My long-term ambition lies at the intersection of IT and life sciences, with plans to "
        "pursue a Master's in Bioinformatics. I am already exploring the field independently "
        "through algorithmic challenges such as Hamming distances for DNA error correction and "
        "RNA structure analysis.\n\n"
        "I would welcome the opportunity to discuss my motivation and CV in a personal interview."
    )


# --------------------------------------------------------------------------- #
# .docx rendering                                                              #
# --------------------------------------------------------------------------- #
def _base_doc():
    import docx
    from docx.shared import Pt
    doc = docx.Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    return doc


def _heading(doc, text: str, size: int = 13):
    from docx.shared import Pt, RGBColor
    p = doc.add_paragraph()
    run = p.add_run(text.upper())
    run.bold = True
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
    return p


def build_cv_docx(candidate: dict, summary: str, lang: str, path: Path) -> Path:
    from docx.shared import Pt
    doc = _base_doc()
    p = candidate.get("personal", {})

    name_p = doc.add_paragraph()
    run = name_p.add_run(p.get("name", ""))
    run.bold = True
    run.font.size = Pt(20)
    doc.add_paragraph(f"{p.get('address','')}  |  {p.get('email','')}  |  {p.get('phone','')}")

    _heading(doc, "Profiel" if lang == "nl" else "Profile")
    doc.add_paragraph(summary)

    _heading(doc, "Werkervaring" if lang == "nl" else "Work Experience")
    for e in candidate.get("experience", []):
        title = e.get("title" if lang == "nl" else "title_en") or e.get("title", "")
        period = e.get("period" if lang == "nl" else "period_en") or e.get("period", "")
        head = doc.add_paragraph()
        r = head.add_run(f"{title} — {e.get('company','')}")
        r.bold = True
        head.add_run(f"   ({period})").italic = True
        for b in e.get(f"bullets_{lang}") or e.get("bullets_nl") or []:
            doc.add_paragraph(b, style="List Bullet")

    _heading(doc, "Opleiding" if lang == "nl" else "Education")
    for e in candidate.get("education", []):
        degree = e.get("degree" if lang == "nl" else "degree_en") or e.get("degree", "")
        school = e.get("school" if lang == "nl" else "school_en") or e.get("school", "")
        period = e.get("period" if lang == "nl" else "period_en") or e.get("period", "")
        line = doc.add_paragraph()
        line.add_run(f"{degree} — {school}").bold = True
        line.add_run(f"   ({period})").italic = True

    _heading(doc, "Vaardigheden" if lang == "nl" else "Skills")
    for s in candidate.get("skills", []):
        lvl = s.get("level" if lang == "nl" else "level_en") or s.get("level", "")
        doc.add_paragraph(f"{s.get('name','')} — {lvl}", style="List Bullet")

    _heading(doc, "Talen" if lang == "nl" else "Languages")
    for l in candidate.get("languages", []):
        nm = l.get("name" if lang == "nl" else "name_en") or l.get("name", "")
        lvl = l.get("level" if lang == "nl" else "level_en") or l.get("level", "")
        doc.add_paragraph(f"{nm} — {lvl}", style="List Bullet")

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def build_letter_docx(candidate: dict, job: dict, body: str, lang: str, path: Path) -> Path:
    from docx.shared import Pt
    doc = _base_doc()
    p = candidate.get("personal", {})

    name_p = doc.add_paragraph()
    r = name_p.add_run(p.get("name", ""))
    r.bold = True
    r.font.size = Pt(14)
    doc.add_paragraph(f"{p.get('email','')}  |  {p.get('phone','')}")
    today = datetime.date.today()
    doc.add_paragraph(today.strftime("%d-%m-%Y") if lang == "nl" else today.strftime("%B %d, %Y"))
    doc.add_paragraph("")

    subject = doc.add_paragraph()
    label = "Betreft: sollicitatie " if lang == "nl" else "Re: Application for "
    subject.add_run(f"{label}{job.get('title','')} — {job.get('company','')}").bold = True
    doc.add_paragraph("")

    doc.add_paragraph("Geachte heer of mevrouw," if lang == "nl" else f"Dear {job.get('company','')} team,")
    doc.add_paragraph("")
    for para in [b for b in body.split("\n") if b.strip()]:
        doc.add_paragraph(para.strip())
    doc.add_paragraph("")
    doc.add_paragraph("Met vriendelijke groet," if lang == "nl" else "Sincerely,")
    doc.add_paragraph(p.get("name", ""))

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

    folder = out_dir / f"{_safe(job.get('company',''))}_{_safe(job.get('title',''))}"
    cv_path = build_cv_docx(candidate, summary, lang,
                            folder / ("CV_Brian_Haubrich.docx" if lang == "nl"
                                      else "Resume_Brian_Haubrich.docx"))
    letter_path = build_letter_docx(candidate, job, body, lang,
                                    folder / ("Motivatiebrief.docx" if lang == "nl"
                                              else "Cover_Letter.docx"))
    return {"cv": cv_path, "letter": letter_path, "lang": lang,
            "llm_used": caller is not None}
