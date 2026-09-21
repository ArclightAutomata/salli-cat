#!/usr/bin/env python3
"""Background process to check if manuals exist on archive.org.

Slowly checks manuals against archive.org to pre-identify which ones
are already archived, avoiding unnecessary downloads.
"""

import argparse
import logging
import random
import time
import urllib.request
import urllib.error

import database
from ia_uploader import build_identifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Rate limiting settings
DEFAULT_DELAY_MIN = 5.0   # Minimum seconds between requests
DEFAULT_DELAY_MAX = 15.0  # Maximum seconds between requests
DEFAULT_BATCH_SIZE = 50   # How many to check before longer pause
DEFAULT_BATCH_PAUSE = 60  # Seconds to pause between batches


def get_archive_url_for_manual(manual: dict) -> str | None:
    """Get the expected archive.org URL for a manual based on its source."""
    identifier = build_identifier(manual, fallback=False)
    if identifier:
        return f"https://archive.org/details/{identifier}"
    return None


def check_archive_org(archive_url: str) -> bool:
    """Check if an item exists on archive.org. Returns True if exists."""
    try:
        req = urllib.request.Request(archive_url, method='HEAD')
        req.add_header('User-Agent', 'Mozilla/5.0 (compatible; Salica/1.0)')
        with urllib.request.urlopen(req, timeout=10) as response:
            # 200 means it exists
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        logger.warning(f"HTTP error checking archive.org: {e.code}")
        return False
    except Exception as e:
        logger.warning(f"Error checking archive.org: {e}")
        return False


def random_delay(min_sec: float, max_sec: float):
    """Sleep for a random duration between min and max seconds."""
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)


def run_checker(
    delay_min: float = DEFAULT_DELAY_MIN,
    delay_max: float = DEFAULT_DELAY_MAX,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_pause: int = DEFAULT_BATCH_PAUSE,
    continuous: bool = False,
    limit: int = None,
):
    """Run the archive checker.

    Args:
        delay_min: Minimum delay between checks (seconds)
        delay_max: Maximum delay between checks (seconds)
        batch_size: Number of checks before taking a longer pause
        batch_pause: Seconds to pause between batches
        continuous: If True, run forever checking new manuals as they appear
        limit: Maximum number of manuals to check (None = no limit)
    """
    database.init_db()

    total_checked = 0
    total_found = 0
    batch_count = 0

    while True:
        # Get manuals that need checking
        manuals = database.get_manuals_needing_archive_check(limit=100)

        if not manuals:
            if continuous:
                logger.info("No manuals to check. Waiting 5 minutes before rechecking...")
                time.sleep(300)
                continue
            else:
                logger.info("No more manuals to check.")
                break

        for manual in manuals:
            if limit and total_checked >= limit:
                logger.info(f"Reached limit of {limit} checks.")
                break

            archive_url = get_archive_url_for_manual(manual)
            if not archive_url:
                logger.warning(f"  Skipping {manual['brand']} {manual['model']} - no ID available")
                continue

            source = manual.get("source", "manualslib")
            manual_id = manual.get("manualslib_id") or manual.get("source_id")
            logger.info(f"Checking: {manual['brand']} {manual['model']} ({source}: {manual_id})")

            is_archived = check_archive_org(archive_url)

            if is_archived:
                logger.info(f"  FOUND on archive.org: {archive_url}")
                database.update_archive_checked(manual["id"], True, archive_url)
                total_found += 1
            else:
                logger.debug(f"  Not on archive.org")
                database.update_archive_checked(manual["id"], False)

            total_checked += 1
            batch_count += 1

            # Batch pause
            if batch_count >= batch_size:
                logger.info(f"Batch complete. Checked {total_checked}, found {total_found}. Pausing {batch_pause}s...")
                time.sleep(batch_pause)
                batch_count = 0
            else:
                random_delay(delay_min, delay_max)

        if limit and total_checked >= limit:
            break

    # Final stats
    stats = database.get_archive_check_stats()
    logger.info(f"Archive check complete.")
    logger.info(f"  This run: checked {total_checked}, found {total_found}")
    logger.info(f"  Overall: {stats['archived']} archived, {stats['checked_not_archived']} checked (not archived), {stats['never_checked']} never checked")


def print_stats():
    """Print current archive check statistics."""
    database.init_db()
    stats = database.get_archive_check_stats()

    print("\nArchive.org Check Statistics")
    print("=" * 40)
    print(f"Total checkable (have source ID):  {stats['total_checkable']}")
    print(f"Already archived:                  {stats['archived']}")
    print(f"Checked, not archived:             {stats['checked_not_archived']}")
    print(f"Never checked:                     {stats['never_checked']}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Check manuals against archive.org",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check all pending manuals once (with default rate limiting)
  python archive_checker.py

  # Run continuously, checking new manuals as they appear
  python archive_checker.py --continuous

  # Check with faster rate (be careful not to hit rate limits)
  python archive_checker.py --delay-min 2 --delay-max 5

  # Just show current statistics
  python archive_checker.py --stats
"""
    )
    parser.add_argument("--continuous", action="store_true",
                        help="Run continuously, checking new manuals as they appear")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum number of manuals to check")
    parser.add_argument("--delay-min", type=float, default=DEFAULT_DELAY_MIN,
                        help=f"Minimum delay between checks in seconds (default: {DEFAULT_DELAY_MIN})")
    parser.add_argument("--delay-max", type=float, default=DEFAULT_DELAY_MAX,
                        help=f"Maximum delay between checks in seconds (default: {DEFAULT_DELAY_MAX})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Number of checks before batch pause (default: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--batch-pause", type=int, default=DEFAULT_BATCH_PAUSE,
                        help=f"Seconds to pause between batches (default: {DEFAULT_BATCH_PAUSE})")
    parser.add_argument("--stats", action="store_true",
                        help="Just print current statistics and exit")

    args = parser.parse_args()

    if args.stats:
        print_stats()
        return

    logger.info("Starting archive.org checker...")
    logger.info(f"Settings: delay={args.delay_min}-{args.delay_max}s, batch_size={args.batch_size}, batch_pause={args.batch_pause}s")

    try:
        run_checker(
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            batch_size=args.batch_size,
            batch_pause=args.batch_pause,
            continuous=args.continuous,
            limit=args.limit,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        print_stats()


if __name__ == "__main__":
    main()
