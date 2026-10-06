"""Build report/report.html and report/report.pdf from report/report.md.

The HTML links the figures in report/figures/ by relative path (embedded image data would put
random text into the repository); the PDF embeds them.

Needs the optional extra: ``pip install -e .[report]`` (markdown, weasyprint; WeasyPrint uses the
system Pango library).
"""

from __future__ import annotations

import re
from pathlib import Path

import markdown

REPORT = Path(__file__).resolve().parents[1]
SOURCE = REPORT / "report.md"
HTML_OUT = REPORT / "report.html"
PDF_OUT = REPORT / "report.pdf"
TITLE = "Historical road networks: feasibility demonstration"
REPO_URL = "https://github.com/alexanderhellervik/historical-networks-prestudy"
NAV = (
    f'<p class="nav"><a href="report.pdf">PDF version</a> · '
    f'<a href="{REPO_URL}">Code and data on GitHub</a></p>'
)

CSS = """
:root { --ink: #222; --muted: #555; --rule: #ccc; --head: #f3f3f3; }
body { font-family: "DejaVu Serif", Georgia, serif; max-width: 52rem; margin: 2rem auto;
       padding: 0 1rem; line-height: 1.5; color: var(--ink); background: #fff; }
h1 { font-size: 1.6rem; line-height: 1.25; }
h2 { margin-top: 2rem; border-bottom: 1px solid var(--rule); }
table { border-collapse: collapse; font-size: .85rem; margin: 1rem 0; }
th, td { border: 1px solid var(--rule); padding: .25rem .5rem; vertical-align: top; }
th { background: var(--head); }
figure { margin: 1.5rem 0; }
.pair { display: flex; gap: 1rem; align-items: flex-start; }
.pair figure { flex: 1 1 0; margin: 1rem 0; min-width: 0; }
img { max-width: 100%; }
figcaption { font-size: .85rem; color: var(--muted); }
code { font-size: .9em; }
@page { size: A4; margin: 18mm 16mm 20mm 16mm;
        @bottom-center { content: counter(page) " / " counter(pages); font-size: 8pt;
                         color: #777; font-family: "DejaVu Sans", sans-serif; } }
.nav { font-family: "DejaVu Sans", sans-serif; font-size: .9rem; color: var(--muted);
       border-bottom: 1px solid var(--rule); padding-bottom: .5rem; }
@media screen and (max-width: 760px) {
  .pair { flex-direction: column; gap: 0; }
  table { display: block; overflow-x: auto; }
}
@media print {
  body { max-width: none; margin: 0; padding: 0; font-size: 10pt; }
  h1 { font-size: 17pt; } h2 { font-size: 13pt; break-after: avoid; }
  h3 { font-size: 11pt; break-after: avoid; }
  table { font-size: 7.5pt; } tr, figure { break-inside: avoid; }
  figure img { max-height: 200mm; display: block; margin: 0 auto; }
  .pair, .keep { break-inside: avoid; } .pair figure img { max-height: 110mm; }
}
"""


def figure(match: re.Match[str]) -> str:
    alt, src = match.group(1), match.group(2)
    if not (REPORT / src).is_file():
        raise FileNotFoundError(REPORT / src)
    return f'<figure><img alt="{alt}" src="{src}"><figcaption>{alt}</figcaption></figure>'


IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+\.png)\)")


def figures(text: str) -> str:
    """Image lines become figure blocks; two images on one line are set side by side."""
    out = []
    for line in text.splitlines():
        found = IMAGE.findall(line)
        if found and not IMAGE.sub("", line).strip():
            figs = "".join(figure(m) for m in IMAGE.finditer(line))
            cls = "pair" if len(found) > 1 else "single"
            lead = ""
            while out and not out[-1].strip():
                out.pop()
            if out and out[-1].rstrip().endswith(":") and not out[-1].lstrip().startswith("-"):
                lead = markdown.markdown(out.pop())  # the lead-in stays with its figures
            out += ["", f'<div class="keep">{lead}<div class="{cls}">{figs}</div></div>', ""]
        else:
            out.append(line)
    return "\n".join(out)


def build() -> tuple[Path, Path]:
    md = figures(SOURCE.read_text(encoding="utf-8"))
    # two-space list nesting, as GitHub renders it
    body = markdown.markdown(md, extensions=["tables", "fenced_code"], tab_length=2)
    head = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{TITLE}</title><style>{CSS}</style></head><body>"
    )
    # the web page gets a link bar (PDF, repository); the PDF does not
    HTML_OUT.write_text(f"{head}{NAV}{body}</body></html>\n", encoding="utf-8")
    from weasyprint import HTML  # imported here: only the PDF needs the system Pango library

    HTML(string=f"{head}{body}</body></html>", base_url=str(REPORT)).write_pdf(PDF_OUT)
    return HTML_OUT, PDF_OUT


if __name__ == "__main__":
    for p in build():
        print(p, f"{p.stat().st_size / 1e6:.1f} MB")
