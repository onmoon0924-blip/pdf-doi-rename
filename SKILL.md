---
name: pdf-doi-rename
description: Batch-rename PDF files using the DOI extracted from inside each file. Use when the user wants to rename or organize a folder of academic papers, journal articles, or PDFs by DOI (including Chinese requests like "按DOI重命名文献"). Renaming is by DOI itself, not by author/title metadata. Not for extracting text, converting formats, or renaming by title.
metadata:
  short-description: Rename PDFs by the DOI found inside them
---

# PDF DOI Rename

Rename every PDF in a folder to the DOI printed inside it. Windows filenames
cannot contain `/`, so the DOI separator becomes `_`:
`10.1016/j.corsci.2010.06.019` -> `10.1016_j.corsci.2010.06.019.pdf`.

The script never modifies source files by default: it copies renamed files into
a separate output folder. Source files are only touched with `--mode rename`,
which must be requested explicitly.

## Workflow

1. Count the PDFs and confirm the output location with the user.
   For more than a few hundred files, warn about run time (~10 s per 100 files)
   and disk space needed for the copies.
2. Dry-run first, always:

   ```bash
   python scripts/rename_pdfs_by_doi.py --source "<SRC>" --out "<OUT>" --limit 30 --dry-run
   ```

   Check the printed mappings before writing anything.
3. Real run:

   ```bash
   python scripts/rename_pdfs_by_doi.py --source "<SRC>" --out "<OUT>" --quiet
   ```

   Add `--recursive` when the folder has subfolders. Keep `--workers 1`.
   When the subfolders are a real classification scheme (papers sorted by topic,
   material system, application, etc.), use `--keep-tree` instead of `--recursive`:
   it mirrors the folder tree into `--out` and de-duplicates only within each
   folder, so a paper intentionally filed under two categories keeps both copies.
   Bump `--max-pages` to 6 when DOIs sit on later pages (e.g. Science puts its DOI
   in the supplementary-materials block near the end).
4. Verify: file counts must reconcile, and every kept DOI must be unique.
   `rename_report.csv` in the output folder has one row per source file with the
   DOI, the new name, the extraction method, and why a file was skipped.
5. Report the leftovers honestly. Files with no DOI are books/handbooks,
   theses, vendor datasheets, scanned items without a text layer, or reports
   (IAEA/ORNL). In flat mode they land in `未识别DOI/`; in `--keep-tree` mode they
   stay in their own category folder under the original name. Never invent one.

   An `error: FileNotDecryptedError` row means the PDF is password-protected and
   could not be opened (pikepdf with an empty password also fails when a real
   open-password is set). Such a file is left untouched in the source; report it
   to the user rather than trying to defeat the password.

## Constraints that matter

**Keep `--workers 1`.** PDFium is not thread-safe. Threads do not crash visibly,
they silently fail: on a 197-file sample, only 6 files resolved via the fast path
instead of ~160, and the run took 313 s instead of 36 s. Multiprocessing is
blocked inside the Codex sandbox. Single-process pypdfium2 costs ~65 ms/file.

**Never trust a filename's DOI as the answer.** This corpus contained files whose
names carried a DOI belonging to a different document: a 388-page IAEA report
named `003875_10.1155_2024_8811265.pdf`, an INCONEL vendor datasheet named
`004405_10.1155_er_8410668.pdf`, a 52-page ORNL handbook named
`direct_10_1038_s41529_024_00499_x_...`. A filename DOI is used only to break
ties between DOIs actually found in the text, never to name a file on its own.

**Corrigenda, corrections, and reprints carry two DOIs.** They print their own
plus the original article's (`DOI of original article:`, `Correction to:`,
`addenda and errata`, `Preface for this article:`). The script deprioritises the
latter and prefers the DOI that repeats across pages (the running header). If
every candidate is deprioritised, the script reports no DOI rather than guessing.

**Publisher placeholder DOIs are rejected**: Wiley early-view `10.1002/nme.0000`,
RSC `10.1039/x0xx00000x`, IOP `XXXX/XXXX`.

**Data/code repository DOIs are excluded**, not the article's DOI: Elsevier Data
(10.17632), Code Ocean (10.24433), Zenodo (10.5281), figshare (10.6084), Dryad
(10.5006 when contextualised as a repository), IEEE DataPort (10.21227).

**Reference-list DOIs are not the article's DOI.** A deep scan will find many
`10.xxxx/...` strings inside a bibliography; those belong to cited papers. The
ranking above plus the "all candidates deprioritised => report none" rule keeps
them out. This is why long handbooks and theses often end up as no-DOI: their only
DOIs are citations. Do not widen the scan to "fix" this.

Extraction quirks found in real corpora are documented in
`references/extraction-quirks.md`. Read it before debugging a wrong DOI.

## Dependencies

`pypdfium2` (required), `pypdf`, `pdfplumber`, and `openpyxl` (only for
`--index`). Check before running:

```bash
python -c "import pypdfium2, pypdf, pdfplumber; print('ok')"
```

If missing, install with pip (needs network approval in the sandbox):
`python -m pip install pypdfium2 pypdf pdfplumber`.
