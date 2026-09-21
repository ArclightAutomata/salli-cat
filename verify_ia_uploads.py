#!/usr/bin/env python3
"""Verify that all manuals marked as archived actually exist on Internet Archive."""

import argparse
import logging
import urllib.request
import urllib.error

import database
from ia_uploader import build_identifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def check_ia_exists(archive_url: str) -> bool:
    """Check if an Internet Archive item exists."""
    try:
        req = urllib.request.Request(archive_url, method='HEAD')
        req.add_header('User-Agent', 'Mozilla/5.0 (compatible; ManualsLibScraper/1.0)')
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        logger.warning(f"HTTP error {e.code} checking {archive_url}")
        return False
    except Exception as e:
        logger.warning(f"Error checking {archive_url}: {e}")
        return False


def get_archived_manuals() -> list[dict]:
    """Get all manuals marked as archived."""
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM manuals
        WHERE archived = 1
        ORDER BY brand, model
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_downloaded_not_archived() -> list[dict]:
    """Get manuals that are downloaded but not marked as archived."""
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM manuals
        WHERE downloaded = 1 AND archived = 0
        ORDER BY brand, model
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def unmark_archived(manual_id: int):
    """Remove archived flag from a manual."""
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE manuals
        SET archived = 0, archive_url = NULL
        WHERE id = ?
    """, (manual_id,))
    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Verify Internet Archive uploads")
    parser.add_argument("--fix", action="store_true", help="Unmark manuals that don't exist on IA")
    parser.add_argument("--check-unarchived", action="store_true",
                        help="Also check if downloaded-but-not-archived items exist on IA")
    args = parser.parse_args()

    database.init_db()

    # Check archived manuals
    archived = get_archived_manuals()
    logger.info(f"Checking {len(archived)} manuals marked as archived...")

    missing = []
    verified = 0

    for i, manual in enumerate(archived, 1):
        archive_url = manual.get("archive_url")
        logger.info(f"[{i}/{len(archived)}] Checking {manual['brand']} {manual['model']}...")

        if not archive_url:
            logger.warning(f"  ✗ No archive_url in database")
            missing.append(manual)
            continue

        exists = check_ia_exists(archive_url)
        if exists:
            verified += 1
            logger.info(f"  ✓ Exists: {archive_url}")
        else:
            logger.warning(f"  ✗ NOT FOUND: {archive_url}")
            missing.append(manual)

    print()
    print("=" * 60)
    print(f"Verified: {verified}")
    print(f"Missing:  {len(missing)}")
    print("=" * 60)

    if missing:
        print("\nMissing items:")
        for m in missing:
            print(f"  - {m['brand']} {m['model']} (id={m['id']})")
            print(f"    URL: {m.get('archive_url', 'N/A')}")

        if args.fix:
            print(f"\nUnmarking {len(missing)} items as not archived...")
            for m in missing:
                unmark_archived(m["id"])
            print("Done. These items can now be re-uploaded.")

    # Optionally check if unarchived items actually exist on IA
    if args.check_unarchived:
        print()
        print("=" * 60)
        print("Checking downloaded-but-not-archived items...")
        print("=" * 60)

        unarchived = get_downloaded_not_archived()
        logger.info(f"Checking {len(unarchived)} unarchived manuals...")

        found_on_ia = []
        for i, manual in enumerate(unarchived, 1):
            identifier = build_identifier(manual, fallback=False)
            if not identifier:
                logger.info(f"[{i}/{len(unarchived)}] {manual['brand']} {manual['model']} - no source ID, skipping")
                continue

            archive_url = f"https://archive.org/details/{identifier}"
            logger.info(f"[{i}/{len(unarchived)}] Checking {manual['brand']} {manual['model']}...")

            exists = check_ia_exists(archive_url)
            if exists:
                logger.info(f"  ✓ Found on IA: {archive_url}")
                found_on_ia.append((manual, archive_url))
            else:
                logger.info(f"  - Not on IA")

        if found_on_ia:
            print(f"\nFound {len(found_on_ia)} items on IA that aren't marked as archived:")
            for m, url in found_on_ia:
                print(f"  - {m['brand']} {m['model']} -> {url}")

            if args.fix:
                print(f"\nMarking {len(found_on_ia)} items as archived...")
                for m, url in found_on_ia:
                    database.update_archived(m["id"], url)
                print("Done.")


if __name__ == "__main__":
    main()
