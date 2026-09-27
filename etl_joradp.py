#!/usr/bin/env python3
"""ETL des Journaux Officiels JORADP (PDF scannés) -> CSV / JSON / SQL MySQL.

Pipeline :
  1. Rendre chaque page en image (PyMuPDF, qui décode le JBIG2 des scans).
  2. OCR avec Tesseract (fra pour pdfs/fr, ara pour pdfs/ar).
  3. Extraire par regex : type + numéro, date de publication, ministère, contenu.
  4. Nettoyer (UTF-8, suppression abonnements/publicité/sommaire).
  5. Écrire CSV, JSON et un fichier SQL d'INSERT INTO (échappé MySQL).

Schéma SQL cible :
  CREATE TABLE jo_textes (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    numero VARCHAR(64) NULL,
    date_publi DATE NULL,
    ministere VARCHAR(255) NULL,
    contenu TEXT NULL,
    fichier VARCHAR(255) NULL,
    page INT NULL
  ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import fitz  # PyMuPDF
import pytesseract
from PIL import Image

# ---------------------------------------------------------------------------
# Configuration (surchargée par les options CLI)
# ---------------------------------------------------------------------------
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSDATA_DIR = r"C:\Users\DELL\AppData\Local\joradp\tessdata"
DEFAULT_DPI = 300
DEFAULT_TABLE = "jo_textes"

# Chiffres arabes/indiens -> chiffres latins (pour les numéros et dates arabes).
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize_digits(text: str) -> str:
    return text.translate(_AR_DIGITS)


# Suppression des accents français pour la détection par mots-clés.
_UNACCENT = {
    "é": "e", "è": "e", "ê": "e", "ë": "e",
    "à": "a", "â": "a", "ä": "a",
    "î": "i", "ï": "i",
    "ô": "o", "ö": "o", "œ": "oe",
    "û": "u", "ù": "u", "ü": "u",
    "ç": "c",
    "É": "E", "È": "E", "Ê": "E", "Ë": "E",
    "À": "A", "Â": "A", "Ä": "A",
    "Î": "I", "Ï": "I",
    "Ô": "O", "Ö": "O",
    "Û": "U", "Ù": "U", "Ü": "U",
    "Ç": "C",
    "’": "'", "‘": "'", "`": "'", "´": "'",
    "«": '"', "»": '"',
}


def unaccent(text: str) -> str:
    return "".join(_UNACCENT.get(ch, ch) for ch in text)


# ---------------------------------------------------------------------------
# Expressions rationnelles de détection
# ---------------------------------------------------------------------------
# En-tête de texte réglementaire français : « Ordonnance n° 62-1 du … ».
# Le « (?!…) » écarte les intitulés de rubriques (« AVIS ET COMMUNICATIONS »)
# et la formule opérative « Arrête : ».
FR_TYPE_NUM_RE = re.compile(
    r"^\s*(d[eé]cret\s+(?:ex[eé]cutif|pr[eé]sidentiel)|"
    r"ordonnance|"
    r"arr[eê]t[eé](?:\s+interminist[eé]riel)?|"
    r"d[eé]cision|circulaire|loi|avis)"
    r"\b(?!\s*(?:et\b|:|&))",
    re.IGNORECASE,
)

FR_NUM_RE = re.compile(
    r"\bn\s*[o°]?\s*([0-9]{1,4}(?:\s*[-–—/]\s*[0-9]{1,4})?)",
    re.IGNORECASE,
)

FR_MONTHS = (
    "janvier|f[eé]vrier|mars|avril|mai|juin|juillet|"
    "ao[uû]t|septembre|octobre|novembre|d[eé]cembre"
)
FR_DATE_RE = re.compile(
    r"\b([0-9]{1,2})(?:er)?\s+(" + FR_MONTHS + r")\s+([0-9]{4})\b",
    re.IGNORECASE,
)

FR_MINISTRY_RE = re.compile(
    r"^\s*(MINIST[EÈ]RE|SECR[EÈ]TARIAT|D[EÈ]L[EÈ]GATION|"
    r"PR[EÈ]SIDENCE|GOUVERNEMENT|PRIMATURE|COMMISSARIAT|"
    r"COUR|CONSEIL|DIRECTION\s+G[EÈ]N[EÈ]RALE)\b",
)

# En-tête de texte réglementaire arabe : « مرسوم تنفيذي رقم 96-... ».
AR_TYPE_NUM_RE = re.compile(
    r"(مرسوم(?:\s+(?:تنفيذي|رئاسي|تشريعي))?|"
    r"أمر|قرار|مقرر|قانون|منشور)\s+"
    r"(?:رقم\s+)?([0-9٠-٩]{1,4}(?:\s*[-–—/]\s*[0-9٠-٩]{1,4})?)"
)

AR_MINISTRY_RE = re.compile(r"^\s*(وزارة|وزير)")

AR_MONTHS = (
    "جانفي|جانفى|يناير|فيفري|فبراير|مارس|أفريل|افريل|أبريل|"
    "ماي|مايو|جوان|يونيو|جويلية|جولية|يوليو|أوت|اوت|أغسطس|اغسطس|"
    "سبتمبر|أكتوبر|اكتوبر|نوفمبر|ديسمبر"
)
AR_DATE_RE = re.compile(
    r"(?:بتاريخ|في|المؤر[خخ]\s+في|يوم|الموافق)\s*"
    r"([0-9٠-٩]{1,2})\s*(" + AR_MONTHS + r")\s*(?:سنة|عام)?\s*([0-9٠-٩]{4})"
)

# Lignes non pertinentes (abonnements, publicité, mentions d'imprimerie…).
BOILERPLATE_RE = re.compile(
    r"(abonnement|publicit[eé]|sommaire|imprimerie|journal\s+officiel|"
    r"bi-hebdomadaire|le\s+num[eé]ro|les\s+tables|direction[,.]?\s+r[eé]daction|"
    r"t[eé]l\.?|c\.?c\.?p\.?|tarif|bulletin\s+officiel|الاشتراك|إدارة|الثمن|"
    r"قيمة\s+الاشتراك|العدد|مطبعة|الإعلانات)",
    re.IGNORECASE,
)

# Page de garde / sommaire : on n'y extrait pas de textes, seulement la date.
SOMMAIRE_RE = re.compile(
    r"\bSOMMAIRE\b|\bفهرس\b|\bمحتوى\b|\bالمحتويات\b|\bفِهْرِس\b",
    re.IGNORECASE,
)

# Entrée de sommaire : renvoi de page « (p. 6) » en fin de ligne.
PAGE_REF_RE = re.compile(r"\(\s*p\.?\s*[0-9]+", re.IGNORECASE)


@dataclass
class Record:
    numero: str | None
    type_label: str | None
    date_publi: str | None
    ministere: str | None
    contenu: str
    fichier: str
    page: int
    langue: str


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
def configure_tesseract(tesseract_cmd: str, tessdata: str | None) -> None:
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata


def lang_for_path(path: Path) -> str:
    parts = {p.lower() for p in path.parts}
    if "ar" in parts:
        return "ara"
    return "fra"


def ocr_pdf(path: Path, lang: str, dpi: int, max_pages: int, psm: int) -> list[str]:
    """Rend chaque page et renvoie la liste du texte OCR (une entrée par page)."""
    pages_text: list[str] = []
    doc = fitz.open(str(path))
    config = f"--psm {psm}" if psm else ""
    for idx, page in enumerate(doc):
        if max_pages and idx >= max_pages:
            break
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        text = pytesseract.image_to_string(img, lang=lang, config=config)
        pages_text.append(text)
    doc.close()
    return pages_text


# ---------------------------------------------------------------------------
# Extraction des champs
# ---------------------------------------------------------------------------
FR_MONTH_MAP = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "aout": 8, "août": 8, "aouût": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    "décembre": 12,
}

AR_MONTH_MAP = {
    "جانفي": 1, "جانفى": 1, "يناير": 1, "فيفري": 2, "فبراير": 2,
    "مارس": 3, "أفريل": 4, "افريل": 4, "أبريل": 4, "ماي": 5, "مايو": 5,
    "جوان": 6, "يونيو": 6, "جويلية": 7, "جولية": 7, "يوليو": 7,
    "أوت": 8, "اوت": 8, "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9,
    "أكتوبر": 10, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
}


def month_number(name: str, lang: str) -> int | None:
    key = unaccent(name.lower())
    if lang == "ara":
        return AR_MONTH_MAP.get(name)
    return FR_MONTH_MAP.get(key)


def to_iso_date(day: str, month: str, year: str, lang: str) -> str | None:
    try:
        d = int(normalize_digits(day))
        y = int(normalize_digits(year))
        m = month_number(month, lang)
        if m is None:
            return None
        return date(y, m, d).isoformat()
    except (ValueError, TypeError):
        return None


def extract_issue_date(first_page_text: str, lang: str) -> str | None:
    """Date de publication : première date trouvée sur la page de garde."""
    if lang == "ara":
        m = AR_DATE_RE.search(first_page_text[:2000])
    else:
        m = FR_DATE_RE.search(first_page_text[:2000])
    if m:
        return to_iso_date(m.group(1), m.group(2), m.group(3), lang)
    return None


FR_TYPE_LABELS = {
    "decret executif": "Décret exécutif",
    "decret presidentiel": "Décret présidentiel",
    "decret": "Décret",
    "ordonnance": "Ordonnance",
    "arrete": "Arrêté",
    "arreté": "Arrêté",
    "décision": "Décision",
    "decision": "Décision",
    "circulaire": "Circulaire",
    "loi": "Loi",
    "avis": "Avis",
}


def fr_type_label(matched_type: str) -> str:
    key = unaccent(matched_type.strip().lower())
    return FR_TYPE_LABELS.get(key, matched_type.strip())


AR_TYPE_LABELS = {
    "مرسوم تنفيذي": "Décret exécutif",
    "مرسوم رئاسي": "Décret présidentiel",
    "مرسوم تشريعي": "Décret législatif",
    "مرسوم": "Décret",
    "أمر": "Ordonnance",
    "قرار": "Arrêté/Décision",
    "مقرر": "Arrêté",
    "قانون": "Loi",
    "منشور": "Circulaire",
}


def detect_header_fr(line: str) -> tuple[str | None, str | None]:
    m = FR_TYPE_NUM_RE.match(line)
    if not m:
        return None, None
    type_label = fr_type_label(m.group(1))
    # Les en-têtes sont capitalisés ; écarte les continuations en minuscule
    # (« décision du Conseil … » au milieu d'un texte).
    first = m.group(1).strip()[0]
    if first.isalpha() and first.islower():
        return None, None
    num = None
    nm = FR_NUM_RE.search(line[:120])
    if nm:
        num = re.sub(r"\s+", "", normalize_digits(nm.group(1)))
    return type_label, num


def detect_header_ar(line: str) -> tuple[str | None, str | None]:
    m = AR_TYPE_NUM_RE.search(line)
    if not m:
        return None, None
    raw_type = m.group(1).strip()
    type_label = AR_TYPE_LABELS.get(raw_type, raw_type)
    num = re.sub(r"\s+", "", normalize_digits(m.group(2)))
    return type_label, num


def detect_ministry_fr(line: str) -> str | None:
    stripped = line.strip()
    if len(stripped) > 120:
        return None
    if not FR_MINISTRY_RE.match(stripped):
        return None
    alpha = [c for c in stripped if c.isalpha()]
    if not alpha:
        return None
    upper = sum(1 for c in alpha if c.isupper())
    if upper / len(alpha) < 0.7:
        return None
    return stripped


def detect_ministry_ar(line: str) -> str | None:
    stripped = line.strip()
    if len(stripped) > 60:
        return None
    if AR_MINISTRY_RE.match(stripped):
        return stripped
    return None


def clean_content(lines: list[str]) -> str:
    kept: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if BOILERPLATE_RE.search(s):
            continue
        kept.append(s)
    text = " ".join(kept)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Extraction d'un document entier en enregistrements
# ---------------------------------------------------------------------------
def extract_records(
    pages_text: list[str],
    lang: str,
    fichier: str,
    issue_date: str | None,
) -> list[Record]:
    records: list[Record] = []
    current_ministry: str | None = None
    current: list[str] | None = None
    current_type: str | None = None
    current_num: str | None = None
    current_page: int = 0

    detect_header = detect_header_fr if lang != "ara" else detect_header_ar
    detect_ministry = detect_ministry_fr if lang != "ara" else detect_ministry_ar

    def flush() -> None:
        nonlocal current, current_type, current_num, current_page
        if current is None:
            return
        content = clean_content(current)
        # Ignorer les entrées de SOMMAIRE : texte court avec renvoi de page.
        if content and len(content) < 400 and PAGE_REF_RE.search(content):
            content = ""
        if content:
            records.append(
                Record(
                    numero=current_num,
                    type_label=current_type,
                    date_publi=issue_date,
                    ministere=current_ministry,
                    contenu=content,
                    fichier=fichier,
                    page=current_page,
                    langue="ar" if lang == "ara" else "fr",
                )
            )
        current = None
        current_type = None
        current_num = None

    for page_index, page_text in enumerate(pages_text, start=1):
        # La page de garde / sommaire ne contient pas le texte intégral.
        if SOMMAIRE_RE.search(page_text):
            continue
        for line in page_text.splitlines():
            ministry = detect_ministry(line)
            if ministry:
                current_ministry = ministry
                continue

            t, n = detect_header(line)
            if t is not None:
                flush()
                current = [line.strip()]
                current_type = t
                current_num = n
                current_page = page_index
                continue

            if current is not None:
                current.append(line)

    flush()

    # Déduplication des entrées de SOMMAIRE : garder le texte le plus long.
    deduped: dict[tuple[str, str], Record] = {}
    ordered: list[Record] = []
    for rec in records:
        if rec.numero is not None:
            key = (rec.type_label or "", rec.numero)
            if key in deduped:
                if len(rec.contenu) > len(deduped[key].contenu):
                    deduped[key] = rec
                continue
            deduped[key] = rec
        ordered.append(rec)
    result = [rec for rec in ordered if rec.numero is None]
    result = list(deduped.values()) + result
    result.sort(key=lambda r: (r.page, r.numero is None))
    return result


FILENAME_YEAR_RE = re.compile(r"^[FA](\d{4})\d{3}\.pdf$", re.IGNORECASE)


def year_from_filename(name: str) -> str | None:
    m = FILENAME_YEAR_RE.match(name)
    return m.group(1) if m else None


def process_pdf(path: Path, dpi: int, max_pages: int, psm: int) -> list[Record]:
    lang = lang_for_path(path)
    pages_text = ocr_pdf(path, lang, dpi, max_pages, psm)
    if not pages_text:
        return []
    issue_date = extract_issue_date(pages_text[0], lang)
    # Repli : année lue dans le nom de fichier (ex. A1964001.pdf -> 1964).
    if issue_date is None:
        year = year_from_filename(path.name)
        if year:
            issue_date = f"{year}-01-01"
    records = extract_records(pages_text, lang, path.name, issue_date)
    for rec in records:
        if rec.date_publi is None:
            rec.date_publi = issue_date
    return records


# ---------------------------------------------------------------------------
# Écriture des sorties
# ---------------------------------------------------------------------------
def escape_sql(value: str | None) -> str:
    if value is None:
        return "NULL"
    value = value.replace("\\", "\\\\")
    value = value.replace("\x00", "\\0")
    value = value.replace("\n", "\\n")
    value = value.replace("\r", "\\r")
    value = value.replace("\x1a", "\\Z")
    value = value.replace("'", "''")
    return f"'{value}'"


def records_to_dicts(records: list[Record]) -> list[dict]:
    out = []
    for r in records:
        out.append(
            {
                "numero": r.numero,
                "type": r.type_label,
                "date_publi": r.date_publi,
                "ministere": r.ministere,
                "contenu": r.contenu,
                "fichier": r.fichier,
                "page": r.page,
                "langue": r.langue,
            }
        )
    return out


def write_json(records: list[Record], dest: Path) -> None:
    dest.write_text(
        json.dumps(records_to_dicts(records), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_csv(records: list[Record], dest: Path) -> None:
    fieldnames = [
        "numero", "type", "date_publi", "ministere", "contenu",
        "fichier", "page", "langue",
    ]
    with dest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records_to_dicts(records))


def write_sql(records: list[Record], dest: Path, table: str) -> None:
    cols = "id, numero, date_publi, ministere, contenu, fichier, page"
    lines = [
        "-- INSERT INTO générés par etl_joradp.py",
        "-- Table cible :",
        f"--   CREATE TABLE {table} (",
        "--     id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,",
        "--     numero VARCHAR(64) NULL,",
        "--     date_publi DATE NULL,",
        "--     ministere VARCHAR(255) NULL,",
        "--     contenu TEXT NULL,",
        "--     fichier VARCHAR(255) NULL,",
        "--     page INT NULL",
        "--   ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
        f"INSERT INTO `{table}` ({cols}) VALUES",
    ]
    values: list[str] = []
    for r in records:
        date_sql = escape_sql(r.date_publi)
        values.append(
            "  (NULL, "
            f"{escape_sql(r.numero)}, {date_sql}, {escape_sql(r.ministere)}, "
            f"{escape_sql(r.contenu)}, {escape_sql(r.fichier)}, "
            f"{r.page if r.page else 'NULL'})"
        )
    lines.append(",\n".join(values) + ";")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ETL JORADP : OCR + extraction + CSV/JSON/SQL."
    )
    parser.add_argument(
        "--pdf-dir", type=Path, default=Path("pdfs"),
        help="Dossier racine des PDF (défaut : pdfs).",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=Path("output"),
        help="Dossier de sortie (défaut : output).",
    )
    parser.add_argument("--table", default=DEFAULT_TABLE, help="Nom de la table SQL.")
    parser.add_argument("--tesseract-cmd", default=TESSERACT_CMD)
    parser.add_argument("--tessdata", default=TESSDATA_DIR)
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--psm", type=int, default=3, help="Mode de segmentation Tesseract.")
    parser.add_argument(
        "--max-pages", type=int, default=0,
        help="Limiter le nombre de pages OCR par PDF (0 = tout).",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Limiter le nombre de PDF traités (0 = tout).",
    )
    parser.add_argument("--lang", choices=["auto", "fr", "ar"], default="auto")
    return parser.parse_args()


def iter_pdfs(root: Path, lang_filter: str) -> list[Path]:
    pdfs = sorted(root.rglob("*.pdf"))
    if lang_filter != "auto":
        pdfs = [p for p in pdfs if lang_filter in {part.lower() for part in p.parts}]
    return pdfs


def main() -> int:
    args = parse_args()
    if not args.pdf_dir.exists():
        print(f"Dossier introuvable : {args.pdf_dir}", file=sys.stderr)
        return 2

    configure_tesseract(args.tesseract_cmd, args.tessdata)

    pdfs = iter_pdfs(args.pdf_dir, args.lang)
    total = len(pdfs)
    if args.limit:
        pdfs = pdfs[: args.limit]
    print(f"{total} PDF détectés, {len(pdfs)} à traiter.", file=sys.stderr)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    all_records: list[Record] = []

    for i, pdf in enumerate(pdfs, start=1):
        try:
            records = process_pdf(pdf, args.dpi, args.max_pages, args.psm)
        except Exception as exc:  # robustesse : un PDF cassé ne stoppe pas le lot
            print(f"[{i}/{len(pdfs)}] ERREUR {pdf.name}: {exc}", file=sys.stderr)
            continue
        all_records.extend(records)
        print(
            f"[{i}/{len(pdfs)}] {pdf.name}: {len(records)} textes", file=sys.stderr
        )

    json_path = args.out_dir / "jo_extrait.json"
    csv_path = args.out_dir / "jo_extrait.csv"
    sql_path = args.out_dir / "jo_extrait.sql"

    write_json(all_records, json_path)
    write_csv(all_records, csv_path)
    write_sql(all_records, sql_path, args.table)

    print(
        f"Terminé : {len(all_records)} textes -> "
        f"{json_path.name}, {csv_path.name}, {sql_path.name}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
