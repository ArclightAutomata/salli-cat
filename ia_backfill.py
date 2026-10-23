#!/usr/bin/env python3
"""Download local copies of manuals that are on archive.org but not downloaded.

The scrapers skip downloading anything already on archive.org, so those
manuals have no local file. This fetches the PDF back from the archive.org
item and records it with file_origin = "archive.org" (provenance of the item
itself stays in archive_uploader / archived_by_us).
"""
import logging
import shutil
import tempfile
from pathlib import Path

import internetarchive as ia

import database
from ia_provenance import identifier_of
from manualslib_scraper import compute_checksums, get_sha1_storage_path

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"


def pick_pdf(item) -> dict | None:
    """The item's uploaded PDF (not IA's derivatives)."""
    pdfs = [f for f in item.files if f["name"].lower().endswith(".pdf")]
    originals = [f for f in pdfs if f.get("source") == "original"]
    return (originals or pdfs or [None])[0]


def backfill(manual_ids: list[int]) -> dict:
    conn = database.get_connection()
    q = f"SELECT id, archive_url FROM manuals WHERE id IN ({','.join('?' * len(manual_ids))}) AND downloaded = 0 AND archived = 1 AND archive_url IS NOT NULL"
    rows = conn.execute(q, manual_ids).fetchall()
    conn.close()

    by_url = {}
    for r in rows:
        by_url.setdefault(r["archive_url"], []).append(r["id"])
    counts = {"downloaded": 0, "rows": 0, "dark": 0, "no_pdf": 0, "error": 0}
    session = ia.get_session()
    for i, (url, ids) in enumerate(sorted(by_url.items()), 1):
        identifier = identifier_of(url)
        try:
            item = session.get_item(identifier)
            if item.is_dark or not item.exists:
                counts["dark"] += 1
                continue
            f = pick_pdf(item)
            if not f:
                counts["no_pdf"] += 1
                logger.warning(f"{identifier}: no PDF in item")
                continue
            with tempfile.TemporaryDirectory() as tmp:
                r = session.get(f"https://archive.org/download/{identifier}/{f['name']}", stream=True, timeout=120)
                r.raise_for_status()
                with open(f"{tmp}/m.pdf", "wb") as out:
                    for chunk in r.iter_content(1 << 16):
                        out.write(chunk)
                sha1, md5 = compute_checksums(Path(f"{tmp}/m.pdf"))
                dest = get_sha1_storage_path(DOWNLOAD_DIR, sha1, ".pdf")
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    shutil.move(f"{tmp}/m.pdf", dest)
            size = dest.stat().st_size
            for mid in ids:
                database.update_downloaded(mid, str(dest), sha1, md5, size, original_filename=f["name"],
                                           file_origin="archive.org")
            counts["downloaded"] += 1
            counts["rows"] += len(ids)
        except Exception as e:
            counts["error"] += 1
            logger.warning(f"{identifier}: {e}")
        if i % 25 == 0:
            logger.info(f"{i}/{len(by_url)} items: {counts}")
    logger.info(f"Done: {counts}")
    return counts


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    ids = [int(x) for x in sys.stdin.read().split()]
    backfill(ids)
