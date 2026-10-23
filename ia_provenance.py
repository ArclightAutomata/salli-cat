#!/usr/bin/env python3
"""Record who uploaded each archive.org item our manuals point at.

`archived = 1` only says an item exists at the expected identifier. Many were
already there - uploaded by us from an earlier database, or by third parties
(e.g. slaxemulator@gmail.com's 2020 ManualsLib/ManualsBase mirrors) - so the
uploader is read from each item's own metadata into the provenance columns
(archive_uploader, archived_by_us, archive_added_at, archive_dark).

Dark items hide their metadata even from the uploading account, so for them
only archive_dark = 1 is known.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import internetarchive as ia

import database

logger = logging.getLogger(__name__)

# The archive.org account salli-cat uploads with
OUR_IA_UPLOADER = "root+archiving@arclight.run"


def identifier_of(archive_url: str) -> str:
    return archive_url.rstrip("/").rsplit("/", 1)[-1]


def fetch(identifier: str, session=None) -> dict | None:
    """Read an item's provenance from its metadata. None if the item is gone."""
    session = session or ia.get_session()
    data = session.get(f"https://archive.org/metadata/{identifier}", timeout=60).json()
    if not data:
        return None
    if data.get("is_dark"):
        return {"uploader": None, "added_at": None, "by_us": None, "dark": True}
    meta = data.get("metadata", {})
    uploader = meta.get("uploader")
    return {
        "uploader": uploader,
        "added_at": meta.get("addeddate"),
        "by_us": None if uploader is None else uploader == OUR_IA_UPLOADER,
        "dark": False,
    }


def record(archive_url: str, session=None) -> dict | None:
    """Fetch an item's provenance and store it on every row that points at it."""
    prov = fetch(identifier_of(archive_url), session)
    if prov is not None:
        database.set_archive_provenance(archive_url, prov["uploader"], prov["added_at"], prov["by_us"], prov["dark"])
    return prov


def backfill(workers: int = 4, limit: int = None) -> None:
    """Fill provenance for every archived manual that doesn't have it yet.

    Our own public uploads come from one search; everything else needs a
    metadata request per item.
    """
    session = ia.get_session()
    pending = set(database.get_archive_urls_missing_provenance())
    logger.info(f"{len(pending)} archive.org items without provenance")

    by_id = {identifier_of(u): u for u in pending}
    ours = 0
    for item in session.search_items(f'uploader:"{OUR_IA_UPLOADER}"', fields=["identifier", "addeddate"]):
        url = by_id.get(item["identifier"])
        if url:
            database.set_archive_provenance(url, OUR_IA_UPLOADER, item.get("addeddate"), True, False)
            pending.discard(url)
            ours += 1
    logger.info(f"{ours} are our own public uploads; checking the other {len(pending)} one by one")

    rest = sorted(pending)[:limit] if limit else sorted(pending)
    counts = {"ours": 0, "others": 0, "dark": 0, "missing": 0, "error": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(record, url, session): url for url in rest}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                prov = fut.result()
            except Exception as e:
                counts["error"] += 1
                logger.warning(f"{futures[fut]}: {e}")
                continue
            if prov is None:
                counts["missing"] += 1
            elif prov["dark"]:
                counts["dark"] += 1
            elif prov["by_us"]:
                counts["ours"] += 1
            else:
                counts["others"] += 1
            if i % 500 == 0:
                logger.info(f"{i}/{len(rest)} checked: {counts}")
    logger.info(f"Done: {ours} ours from search, then {counts}")
