#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch-rename PDFs by the DOI extracted from inside each PDF.

Tuned for large corpora (thousands of files):
  * multiprocessing (PDFium is not thread-safe, and the GIL blocks threads)
  * fast engine first; slow engines only when a PDF has no text layer at all

Which DOI wins when a page contains several?
  * the article's own DOI is repeated in the running header/footer, so the
    most frequently occurring one wins (a cited paper's DOI appears once)
  * "DOI of original article:" (corrigenda / reprints) is deprioritised
  * a DOI introduced by a doi:/doi.org label beats a bare one
  * otherwise the earliest occurrence wins
Fallbacks: PDF metadata -> legacy Elsevier PII (10.1016/<PII>) -> xlsx index.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pypdfium2 as pdfium
from pypdf import PdfReader

try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    import openpyxl
except Exception:
    openpyxl = None

DOI_RE = re.compile(r"10\.\d{4,9}/[A-Za-z0-9._;()/:\-]+")

# Kovove Materialy prints its DOI with literal spaces: "DOI: 10.4149/km 2019 3 151".
# Handled by a dedicated pattern so the general DOI_RE stays strict.
KOVOVE_RE = re.compile(r"10\.4149/km(?:\s+\d{1,4}){2,3}", re.IGNORECASE)
DOI_FULL_RE = re.compile(r"^10\.\d{4,9}/.+$")
PII_RE = re.compile(r"PII\s*:?\s*(S\d{4}-\d{4}\(\d{2}\)\d{5}-[0-9X])")
ORIGINAL_REF_RE = re.compile(
    r"original\s*article|extended\s+version|correction\s+to|corrections\s*&|"
    r"addenda\s*(?:and|&)\s*errata|erratum|author\s+correction|publisher\s*note|"
    r"retraction|amendment\s+to|preface\s+for", re.IGNORECASE)
LABEL_RE = re.compile(r"doi|dx\.doi\.org")
INVALID_CHARS = set('\\/:*?"<>|')
MARKER = ".created_by_rename_pdfs_by_doi"
PLACEHOLDER_RE = re.compile(r"x0xx00000x|placeholder|xxxxx", re.IGNORECASE)
# Wiley early-view placeholders look like 10.1002/nme.0000 - a real article
# number is never all zeros.
ALL_ZERO_TAIL_RE = re.compile(r"\.0{3,}$")

# Registrants that mint DOIs for data / code repositories, not for the article.
DATA_REPO_PREFIXES = {
    "10.17632",  # Elsevier Data / Mendeley Data
    "10.24433",  # Code Ocean
    "10.5281",   # Zenodo
    "10.6084",   # figshare
    "10.5061",   # Dryad
    "10.7908",   # Materials Data Facility
    "10.15468",  # GBIF
    "10.21227",  # IEEE DataPort
}
# A DOI printed inside the reference list belongs to a cited paper, not to this
# one. The heading must sit on its own line to avoid matching prose.
REFS_HEADING_RE = re.compile(
    r"(?im)^\s*(references|bibliography|literature\s+cited|reference\s+list|"
    r"referencias|refer\u00eancias|r\u00e9f\u00e9rences|\u53c2\u8003\u6587\u732e)\s*[.:]?\s*$")

REPO_CONTEXT_RE = re.compile(
    r"code\s*ocean|capsule|data\s+availability|data\s+repository|repository|"
    r"dataset|data\s*set|supplementary|library\s+link|program\s+files|"
    r"source\s+code|zenodo|figshare|mendeley\s+data", re.IGNORECASE)
TRAILING = set('.,;:)]}>"\'' + "\u2018\u2019\u201c\u201d")

# Zero-width / soft-hyphen code points that publishers leave inside a DOI run.
# They are never part of a real DOI, so dropping them rejoins tokens such as
# "Phys<SOFT HYPHEN>RevB.108.235145".
INVISIBLE_RE = re.compile("[\u00ad\u200b\u200c\u200d\u2060\ufeff\ufffe\uffff]")

# arXiv preprints carry a deterministic DOI: 10.48550/arXiv.<id>
# Chinese journals often typeset spaces *inside* the DOI:
#   "DOI: 10. 7502 / j. issn. 1674-3962. 202112010"
# The trigger requires whitespace inside the registrant ("10 . 7502"), which a
# normal DOI never has, so this cannot disturb ordinary matches.
SPACED_DOI_TRIGGER_RE = re.compile(r"(?i)\bdoi\s*[:：]\s*(10\s*\.\s*\d{4,9}\s*/)")
DOI_TOKEN_RE = re.compile(r"[A-Za-z0-9._;()/:\-]+")
# A "DOI:" label that is NOT a doi.org URL. Reference lists overwhelmingly use
# the URL form, so label-anchored matches are usually the article's own DOI.
DOI_LABEL_RE = re.compile(r"(?i)\bdoi\s*[:：]\s*(?!https?://)(10\.\d{4,9}/[A-Za-z0-9._;()/:\-]+)")

# arXiv ids printed in the text, both modern and pre-2015 archives
ARXIV_TEXT_RE = re.compile(
    r"(?i)arxiv\s*[:：]?\s*(\d{4}\.\d{4,5})v?\d*"
    r"|arxiv\s*[:：]?\s*([a-z\-]{3,12}(?:\.[A-Z]{2})?/\d{7})v?\d*")

ARXIV_RE = re.compile(r"arxiv[_:\- ]?(\d{4}\.\d{4,5})", re.IGNORECASE)
# Pre-2015 identifiers keep their archive name: arXiv:cond-mat/0403421
ARXIV_OLD_RE = re.compile(
    r"arxiv[_:\- ]?([a-z\-]{3,12}(?:\.[A-Z]{2})?)[_:/](\d{7})", re.IGNORECASE)


def arxiv_doi(filename: str):
    m = ARXIV_RE.search(filename)
    if m:
        return "10.48550/arXiv." + m.group(1)
    m = ARXIV_OLD_RE.search(filename)
    if m:
        return f"10.48550/arXiv.{m.group(1).lower()}/{m.group(2)}"
    return None


# Filenames in this corpus often embed the DOI with "/" written as "_",
# e.g. "009081_10.1039_c9tc01416k.pdf". Only trust it when unambiguous.
FILENAME_DOI_RE = re.compile(r"10\.\d{4,9}_[^\s]+")


def filename_doi(name: str):
    """Strict resolver: only an unambiguous single-slash DOI becomes a name."""
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    m = FILENAME_DOI_RE.search(stem)
    if not m:
        return None
    token = clean_doi(m.group(0))
    registrant, _, rest = token.partition("_")
    # more than one "_" means we cannot tell which was the real "/"
    if not rest or "_" in rest:
        return None
    doi = clean_doi(registrant + "/" + rest)
    return doi if is_valid_doi(doi) else None


FILENAME_HINT_RE = re.compile(
    r"(10\.\d{4,9})[_:]((?:[A-Za-z0-9.\-]+[_:]){0,4}[A-Za-z0-9.\-]+)")


def filename_doi_hint(name: str):
    """Loose reader, used only as a tie-break prior, never as a final answer.

    Underscores stand in for slashes, so "10.1088_0256-307x_39_9_097501.pdf"
    may encode 10.1088/0256-307X/39/9/097501. That guess can be wrong, which
    is exactly why it only breaks ties among DOIs actually found in the text.
    """
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    m = FILENAME_HINT_RE.search(stem)
    if not m:
        return None
    doi = m.group(1) + "/" + m.group(2).replace("_", "/").replace(":", "/")
    doi = clean_doi(doi)
    return doi if is_valid_doi(doi) else None


SPRINGER_BOILER_RE = re.compile(r"\(0123456789")
DOUBLED_JOURNAL_RE = re.compile(r"([A-Za-z][A-Za-z0-9]*\.[A-Za-z0-9.\-]{3,})\1$")
# Some filenames repeat only the article id: "sciadv.abf6771abf6771". The unit
# must start with a letter and end with a digit, and the repeat must follow a
# dot, because purely numeric repeats are legitimate:
# 10.1590/s0102-09352012000400040 really does end in "0040" twice.
DOUBLED_ID_RE = re.compile(r"\.([A-Za-z][A-Za-z0-9]{3,}\d)\1$")

TRAILING_WORD_RE = re.compile(r"(\d)([A-Z][A-Za-z]{1,})$")


def clean_doi(raw: str) -> str:
    raw = raw.strip()
    # Springer pastes "(0123456789().,-volV)" right after the DOI
    m = SPRINGER_BOILER_RE.search(raw)
    if m:
        raw = raw[: m.start()]
    while raw and raw[-1] in TRAILING:
        raw = raw[:-1].rstrip()
    reg, sep, sfx = raw.partition("/")
    if sep:
        # "sciadv.adq0083sciadv.adq0083" -> "sciadv.adq0083".
        # Requires a dotted journal unit, so SciELO DOIs such as
        # 10.1590/s0102-09352012000400040 (digit-only repeat) are left alone.
        m2 = DOUBLED_JOURNAL_RE.search(sfx)
        if m2:
            sfx = sfx[: m2.start(1)] + m2.group(1)
        else:
            m3 = DOUBLED_ID_RE.search(sfx)
            if m3:
                sfx = sfx[: m3.start()] + "." + m3.group(1)
        # "…2023.11.010This" -> "…2023.11.010". Only a capitalised word glued
        # to digits is trimmed, so ACS codes like "ja208894m" survive.
        sfx = TRAILING_WORD_RE.sub(r"\1", sfx)
        raw = reg + sep + sfx
        while raw and raw[-1] in TRAILING:
            raw = raw[:-1].rstrip()
    return raw


def is_valid_doi(s: str) -> bool:
    if not s or not DOI_FULL_RE.match(s):
        return False
    if PLACEHOLDER_RE.search(s):
        return False
    reg, _, sfx = s.partition("/")
    if reg.lower() in DATA_REPO_PREFIXES:
        return False
    if len(sfx) < 4:
        return False
    if ALL_ZERO_TAIL_RE.search(sfx):
        return False
    # "10.3390/10.3390/..." means the registrant got glued on twice
    if sfx.startswith("10."):
        return False
    # every real DOI suffix carries at least one digit; "10.1103/Phys" and
    # "10.3390/chemosensors" are line-break truncations
    return any(ch.isdigit() for ch in sfx)


# ---------------------------------------------------------------- candidates

def _add(cands: dict, doi: str, is_orig: bool, has_label: bool, pos: int):
    e = cands.get(doi)
    if e is None:
        cands[doi] = [1, is_orig, has_label, pos]
    else:
        e[0] += 1
        e[1] = e[1] and is_orig
        e[2] = e[2] or has_label


CONTINUATION_RE = re.compile(r"[ \t\r\n]+(\d[A-Za-z0-9._;()/:\-]*)")


def _rejoin(text: str, end: int, partial_raw: str):
    """Rejoin a DOI broken by a line wrap, e.g. "10.1016/j.isci.\n2023.106718".

    Only tried when the match was cut off *before* any digits appeared (the
    suffix carries no digit at all), which is what a mid-token line break looks
    like. A placeholder such as Wiley's early-view "10.1002/nme.0000" does carry
    digits, so it is rejected as-is instead of being "repaired" by gluing the
    next line's page number onto it. Continuations must also start with a digit,
    since the rest of a DOI is numeric whereas the next line usually starts with
    prose or a journal name that must not be glued on.
    """
    best = partial_raw
    for _ in range(2):
        m = CONTINUATION_RE.match(text, end)
        if not m:
            break
        joined = clean_doi(best + m.group(1))
        if not is_valid_doi(joined):
            break
        best, end = joined, m.end()
    joined = clean_doi(best)
    return joined if joined != clean_doi(partial_raw) and is_valid_doi(joined) else None


def _spaced_doi(text: str):
    """Rejoin a DOI that was typeset with spaces inside it.

    Only fires when the registrant itself contains whitespace ("10. 7502"), which
    no normally-typeset DOI has, so ordinary matches are never touched. Tokens are
    then re-joined one at a time and must each be short and DOI-shaped; the walk
    stops at the first long word or non-ASCII (e.g. Chinese) run that follows the
    DOI. Verified on 10.7502/j.issn.1674-3962.202112010 (Materials China).
    """
    m = SPACED_DOI_TRIGGER_RE.search(text)
    if not m or not re.search(r"\s", m.group(1)):
        return None
    window = text[m.start(1): m.start(1) + 200]
    parts = []
    for tok in re.finditer(r"\S+", window):
        t = tok.group(0)
        if len(t) > 14 or not DOI_TOKEN_RE.fullmatch(t.rstrip(".,;:)")):
            break
        parts.append(t)
    if not parts:
        return None
    cand = clean_doi("".join(parts))
    return cand if is_valid_doi(cand) else None


def _harvest(text: str, cands: dict, offset: int):
    text = INVISIBLE_RE.sub("", text)
    sp = _spaced_doi(text)
    if sp:
        _add(cands, sp, False, True, offset)
    for m in list(DOI_RE.finditer(text)) + list(KOVOVE_RE.finditer(text)):
        c = clean_doi(m.group(0))
        cut_before_digits = not any(ch.isdigit() for ch in c.split("/", 1)[1])
        if cut_before_digits:
            j = _rejoin(text, m.end(), m.group(0))
            if j:
                c = j
        if is_valid_doi(c):
            pre = text[max(0, m.start() - 100): m.start()].lower()
            in_refs = bool(REFS_HEADING_RE.search(text[: m.start()]))
            deprio = bool(in_refs or ORIGINAL_REF_RE.search(pre)
                          or REPO_CONTEXT_RE.search(pre))
            _add(cands, c, deprio,
                 bool(LABEL_RE.search(pre)), offset + m.start())


def _norm_doi(d: str) -> str:
    return re.sub(r"[^a-z0-9]", "", d.lower())


def _pick(cands: dict, hint: str | None = None):
    if not cands:
        return None
    hn = _norm_doi(hint) if hint else ""
    best = min(cands.items(),
               key=lambda kv: (-kv[1][0], kv[1][1], not kv[1][2],
                               not (hn and _norm_doi(kv[0]) == hn), kv[1][3]))
    return best[0]


def _scan(gen, max_pages: int, hint: str | None = None):
    """Consume page texts; return (doi, saw_text, pii, strong).

    Stops early once one DOI is clearly the running-header DOI (2+ pages).
    `hint` is a DOI recovered from the filename, used only as a weak prior to
    break ties between text candidates. It never becomes the answer on its own:
    in this corpus several filenames carried a DOI that did not match the file
    contents at all (an IAEA report, a vendor datasheet, an ORNL handbook).
    """
    cands: dict = {}
    offset = 0
    saw_text = False
    pii = None
    for i, text in enumerate(gen):
        if i >= max_pages:
            break
        if text and text.strip():
            saw_text = True
            _harvest(text, cands, offset)
            offset += len(text) + 1
            if pii is None:
                pm = PII_RE.search(text)
                if pm:
                    pii = "10.1016/" + pm.group(1)
        strong = [d for d, e in cands.items() if e[0] >= 2]
        if len(strong) == 1:
            return strong[0], True, pii, True
    # Every candidate is deprioritised => they all belong to cited papers,
    # corrigendum notices or data repositories. Emitting one of them would name
    # this file after a different publication, so report "no DOI" instead.
    if cands and all(e[1] for e in cands.values()):
        return None, saw_text, pii, False
    return _pick(cands, hint), saw_text, pii, False


# ------------------------------------------------------------ text engines

def _texts_pium(path: Path, max_pages: int):
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception:
        return
    try:
        for i in range(min(len(pdf), max_pages)):
            tp = None
            try:
                tp = pdf[i].get_textpage()
                yield tp.get_text_range() or ""
            except Exception:
                yield ""
            finally:
                if tp is not None:
                    try:
                        tp.close()
                    except Exception:
                        pass
    finally:
        pdf.close()


def _texts_plumber(path: Path, max_pages: int):
    if pdfplumber is None:
        return
    try:
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:max_pages]:
                try:
                    yield page.extract_text() or ""
                except Exception:
                    yield ""
    except Exception:
        return


def _texts_pypdf(path: Path, max_pages: int):
    try:
        r = PdfReader(str(path))
    except Exception:
        return
    for page in r.pages[:max_pages]:
        try:
            yield page.extract_text() or ""
        except Exception:
            yield ""


def _metadata_doi(path: Path):
    try:
        meta = PdfReader(str(path)).metadata
    except Exception:
        return None
    if not meta:
        return None
    try:
        vals = list(meta.values())
    except Exception:
        return None
    for v in vals:
        if isinstance(v, str):
            m = DOI_RE.search(v)
            if m:
                c = clean_doi(m.group(0))
                if is_valid_doi(c):
                    return c
    return None


def _label_doi_whole_doc(path: Path):
    """Last-resort scan of EVERY page for a 'DOI:'-labelled DOI.

    Accepted only when exactly one distinct DOI carries that label anywhere in
    the document. If several appear (a reference list that uses the label form)
    the result is ambiguous and we refuse rather than guess. Measured on a
    277-file corpus: 66 files yield a unique value, 17 are ambiguous (up to 110
    distinct DOIs) and are correctly refused.
    """
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception:
        return None
    found = set()
    try:
        for i in range(len(pdf)):
            tp = None
            try:
                tp = pdf[i].get_textpage()
                t = tp.get_text_range() or ""
            except Exception:
                continue
            finally:
                if tp is not None:
                    try:
                        tp.close()
                    except Exception:
                        pass
            for mm in DOI_LABEL_RE.finditer(INVISIBLE_RE.sub("", t)):
                d = clean_doi(mm.group(1))
                if is_valid_doi(d):
                    found.add(d)
    finally:
        pdf.close()
    return found.pop() if len(found) == 1 else None


def _arxiv_doi_from_text(path: Path, max_pages: int):
    """arXiv preprints print their identifier but often no DOI."""
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception:
        return None
    try:
        for i in range(min(len(pdf), max_pages)):
            tp = None
            try:
                tp = pdf[i].get_textpage()
                t = tp.get_text_range() or ""
            except Exception:
                continue
            finally:
                if tp is not None:
                    try:
                        tp.close()
                    except Exception:
                        pass
            for mm in ARXIV_TEXT_RE.finditer(t):
                ident = mm.group(1) or mm.group(2)
                if ident:
                    return "10.48550/arXiv." + ident
    finally:
        pdf.close()
    return None


def _pii_doi(path: Path, max_pages: int):
    try:
        r = PdfReader(str(path))
        meta = r.metadata
        if meta:
            for v in list(meta.values()):
                if isinstance(v, str):
                    m = PII_RE.search(v)
                    if m:
                        return "10.1016/" + m.group(1)
        for page in r.pages[:max_pages]:
            try:
                txt = page.extract_text() or ""
            except Exception:
                continue
            m = PII_RE.search(txt)
            if m:
                return "10.1016/" + m.group(1)
    except Exception:
        return None
    return None


def extract_doi(path: Path, max_pages: int, deep: bool = False,
                overrides: dict | None = None):
    """Return (doi, method, error)."""
    if overrides and path.name in overrides:
        return overrides[path.name], "override", None
    hint = filename_doi_hint(path.name)
    try:
        doi, saw_text, pii, _ = _scan(_texts_pium(path, max_pages),
                                      max_pages, hint)
        if doi:
            return doi, "pdf", None
        if deep or not saw_text:
            for gen in (_texts_plumber, _texts_pypdf):
                d2, _, p2, _ = _scan(gen(path, max_pages), max_pages, hint)
                if d2:
                    return d2, "pdf-deep", None
                if p2 and not pii:
                    pii = p2
        d = _metadata_doi(path)
        if d:
            return d, "metadata", None
        if pii:
            return pii, "pii", None
        d = _pii_doi(path, max_pages)
        if d:
            return d, "pii", None
        d = _arxiv_doi_from_text(path, max_pages)
        if d:
            return d, "arxiv-text", None
        d = arxiv_doi(path.name)
        if d:
            return d, "arxiv", None
        d = _label_doi_whole_doc(path)
        if d:
            return d, "pdf-label-full", None
        d = filename_doi(path.name)
        if d:
            return d, "filename", None
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"
    return None, None, None


# ------------------------------------------------------------------ worker

def _job(args):
    path_str, max_pages, deep = args
    p = Path(path_str)
    try:
        doi, method, err = extract_doi(p, max_pages, deep)
    except Exception as e:
        doi, method, err = None, None, f"{type(e).__name__}: {e}"
    return path_str, doi, method, err


# ------------------------------------------------------------------- index

def load_index(xlsx: Path, name_col: int, doi_col: int, header_row: int) -> dict:
    if openpyxl is None:
        print("openpyxl not available; ignoring --index", file=sys.stderr)
        return {}
    try:
        wb = openpyxl.load_workbook(str(xlsx), read_only=True, data_only=True)
    except Exception as e:
        print(f"cannot read index {xlsx}: {e}", file=sys.stderr)
        return {}
    out: dict = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows(min_row=header_row, values_only=True):
            if not row:
                continue
            try:
                name, doi = row[name_col], row[doi_col]
            except IndexError:
                continue
            if not name or not doi:
                continue
            name = str(name).strip()
            if not name.lower().endswith(".pdf"):
                continue
            doi = clean_doi(str(doi).strip())
            if is_valid_doi(doi):
                out.setdefault(name, doi)
    wb.close()
    return out


# -------------------------------------------------------------------- misc

def sanitize_stem(s: str, maxlen: int = 180) -> str:
    s = "".join("_" if (ch in INVALID_CHARS or ord(ch) < 32) else ch
                for ch in s.strip())
    s = s.rstrip(". ")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip(". ")
    return s or "unnamed"


def file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unique_name(base: str, used: dict) -> str:
    n = used.get(base, 0) + 1
    used[base] = n
    return base + ".pdf" if n == 1 else f"{base} ({n}).pdf"


def _place(src_file: Path, dest: Path, mode: str, dry_run: bool) -> bool:
    if dry_run:
        return True
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if mode == "rename":
            if dest.exists():
                print(f"    target exists, skipped: {dest.name}")
                return False
            os.replace(src_file, dest)
        else:
            shutil.copy2(src_file, dest)
        return True
    except Exception as e:
        print(f"    WRITE ERROR {src_file.name}: {type(e).__name__}: {e}")
        return False


# -------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Copy/rename PDFs using the DOI found inside each file.")
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["copy", "rename"], default="copy",
                    help="copy = source untouched (default); rename = in place")
    ap.add_argument("--index", default="",
                    help="optional .xlsx filename->DOI map, last resort only")
    ap.add_argument("--index-name-col", type=int, default=4)
    ap.add_argument("--index-doi-col", type=int, default=2)
    ap.add_argument("--index-header-row", type=int, default=6)
    ap.add_argument("--index-as-source", action="store_true",
                    help="allow the spreadsheet DOI to be used when the PDF yields "
                         "none (default: spreadsheet is cross-check only, because it "
                         "contains truncated DOIs)")
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--workers", type=int, default=1,
                    help="keep 1: PDFium is not thread-safe, >1 corrupts extraction")
    ap.add_argument("--executor", choices=["thread", "process"], default="thread",
                    help="thread is safest; process may be blocked in sandboxes")
    ap.add_argument("--deep", action="store_true",
                    help="also try pdfplumber/pypdf even when a text layer exists")
    ap.add_argument("--only-list", default="",
                    help="text file of filenames (one per line) to process")
    ap.add_argument("--recursive", action="store_true",
                    help="also process PDFs in subfolders of --source")
    ap.add_argument("--keep-tree", action="store_true",
                    help="mirror the source subfolder structure in --out and "
                         "de-duplicate per folder instead of globally "
                         "(implies --recursive)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--clean", action="store_true",
                    help="delete an output folder previously made by this script")
    ap.add_argument("--keep-dups", action="store_true",
                    help="also save duplicate-DOI files into <out>\\_重复文件")
    ap.add_argument("--quiet", action="store_true", help="only print summary")
    args = ap.parse_args()

    if args.keep_tree:
        args.recursive = True
    src = Path(args.source).expanduser()
    out = Path(args.out).expanduser()
    if not src.is_dir():
        print(f"source folder not found: {src}")
        return 1
    if args.mode == "copy" and out.resolve() == src.resolve():
        print("--out must differ from --source in copy mode")
        return 1

    if args.clean and out.exists():
        if not (out / MARKER).exists():
            print(f"Refusing --clean: {out} was not created by this script.")
            return 1
        print(f"cleaning {out}")
        if not args.dry_run:
            shutil.rmtree(out)

    dup_dir = out / "_重复文件"
    no_doi_dir = out / "未识别DOI"
    report_path = out / "rename_report.csv"

    if args.only_list:
        names = [ln.strip() for ln in
                 Path(args.only_list).read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        files = [src / n for n in names if (src / n).exists()]
        missing = len(names) - len(files)
        if missing:
            print(f"warning: {missing} listed file(s) not found")
    else:
        globber = src.rglob if args.recursive else src.glob
        files = sorted(globber("*.pdf"),
                       key=lambda p: p.relative_to(src).as_posix().lower())
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"No PDF files to process in {src}")
        return 1

    index = {}
    if args.index:
        ix = Path(args.index).expanduser()
        if ix.is_file():
            index = load_index(ix, args.index_name_col, args.index_doi_col,
                               args.index_header_row)
            print(f"index entries loaded: {len(index)}")
        else:
            print(f"index file not found: {ix}")

    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)
        (out / MARKER).write_text("ok\n", encoding="utf-8")
        if not args.keep_tree:
            no_doi_dir.mkdir(parents=True, exist_ok=True)
        if args.keep_dups:
            dup_dir.mkdir(parents=True, exist_ok=True)

    total = len(files)
    print(f"extracting DOIs from {total} PDFs "
          f"({args.executor}s={args.workers}, max_pages={args.max_pages}, deep={args.deep}) ...",
          flush=True)
    t0 = time.time()
    extracted: dict = {}
    done = 0
    jobs = [(str(f), args.max_pages, args.deep) for f in files]
    pool_cls = ProcessPoolExecutor if args.executor == "process" else ThreadPoolExecutor
    with pool_cls(max_workers=max(1, args.workers)) as ex:
        futs = [ex.submit(_job, j) for j in jobs]
        for fut in as_completed(futs):
            try:
                path_str, doi, method, err = fut.result()
            except Exception as e:
                print(f"worker crashed: {type(e).__name__}: {e}")
                continue
            extracted[Path(path_str).relative_to(src).as_posix()] = (doi, method, err)
            done += 1
            if done % 200 == 0 or done == total:
                el = time.time() - t0
                rate = done / el if el else 0
                eta = (total - done) / rate if rate else 0
                print(f"  {done}/{total}  {el:.0f}s  ETA {eta:.0f}s", flush=True)

    use_index_as_source = args.index_as_source
    seen_doi: dict = {}
    seen_hash: set = set()
    used_nodoi: dict = {}
    used_dup: dict = {}
    rows = []
    counts = {"kept": 0, "skipped": 0, "no_doi": 0, "error": 0, "mismatch": 0}

    for idx, f in enumerate(files, 1):
        rel = f.relative_to(src).as_posix()
        doi, method, err = extracted.get(rel, (None, None, "not extracted"))
        idx_doi = index.get(f.name, "")
        agree = ""

        if err:
            counts["error"] += 1
            rows.append({"source": rel, "doi": "", "method": "", "new_name": "",
                         "status": f"error: {err}", "index_doi": idx_doi,
                         "agree": "", "duplicate_of": ""})
            if not args.quiet:
                print(f"[{idx}/{total}] ERROR {f.name}: {err}")
            continue

        if not doi and idx_doi and use_index_as_source:
            doi, method = idx_doi, "index"
        if doi and idx_doi:
            agree = "yes" if doi.lower() == idx_doi.lower() else "no"
            if agree == "no":
                counts["mismatch"] += 1

        # In keep-tree mode the source folders are a classification scheme, so
        # the same paper filed under two categories is intentional: de-duplicate
        # within one folder only, and note cross-folder repeats in the report.
        sub = f.parent.relative_to(src)
        dup_scope = (sub.as_posix(), doi) if args.keep_tree else doi
        base_dir = (out / sub) if args.keep_tree else out

        if doi:
            stem = sanitize_stem(doi.replace("/", "_"))
            if dup_scope in seen_doi:
                counts["skipped"] += 1
                first = seen_doi[dup_scope]
                new_name = unique_name(stem, used_dup) if args.keep_dups else ""
                rows.append({"source": rel, "doi": doi, "method": method or "",
                             "new_name": new_name, "status": "skipped_duplicate",
                             "index_doi": idx_doi, "agree": agree,
                             "duplicate_of": first})
                if not args.quiet:
                    print(f"[{idx}/{total}] SKIP {f.name} (dup of {first})")
                if args.keep_dups:
                    _place(f, dup_dir / new_name, args.mode, args.dry_run)
                continue
            seen_doi[dup_scope] = rel
            new_name = stem + ".pdf"
            dest = base_dir / new_name
            status = "renamed"
            counts["kept"] += 1
        else:
            try:
                h = file_sha1(f)
            except Exception:
                h = f.name
            hash_scope = (sub.as_posix(), h) if args.keep_tree else h
            if hash_scope in seen_hash:
                counts["skipped"] += 1
                rows.append({"source": rel, "doi": "", "method": "",
                             "new_name": "", "status": "skipped_duplicate",
                             "index_doi": idx_doi, "agree": agree,
                             "duplicate_of": ""})
                if not args.quiet:
                    print(f"[{idx}/{total}] SKIP {f.name} (duplicate content)")
                continue
            seen_hash.add(hash_scope)
            # Keep an unidentifiable PDF in its own category folder under its
            # original name, so the classification survives.
            new_name = unique_name(sanitize_stem(f.stem), used_nodoi)
            dest = (base_dir / new_name) if args.keep_tree else (no_doi_dir / new_name)
            status = "no_doi"
            counts["no_doi"] += 1

        if not _place(f, dest, args.mode, args.dry_run):
            status = "error"
            counts["error"] += 1
        rows.append({"source": rel, "doi": doi or "", "method": method or "",
                     "new_name": new_name, "status": status, "index_doi": idx_doi,
                     "agree": agree, "duplicate_of": ""})
        if not args.quiet:
            print(f"[{idx}/{total}] {f.name} -> {new_name}  (doi={doi})")

    if not args.dry_run:
        fields = ["source", "doi", "method", "new_name", "status",
                  "index_doi", "agree", "duplicate_of"]
        with open(report_path, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    print("\n===== Summary =====")
    print(f"total={total} kept={counts['kept']} skipped_dup={counts['skipped']} "
          f"no_doi={counts['no_doi']} error={counts['error']} "
          f"index_mismatch={counts['mismatch']}")
    methods = {}
    for r in rows:
        if r["method"]:
            methods[r["method"]] = methods.get(r["method"], 0) + 1
    print("methods:", dict(sorted(methods.items(), key=lambda kv: -kv[1])))
    print(f"elapsed={time.time() - t0:.0f}s")
    print(f"output={out}")
    if not args.dry_run:
        print(f"report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
