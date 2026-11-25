"""Regenerate the placeholder docxtpl templates in config/templates/ (issue #9).

These four .docx files are checked in (see .gitignore: config/templates/*.docx
is the one exception to config/*'s personal-data blanket ignore, since a
template holds layout + Jinja tags only, no facts). They're binaries, so
this script -- not a hand-edited Word file -- is what makes them
reproducible and reviewable: regenerate after any layout change here and
commit the result.

Repeating sections use docxtpl's documented "paragraph tag" loop
({%p for ... %} / {%p endfor %}), which removes the tag paragraph itself and
repeats whatever paragraphs sit between the for/endfor markers -- including
nested, for a bullet list inside each experience entry. Every Jinja tag is
written as a SINGLE python-docx run, so there's no risk of Word splitting a
tag across runs (a common docxtpl corruption source when editing a template
by hand -- doesn't apply to this generated output, but keeps hand-edits of
the placeholder safe too).

Usage:
    python build_templates.py
"""
from __future__ import annotations

from pathlib import Path

import docx
from docx.shared import Pt, RGBColor

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "config" / "templates"


def _base_doc():
    doc = docx.Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    return doc


def _heading(doc, text: str, size: int = 13):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
    return p


def _tag_para(doc, tag: str):
    """A paragraph whose ENTIRE text is one Jinja/docxtpl tag, as a single run."""
    doc.add_paragraph().add_run(tag)


def build_cv_template(path: Path, lang: str):
    labels = {
        "nl": dict(profile="PROFIEL", experience="WERKERVARING", education="OPLEIDING",
                  skills="VAARDIGHEDEN", languages="TALEN"),
        "en": dict(profile="PROFILE", experience="WORK EXPERIENCE", education="EDUCATION",
                  skills="SKILLS", languages="LANGUAGES"),
    }[lang]

    doc = _base_doc()

    name_p = doc.add_paragraph()
    r = name_p.add_run("{{ name }}")
    r.bold = True
    r.font.size = Pt(20)
    doc.add_paragraph("{{ address }}  |  {{ email }}  |  {{ phone }}")

    _heading(doc, labels["profile"])
    doc.add_paragraph("{{ profile }}")

    _heading(doc, labels["experience"])
    _tag_para(doc, "{%p for exp in experience %}")
    head = doc.add_paragraph()
    head.add_run("{{ exp.title }} — {{ exp.company }}").bold = True
    head.add_run("   ({{ exp.period }})").italic = True
    _tag_para(doc, "{%p for b in exp.bullets %}")
    doc.add_paragraph("{{ b }}", style="List Bullet")
    _tag_para(doc, "{%p endfor %}")
    _tag_para(doc, "{%p endfor %}")

    _heading(doc, labels["education"])
    _tag_para(doc, "{%p for edu in education %}")
    eline = doc.add_paragraph()
    eline.add_run("{{ edu.degree }} — {{ edu.school }}").bold = True
    eline.add_run("   ({{ edu.period }})").italic = True
    _tag_para(doc, "{%p endfor %}")

    _heading(doc, labels["skills"])
    _tag_para(doc, "{%p for s in skills %}")
    doc.add_paragraph("{{ s }}", style="List Bullet")
    _tag_para(doc, "{%p endfor %}")

    _heading(doc, labels["languages"])
    _tag_para(doc, "{%p for lg in languages %}")
    doc.add_paragraph("{{ lg }}", style="List Bullet")
    _tag_para(doc, "{%p endfor %}")

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


def build_letter_template(path: Path, lang: str):
    doc = _base_doc()

    name_p = doc.add_paragraph()
    r = name_p.add_run("{{ name }}")
    r.bold = True
    r.font.size = Pt(14)
    doc.add_paragraph("{{ email }}  |  {{ phone }}")
    doc.add_paragraph("{{ date_line }}")
    doc.add_paragraph("")

    subject = doc.add_paragraph()
    subject.add_run("{{ subject_line }}").bold = True
    doc.add_paragraph("")

    doc.add_paragraph("{{ salutation }}")
    doc.add_paragraph("")
    _tag_para(doc, "{%p for para in body_paragraphs %}")
    doc.add_paragraph("{{ para }}")
    _tag_para(doc, "{%p endfor %}")
    doc.add_paragraph("")
    doc.add_paragraph("{{ closing }}")
    doc.add_paragraph("{{ name }}")

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


def main():
    for lang in ("nl", "en"):
        build_cv_template(TEMPLATES_DIR / f"cv_{lang}.docx", lang)
        build_letter_template(TEMPLATES_DIR / f"letter_{lang}.docx", lang)
    print(f"Wrote templates to {TEMPLATES_DIR}")


if __name__ == "__main__":
    main()
