#!/usr/bin/env python3
"""
Genera il PDF della documentazione API a partire dal sorgente Markdown.

    python3 build_pdf.py booking_api.md BOOKING_API.pdf

Il sorgente Markdown resta la fonte di verità: è diffabile in git e leggibile
su GitHub. Il PDF è un artefatto derivato, rigenerato a ogni modifica.
"""
import subprocess
import sys
from pathlib import Path

import markdown

CHROMIUM_CANDIDATES = (
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

CSS = """
@page { size: A4; margin: 18mm 15mm 20mm 15mm; }

:root {
  --ink: #1a1a1a;
  --muted: #5c6370;
  --line: #d8dce3;
  --accent: #1f4e79;
  --soft: #f4f6f9;
  --warn: #8a5a00;
  --warn-bg: #fdf6e3;
}

* { box-sizing: border-box; }

body {
  font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 9.6pt;
  line-height: 1.5;
  color: var(--ink);
  margin: 0;
}

h1 {
  font-size: 22pt; color: var(--accent); margin: 0 0 4pt;
  letter-spacing: -0.3pt; line-height: 1.15;
}
h2 {
  font-size: 14pt; color: var(--accent); margin: 22pt 0 7pt;
  padding-bottom: 4pt; border-bottom: 1.5pt solid var(--accent);
  page-break-after: avoid; break-after: avoid;
}
h3 {
  font-size: 11.5pt; margin: 15pt 0 5pt; color: #111;
  page-break-after: avoid; break-after: avoid;
}
h4 {
  font-size: 10pt; margin: 11pt 0 4pt; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.4pt;
  page-break-after: avoid; break-after: avoid;
}

p { margin: 0 0 7pt; }
ul, ol { margin: 0 0 7pt; padding-left: 16pt; }
li { margin-bottom: 2.5pt; }

hr { border: 0; border-top: 1pt solid var(--line); margin: 16pt 0; }

table {
  width: 100%; border-collapse: collapse; margin: 7pt 0 11pt;
  font-size: 8.7pt; page-break-inside: avoid; break-inside: avoid;
}
th {
  background: var(--accent); color: #fff; text-align: left;
  padding: 4.5pt 6pt; font-weight: 600; font-size: 8.4pt;
}
td { padding: 4pt 6pt; border-bottom: 0.6pt solid var(--line); vertical-align: top; }
tr:nth-child(even) td { background: #fafbfc; }

code {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 8.4pt; background: var(--soft);
  padding: 1pt 3.5pt; border-radius: 2.5pt; color: #b3236a;
}
pre {
  background: #1f2430; color: #e6e6e6; padding: 8pt 10pt;
  border-radius: 4pt; overflow-x: auto; font-size: 8.2pt;
  line-height: 1.45; page-break-inside: avoid; break-inside: avoid;
  margin: 7pt 0 11pt;
}
pre code { background: none; color: inherit; padding: 0; font-size: 8.2pt; }

blockquote {
  margin: 8pt 0; padding: 7pt 11pt;
  background: var(--warn-bg); border-left: 3pt solid var(--warn);
  color: #4a3a12; page-break-inside: avoid; break-inside: avoid;
}
blockquote p { margin: 0 0 4pt; }
blockquote p:last-child { margin-bottom: 0; }

strong { font-weight: 600; }

h2 + table, h3 + table { margin-top: 5pt; }

/* Tabelle senza intestazione (usate come schede proprietà/valore): Markdown
   genera comunque un <thead> con celle vuote, che verrebbe reso come una
   barra colorata priva di contenuto. */
thead:has(th:empty) { display: none; }
thead:has(th:empty) + tbody td:first-child { font-weight: 600; width: 22%; }
"""


def find_chromium() -> str:
    for candidate in CHROMIUM_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise SystemExit(
        "Chromium non trovato. Installalo oppure aggiungi il percorso a "
        "CHROMIUM_CANDIDATES."
    )


def build(source: Path, output: Path) -> None:
    source = source.resolve()
    output = output.resolve()

    html_body = markdown.markdown(
        source.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
    )

    document = (
        "<!DOCTYPE html><html lang='it'><head><meta charset='utf-8'>"
        f"<title>{output.stem}</title><style>{CSS}</style></head>"
        f"<body>{html_body}</body></html>"
    )

    temp_html = output.with_suffix(".tmp.html")
    temp_html.write_text(document, encoding="utf-8")

    subprocess.run(
        [
            find_chromium(),
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            f"--print-to-pdf={output}",
            temp_html.as_uri(),
        ],
        check=True,
        capture_output=True,
    )

    temp_html.unlink()
    print(f"{output} — {output.stat().st_size // 1024} KB")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("uso: build_pdf.py <sorgente.md> <destinazione.pdf>")

    build(Path(sys.argv[1]), Path(sys.argv[2]))
