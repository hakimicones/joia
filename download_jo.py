#!/usr/bin/env python3
"""Télécharge les JO algériens (FR + AR) depuis joradp.dz, 1962 → année en cours.

Les liens PDF ne sont pas listés sur Index.htm : ils sont construits par JavaScript
sur https://www.joradp.dz/JRN/ZF{année}.htm et ZA{année}.htm.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.joradp.dz"
USER_AGENT = (
    "joradp-downloader/1.0 (usage personnel d'archivage des JO publics; "
    "contact: local script)"
)
YEAR_START = 1962
EDITIONS = (
    ("fr", "F", "ZF"),
    ("ar", "A", "ZA"),
)

RE_MAXWIN_PREFIX = re.compile(
    r"""["'](/FTP/[^"']+)["']\s*\+\s*Adr\s*\+\s*["']\.pdf["']""",
    re.IGNORECASE,
)
RE_MAXWIN_CALL = re.compile(r"""MaxWin\(\s*['"](\d{1,3})['"]\s*\)""", re.IGNORECASE)
RE_OPTION = re.compile(
    r"""<option[^>]*\bvalue\s*=\s*["'](\d{1,3})["']""",
    re.IGNORECASE,
)


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/pdf,*/*",
            "Accept-Language": "fr,ar,en;q=0.8",
        }
    )
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def decode_html(content: bytes) -> str:
    if content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff"):
        return content.decode("utf-16")
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig")
    # Les pages arabes JORADP arrivent parfois en UTF-16 sans que
    # requests le détecte (ISO-8859-1 par défaut).
    if b"\x00" in content[:200]:
        return content.decode("utf-16")
    for encoding in ("utf-8", "cp1256"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def pad_issue(raw: str) -> str:
    return f"{int(raw):03d}"


def absolute_url(path: str) -> str:
    path = path.strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return BASE + path


def parse_listing(html: str) -> tuple[str | None, list[str]]:
    prefix_match = RE_MAXWIN_PREFIX.search(html)
    prefix = prefix_match.group(1) if prefix_match else None

    issues = [pad_issue(m.group(1)) for m in RE_MAXWIN_CALL.finditer(html)]
    if not issues:
        issues = [pad_issue(m.group(1)) for m in RE_OPTION.finditer(html)]

    seen: set[str] = set()
    unique: list[str] = []
    for issue in issues:
        if issue not in seen:
            seen.add(issue)
            unique.append(issue)
    unique.sort()
    return prefix, unique


def fetch_listing(
    session: requests.Session, year: int, page_code: str, timeout: float
) -> tuple[str | None, list[str], str | None]:
    url = f"{BASE}/JRN/{page_code}{year}.htm"
    try:
        response = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        return None, [], f"requête: {exc}"

    if response.status_code == 404:
        return None, [], "404"
    if response.status_code >= 400:
        return None, [], f"HTTP {response.status_code}"

    prefix, issues = parse_listing(decode_html(response.content))
    if not prefix:
        return None, [], "préfixe MaxWin introuvable"
    if not issues:
        return prefix, [], "aucun numéro trouvé"
    return prefix, issues, None


def inventory_rows(
    session: requests.Session,
    year_from: int,
    year_to: int,
    delay: float,
    timeout: float,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for year in range(year_from, year_to + 1):
        for lang, _letter, page_code in EDITIONS:
            prefix, issues, error = fetch_listing(session, year, page_code, timeout)
            if error:
                print(f"[skip] {year} {lang}: {error}", file=sys.stderr)
            else:
                print(f"[ok]   {year} {lang}: {len(issues)} numéros", file=sys.stderr)
                for issue in issues:
                    pdf_url = absolute_url(f"{prefix}{issue}.pdf")
                    filename = Path(pdf_url).name
                    rel_path = f"pdfs/{lang}/{year}/{filename}"
                    rows.append(
                        {
                            "year": str(year),
                            "lang": lang,
                            "issue": issue,
                            "url": pdf_url,
                            "path": rel_path,
                        }
                    )
            time.sleep(delay + random.uniform(0, delay * 0.25))
    return rows


def save_inventory(rows: list[dict[str, str]], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["year", "lang", "issue", "url", "path"]
        )
        writer.writeheader()
        writer.writerows(rows)
    json_path = dest.with_suffix(".json")
    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_inventory(src: Path) -> list[dict[str, str]]:
    with src.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def looks_like_pdf(content: bytes) -> bool:
    head = content.lstrip()[:5]
    return head.startswith(b"%PDF")


def download_one(
    session: requests.Session,
    row: dict[str, str],
    root: Path,
    timeout: float,
    min_bytes: int,
) -> str:
    dest = root / row["path"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size >= min_bytes:
        return "skip"

    response = session.get(row["url"], timeout=timeout, stream=True)
    if response.status_code == 404:
        return "404"
    response.raise_for_status()

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total == len(chunk) and not looks_like_pdf(chunk):
            return "not-pdf"

    data = b"".join(chunks)
    if len(data) < min_bytes:
        return "too-small"

    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return "ok"


def download_all(
    session: requests.Session,
    rows: list[dict[str, str]],
    root: Path,
    delay: float,
    timeout: float,
    min_bytes: int,
) -> dict[str, int]:
    stats = {"ok": 0, "skip": 0, "404": 0, "not-pdf": 0, "too-small": 0, "error": 0}
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        label = f"{row['year']} {row['lang']} n°{row['issue']}"
        try:
            status = download_one(session, row, root, timeout, min_bytes)
        except requests.RequestException as exc:
            status = "error"
            print(f"[{index}/{total}] {label}: erreur {exc}", file=sys.stderr)
        else:
            print(f"[{index}/{total}] {label}: {status}", file=sys.stderr)
        stats[status] = stats.get(status, 0) + 1
        if status != "skip":
            time.sleep(delay + random.uniform(0, delay * 0.25))
    return stats


def parse_args() -> argparse.Namespace:
    today_year = date.today().year
    parser = argparse.ArgumentParser(
        description="Inventaire et téléchargement des JO JORADP (FR + AR)."
    )
    parser.add_argument("--year-from", type=int, default=YEAR_START)
    parser.add_argument("--year-to", type=int, default=today_year)
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="Construire le CSV sans télécharger.",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Télécharger à partir d'un inventaire déjà généré.",
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path("inventory") / "jo.csv",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="Pause entre requêtes (s).")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--min-bytes", type=int, default=2048)
    parser.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Limiter le nombre de téléchargements (0 = pas de limite).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.year_from < YEAR_START:
        print(f"Les JO en ligne commencent en {YEAR_START}.", file=sys.stderr)
        return 2
    if args.year_from > args.year_to:
        print("--year-from doit être <= --year-to", file=sys.stderr)
        return 2

    root = Path.cwd()
    session = build_session()

    if args.download_only:
        if not args.inventory.exists():
            print(f"Inventaire introuvable: {args.inventory}", file=sys.stderr)
            return 2
        rows = load_inventory(args.inventory)
    else:
        rows = inventory_rows(
            session, args.year_from, args.year_to, args.delay, args.timeout
        )
        save_inventory(rows, args.inventory)
        print(f"Inventaire: {len(rows)} PDF → {args.inventory}", file=sys.stderr)

    if args.inventory_only:
        return 0

    if args.max_files and args.max_files > 0:
        rows = rows[: args.max_files]

    stats = download_all(
        session, rows, root, args.delay, args.timeout, args.min_bytes
    )
    print("Résumé:", json.dumps(stats, ensure_ascii=False), file=sys.stderr)
    return 0 if stats.get("error", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
