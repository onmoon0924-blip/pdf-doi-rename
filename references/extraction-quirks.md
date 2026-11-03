# Extraction quirks observed in real corpora

Each entry below caused a wrong filename in a production run over 2,951 PDFs.
All are handled by `scripts/rename_pdfs_by_doi.py`; read this before changing
the extraction logic or debugging a suspicious name.

## 1. Line breaks inside a DOI

PDF text layers often split a DOI across lines, so `\S+` stops early.

| Raw text | Wrong result | Correct |
|---|---|---|
| `10.1016/j.ijimpeng.201 1.02.009` | `10.1016/j.ijimpeng.201` | `10.1016/j.ijimpeng.2011.02.009` |
| `https://doi.org/10.1016/j.isci.\n2023.106718` | `10.1016/j.isci` | `10.1016/j.isci.2023.106718` |
| `10.1016/j.actamat.2017.1 1.021` | `10.1016/j.actamat.2017.1` | `10.1016/j.actamat.2017.11.021` |

Fix: pdfplumber and pypdfium2 usually keep the token intact; when the match has
no digits in its suffix, rejoin across one gap, accepting only continuations that
start with a digit. A digit-leading continuation is what distinguishes a wrapped
DOI from the next line of prose.

**Do not rejoin when the suffix already has digits.** Wiley early-view placeholders
like `10.1002/nme.0000` would otherwise swallow the next line's page number and
become `10.1002/nme.00001`, which looks valid.

## 2. Zero-width and soft-hyphen code points

Some PDFs embed `U+00AD`, `U+200B`-`U+200D`, `U+2060`, or `U+FEFF` inside the DOI
run, splitting it mid-token: `10.1103/Phys<SOFT HYPHEN>RevB.108.235145` extracted
as `10.1103/Phys`. These code points are never part of a real DOI, so they are
stripped before matching.

## 3. Text glued to the DOI (no space in the text layer)

pdfplumber can remove every space, so the DOI absorbs what follows:

| Glued text | Wrong result | Correct |
|---|---|---|
| `dx.doi.org/10.1021/ja208894m\|J.Am.Chem.Soc.2012,134,3042-3053` | whole string | `10.1021/ja208894m` |
| `.../10.1016/j.mattod.2023.11.010This is an open access...` | `...010This` | `10.1016/j.mattod.2023.11.010` |
| `.../10.1007/s10973-019-08107-w(0123456789().,-volV)` | boilerplate included | `10.1007/s10973-019-08107-w` |

Fix: match DOI characters only (`A-Za-z0-9._;()/:\-`) rather than `\S+`, so `|`,
`,` and spaces terminate the match. Then strip a capitalised word glued directly
to digits, and cut Springer's `(0123456789().,-volV)` boilerplate.

The trailing-word rule must require a capitalised word after digits. ACS codes
like `10.1021/ja208894m` end in a lowercase letter and must survive.

## 4. Two DOIs on one page

Corrigenda, author corrections, addenda, and reprints cite the original article:

```
addenda and errata   Acta Cryst. (2025). B81   https://doi.org/10.1107/S2052520625003361
research papers      Acta Cryst. (2025). B81, 37-46   https://doi.org/10.1107/S2052520624011053
```

Page 1 holds the erratum's DOI; the article's own DOI appears from page 2 onward.
Also seen: `Correction to: Scientific Reports https://doi.org/10.1038/s41598-024-77786-9`
followed by the correction's own `10.1038/s41598-024-79831-z`, and
`Preface for this article: DOI http://dx.doi.org/10.2109/jcersj2.124.P4-1` above the
real `10.2109/jcersj2.15263`.

Text order is unreliable: pypdf emits the article's own DOI first, pdfplumber
emits the cited one first. Rank candidates instead:

1. not preceded by `original article` / `correction to` / `addenda and errata` /
   `erratum` / `author correction` / `retraction` / `preface for` / `extended version`
2. not inside a References section (heading matched on its own line, so prose
   mentioning "references" does not trigger it)
3. not in a data/code repository context (`Code Ocean capsule`, `Data availability`)
4. most frequent across pages (the running header repeats the article's own DOI)
5. preceded by a `doi` / `doi.org` label
6. earliest position

If every candidate is deprioritised, return no DOI. Naming a file after a cited
paper is worse than leaving it unidentified.

## 5. Reference-list DOIs and supplementary material

A supplementary-information PDF (`003787_10.1007_s12666-023-03195-7.pdf`) has no
DOI of its own; its text yielded three cited DOIs and the first was chosen,
producing `10.1016/j.jallcom.2019.06.387`. Handle by the References deprioritiser
above plus the all-deprioritised refusal.

## 6. DOIs with literal spaces

Kovove Materialy prints `DOI: 10.4149/km 2019 3 151`. A dedicated pattern accepts
`10.4149/km` plus two or three space-separated numeric groups. Keep this narrow:
allowing spaces in the general pattern would glue a following year onto ordinary
DOIs (`...2019.01.001 2019 Elsevier`).

## 7. Legacy Elsevier PII instead of a DOI

Pre-DOI Elsevier PDFs print `PII: S0921-5093(99)00581-X` and no `10.xxxx` string.
Elsevier's registered DOI is exactly `10.1016/<PII>`. The PII often appears only in
PDF metadata (`/Title`), so check metadata as well as page text.

## 8. arXiv identifiers

Preprints have a deterministic DOI `10.48550/arXiv.<id>`. Both forms occur:
modern `arXiv:2406.15022` and pre-2015 `arXiv:cond-mat/0403421`, which keeps its
archive name and yields `10.48550/arXiv.cond-mat/0403421`.

Note that a preprint's own arXiv DOI is correct even when the PDF is the published
version, as long as the published DOI is actually printed in the text - prefer the
printed one.

## 9. Doubled article IDs in filenames

Filenames such as `003639_10.1126_sciadv.adq0083sciadv.adq0083.pdf` repeat the
article ID. Collapse `sciadv.adq0083sciadv.adq0083` -> `sciadv.adq0083` only when
the unit starts with a letter and ends with a digit, following a dot. Purely
numeric repeats are legitimate: `10.1590/s0102-09352012000400040` (SciELO) really
does end in `0040` twice, and `10.2320/jinstmet1952.60.5_463` must be left alone.

## 10. DOI only in metadata

Some PDFs have no DOI in the page text at all; it lives in `/DOI` or `/Subject`.
Materials Today: Proceedings files needed this (`/doi = 10.1016/j.matpr.2018.10.001`).

## 11. Filenames that lie

Never derive a final name from the filename alone. Observed cases: an IAEA report
named with a Hindawi DOI, a Special Metals datasheet named with a Hindawi DOI, an
ORNL handbook named with an npj Materials Degradation DOI. A filename DOI may only
break ties among candidates found in the text.

## 12. Genuinely DOI-less documents

Leave these in `未识别DOI/` with their original names. Real examples: IAEA-TECDOC
reports, INCONEL vendor datasheets, INL and ORNL technical reports, and scanned
items without a text layer. Reporting them as unidentified is correct; guessing a
DOI is not.
