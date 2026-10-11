#!/usr/bin/env python3
"""Re-derive the stripped variant of downloaded manuals from their originals.

The watermark stripper improves over time (ManualsLib's wrapper pages, for
example, were only handled from October 2026), and the untouched download is
kept as the `original` file variant, so any manual can be re-stripped later.
For each distinct original this:

  1. verifies the original on disk still matches its recorded SHA-1
     (originals are never modified - stripping works on a temp copy),
  2. runs the current stripper on a copy,
  3. if the result differs from the manual's current primary file, stores it
     under its content-addressed path and repoints the `stripped` variant and
     the manual's primary file columns at it, for every manual sharing that
     original,
  4. deletes superseded stripped files once nothing references them.

By default only manuals not yet uploaded to the Internet Archive are
processed; already-archived ones need a re-upload afterwards, so they are
opted into with --include-archived.
"""

import argparse
import logging
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

import database
from manualslib_scraper import compute_checksums, get_sha1_storage_path, load_config
from pdf_utils import strip_manualslib_watermark

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_originals(source: str, include_archived: bool, limit: int | None = None) -> list[dict]:
    """Distinct original files of downloaded manuals, with the manuals that share each."""
    conn = database.get_connection()
    query = """
        SELECT o.file_path, o.file_sha1, o.file_md5, o.file_size,
               m.id AS manual_id, m.file_path AS primary_path, m.file_sha1 AS primary_sha1
        FROM manuals m
        JOIN file_variants o ON o.manual_id = m.id AND o.variant_type = 'original'
        WHERE m.source = ? AND m.downloaded = 1 AND m.file_path IS NOT NULL
    """
    if not include_archived:
        query += " AND m.archived = 0"
    rows = conn.execute(query + " ORDER BY o.file_sha1, m.id", (source,)).fetchall()
    conn.close()

    groups = {}
    for row in rows:
        g = groups.setdefault(row["file_sha1"], {
            "file_path": row["file_path"], "file_sha1": row["file_sha1"],
            "file_md5": row["file_md5"], "file_size": row["file_size"], "manuals": [],
        })
        g["manuals"].append({"id": row["manual_id"], "primary_path": row["primary_path"], "primary_sha1": row["primary_sha1"]})
    originals = list(groups.values())
    return originals[:limit] if limit else originals


def restrip_original(original: dict, download_dir: Path, dry_run: bool) -> tuple[str, list[str]]:
    """
    Re-strip one original for all manuals sharing it.

    Returns (outcome, superseded_paths) where outcome is one of
    "updated", "unchanged", "missing", "corrupt", "error".
    """
    src = Path(original["file_path"])
    if not src.exists():
        logger.error(f"Original missing on disk: {src}")
        return "missing", []
    sha1, _ = compute_checksums(src)
    if sha1 != original["file_sha1"]:
        logger.error(f"Original {src} has SHA-1 {sha1[:8]}, expected {original['file_sha1'][:8]} - skipping")
        return "corrupt", []

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        shutil.copyfile(src, temp_path)
        modified = strip_manualslib_watermark(temp_path)
        if modified:
            new_sha1, new_md5 = compute_checksums(temp_path)
            new_size = temp_path.stat().st_size
            variant_type = "stripped"
        else:
            # Nothing to strip: the original is the file to use
            new_sha1, new_md5, new_size = original["file_sha1"], original["file_md5"], original["file_size"]
            variant_type = "original"

        stale = [m for m in original["manuals"] if m["primary_sha1"] != new_sha1]
        if not stale:
            return "unchanged", []

        new_path = get_sha1_storage_path(download_dir, new_sha1)
        if dry_run:
            logger.info(f"Would re-strip {src.name[:12]} -> {new_sha1[:8]} ({len(stale)} manual(s))")
            return "updated", []

        if modified and not new_path.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_path), new_path)
        superseded = set()
        for m in stale:
            if variant_type == "original":
                # The current stripped variant was a false positive of an older stripper
                database.delete_file_variant(m["id"], "stripped")
            database.replace_primary_variant(m["id"], variant_type, str(new_path), new_sha1, new_md5, new_size)
            if m["primary_path"] and m["primary_path"] != str(new_path) and m["primary_path"] != str(src):
                superseded.add(m["primary_path"])
        logger.info(f"Re-stripped {src.name[:12]} -> {new_sha1[:8]} for {len(stale)} manual(s)")
        return "updated", sorted(superseded)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error re-stripping {src}: {e}")
        return "error", []
    finally:
        temp_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Re-strip downloaded manuals from their originals")
    parser.add_argument("--source", default="manualslib")
    parser.add_argument("--include-archived", action="store_true", help="Also re-strip manuals already uploaded to IA (they will need re-uploading)")
    parser.add_argument("--limit", type=int, help="Max distinct originals to process")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without touching files or the database")
    args = parser.parse_args()

    database.init_db()
    config = load_config()
    download_dir = Path(config.get("download_dir", "./downloads")).resolve()

    originals = get_originals(args.source, args.include_archived, args.limit)
    manuals = sum(len(o["manuals"]) for o in originals)
    logger.info(f"{len(originals)} distinct originals covering {manuals} manuals to check{' (dry run)' if args.dry_run else ''}")

    counts = defaultdict(int)
    superseded = set()
    for i, original in enumerate(originals, 1):
        if i % 100 == 0:
            logger.info(f"... {i}/{len(originals)}")
        outcome, paths = restrip_original(original, download_dir, args.dry_run)
        counts[outcome] += 1
        superseded.update(paths)

    freed = 0
    deleted = 0
    for path in sorted(superseded):
        p = Path(path)
        if p.exists() and not database.is_file_referenced(path):
            freed += p.stat().st_size
            p.unlink()
            deleted += 1

    logger.info(
        f"Re-strip complete: {counts['updated']} updated, {counts['unchanged']} already current, "
        f"{counts['missing']} missing, {counts['corrupt']} corrupt, {counts['error']} errors; "
        f"deleted {deleted} superseded files ({freed / 1e6:.0f} MB)"
    )


if __name__ == "__main__":
    main()
