# pdf-doi-rename

A [Codex](https://codex.com) skill that batch-renames academic PDF files using the
DOI printed inside each document.

Renaming is by **DOI itself**, not by author/title metadata. Windows filenames
cannot contain `/`, so the DOI separator becomes `_`:

```
10.1016/j.corsci.2010.06.019  ->  10.1016_j.corsci.2010.06.019.pdf
```

## What it does

- Extracts the DOI from the first pages of each PDF (pypdfium2, with pdfplumber
  and pypdf as fallbacks)
- Falls back to PDF metadata, legacy Elsevier PII numbers, and arXiv identifiers
- Handles DOIs broken across lines, glued to surrounding text, zero-width
  characters, corrigenda/reprints that print two DOIs, and publisher placeholders
- Excludes data/repository DOIs (Zenodo, figshare, Code Ocean, etc.) that are
  not the article's own
- Never trusts a filename's DOI as the answer; the file content wins
- Copies renamed files to an output folder (source files are never modified by
  default)
- De-duplicates by DOI (or per-folder when `--keep-tree` is used)
- Writes a `rename_report.csv` with one row per source file: DOI, new name,
  extraction method, and why anything was skipped

## Install

### From GitHub

```bash
cd ~/.codex/skills
git clone https://github.com/YOUR_USERNAME/pdf-doi-rename.git
```

### Using the Codex skill-installer

Ask Codex to install it from your repo, or use the bundled installer:

```bash
python install-skill-from-github.py --repo YOUR_USERNAME/pdf-doi-rename --path .
```

### Manual

Copy this repository into `~/.codex/skills/pdf-doi-rename/`.

## Dependencies

```bash
python -m pip install pypdfium2 pypdf pdfplumber openpyxl
```

`pypdfium2` is required; the others are fallbacks / optional features.

## Usage

Ask Codex: *"Rename the PDFs in this folder by their DOI"* and point it at the
folder. The skill's instructions will guide the agent through a dry-run, the
real run, and verification.

Direct script usage:

```bash
# Preview (no files written)
python scripts/rename_pdfs_by_doi.py \
  --source "path/to/pdfs" \
  --out "path/to/output" \
  --limit 30 --dry-run

# Real run
python scripts/rename_pdfs_by_doi.py \
  --source "path/to/pdfs" \
  --out "path/to/output" \
  --quiet
```

### Options

| Flag | Meaning |
|---|---|
| `--recursive` | Process PDFs in subfolders |
| `--keep-tree` | Mirror subfolder structure; de-duplicate per folder |
| `--max-pages N` | Pages to scan per PDF (default 3; use 6 for later DOIs) |
| `--mode rename` | Rename in place (default is `copy`) |
| `--index FILE.xlsx` | Optional filename→DOI map as last-resort source |
| `--index-as-source` | Allow the spreadsheet to supply a missing DOI |
| `--keep-dups` | Save duplicate-DOI files into `_重复文件/` |
| `--clean` | Delete an output folder previously made by this script |
| `--only-list FILE` | Process only the files listed (one per line) |
| `--limit N` | Only process the first N files |
| `--quiet` | Only print the summary |

Keep `--workers 1`: PDFium is not thread-safe, and multi-threaded extraction
silently degrades.

## Files that end up as "no DOI"

Books, handbooks, theses, IAEA/ORNL/vendor reports, scanned items without a
text layer, and unpublished preprints legitimately have no DOI. The skill keeps
them under their original names and never invents a DOI.

## Limitations

- Password-protected PDFs cannot be opened (reported as
  `error: FileNotDecryptedError`; the file is left untouched)
- Reference-list DOIs are correctly deprioritised, so very long documents whose
  only DOIs are citations will report "no DOI"
- The script processes only `*.pdf` files

## License

MIT — see [LICENSE](LICENSE).
