#!/usr/bin/env python3
"""Internet Archive uploader for manual PDFs (ManualsLib, ManualsBase, etc.)."""

import hashlib
import logging
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import internetarchive as ia

import database
from pdf_utils import repair_pdf


def get_git_commit() -> str | None:
    """Get the current git commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None

logger = logging.getLogger(__name__)


def sanitize_xml_string(text: str) -> str:
    """
    Remove XML-incompatible characters from a string.

    XML 1.0 only allows: #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD]
    """
    if not text:
        return text
    # Remove control characters (0x00-0x1F except tab, newline, carriage return)
    cleaned = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)
    return cleaned


IA_IDENTIFIER_MAX_LEN = 100
# What archive.org accepts: alphanumeric start, then alphanumerics/_/-/., 5-100 chars.
IA_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{4,99}$")


def _clean_identifier_chars(text: str) -> str:
    """Reduce text to the identifier-safe alphabet [a-zA-Z0-9_-]."""
    # Replace spaces and special chars with underscores
    cleaned = re.sub(r'[^a-zA-Z0-9_-]', '_', text)
    # Collapse multiple underscores
    cleaned = re.sub(r'_+', '_', cleaned)
    # Remove leading/trailing underscores
    return cleaned.strip('_')


def sanitize_identifier(text: str) -> str:
    """
    Create a valid Internet Archive identifier from text.

    IA identifiers must be 5-100 chars, alphanumeric with underscores/dashes.
    """
    identifier = _clean_identifier_chars(text)
    # Ensure minimum length
    if len(identifier) < 5:
        identifier = identifier + '_manual'
    # Truncate if too long
    if len(identifier) > IA_IDENTIFIER_MAX_LEN:
        identifier = identifier[:IA_IDENTIFIER_MAX_LEN]
    return identifier


SLUG_HASH_LEN = 8


def slug_identifier(prefix: str, slug: str) -> str:
    """
    Build an IA identifier from a source's textual key (a URL slug / file name):
    ``<prefix>-<sanitized slug>-<short sha1 of the raw slug>``.

    Unlike numeric IDs, slugs can be long and contain arbitrary characters.
    Sanitizing is lossy (``x.pdf`` and ``x_pdf`` collapse together) and the
    100-char limit may force truncation, so a hash of the *full, raw* slug is
    always appended: the identifier stays human-readable while uniqueness comes
    from the hash. When the readable part doesn't fit it is cut, never the hash.
    """
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:SLUG_HASH_LEN]
    readable = f"{prefix}-{_clean_identifier_chars(slug)}"
    keep = IA_IDENTIFIER_MAX_LEN - len(digest) - 1
    if len(readable) > keep:
        readable = readable[:keep].rstrip('_-')
    return f"{readable}-{digest}"


def build_identifier(manual: dict, fallback: bool = True) -> str | None:
    """
    Return the Internet Archive identifier for a manual.

    Each source has a stable key that makes the identifier deterministic, so
    uploads, archive checks and verification all agree on the same item:
      manualslib:   manualslib-id-<numeric id>
      manualsbase:  manualsbase-id-<numeric id>
      manualzz:     manualzz-id-<numeric id>
      elektrotanya: elektrotanya-<url slug>-<sha1(slug)[:8]>
                    (the site has no numeric IDs; the slug is its file name,
                    e.g. "sony_kv-1400_sm.pdf" from /sony_kv-1400_sm.pdf/download.html,
                    and the hash keeps the identifier unique after sanitizing
                    and truncating - see slug_identifier)

    When a manual has no stable key, ``fallback=True`` derives an identifier
    from brand/model (used for uploads); ``fallback=False`` returns None (used
    for existence checks, where a guessed identifier isn't worth a request).
    """
    source = manual.get("source") or "manualslib"
    source_id = manual.get("source_id")

    if source == "manualsbase":
        if source_id:
            return f"manualsbase-id-{source_id}"
    elif source == "manualzz":
        if source_id:
            return f"manualzz-id-{source_id}"
    elif source == "elektrotanya":
        if source_id:
            return slug_identifier("elektrotanya", source_id)
    elif source == "manualslib":
        manualslib_id = manual.get("manualslib_id") or source_id
        if manualslib_id:
            return f"manualslib-id-{manualslib_id}"
    else:
        # Unknown source: never borrow another source's namespace.
        logger.warning(f"No identifier scheme for source {source!r}; using a sanitized generic one")
        if source_id:
            return slug_identifier(f"{source}-id", str(source_id))

    if not fallback:
        return None
    return sanitize_identifier(f"{source}-{manual.get('brand', '')}-{manual.get('model', '')}")


def build_upload_metadata(manual: dict) -> dict:
    """
    Build metadata dict for an Internet Archive upload.

    Returns dict with: identifier, title, metadata, local_file, remote_filename
    """
    # Get primary variant for file path, fallback to legacy column
    primary_variant = database.get_primary_variant(manual["id"])
    if primary_variant:
        file_path = primary_variant["file_path"]
    else:
        file_path = manual.get("file_path")

    source = manual.get("source") or "manualslib"
    identifier = build_identifier(manual)
    subjects = [source, "manuals"]

    # Build title: "Brand Model Document Type"
    brand = sanitize_xml_string(manual.get("brand", "Unknown"))
    model = sanitize_xml_string(manual.get("model", ""))
    doc_type = sanitize_xml_string(manual.get("doc_type", "Manual"))

    # Clean up model field - remove brand prefix if present
    if model.lower().startswith(brand.lower()):
        model = model[len(brand):].strip()

    # Don't append doc_type if model already contains it
    if doc_type.lower() in model.lower():
        title = f"{brand} {model}".strip()
    else:
        title = f"{brand} {model} {doc_type}".strip()

    # Remote filename: use original_filename, fallback to local filename
    remote_filename = manual.get("original_filename")
    if not remote_filename:
        remote_filename = Path(file_path).name if file_path else "manual.pdf"
    remote_filename = sanitize_xml_string(remote_filename)

    # Build metadata
    git_commit = get_git_commit()
    scanner = "Salli Cat (https://github.com/arclight0/salli-cat)"
    if git_commit:
        scanner += f" @ {git_commit}"

    metadata = {
        "mediatype": "texts",
        "title": title,
        "subject": subjects,
        "scanner": scanner,
    }

    # Add description
    if manual.get("doc_description"):
        metadata["description"] = sanitize_xml_string(manual["doc_description"])

    # Add source URL
    if manual.get("manual_url"):
        metadata["source"] = sanitize_xml_string(manual["manual_url"])

    # Add checksums as external identifiers (searchable on IA)
    # Include checksums from all file variants
    external_ids = []
    variants = database.get_file_variants(manual["id"])
    if variants:
        for v in variants:
            if v["is_primary"]:
                # Primary variant gets plain identifiers
                external_ids.append(f"urn:md5:{v['file_md5']}")
                external_ids.append(f"urn:sha1:{v['file_sha1']}")
            else:
                # Non-primary variants get labeled identifiers only
                external_ids.append(f"urn:{v['variant_type']}-md5:{v['file_md5']}")
                external_ids.append(f"urn:{v['variant_type']}-sha1:{v['file_sha1']}")
    else:
        # Fallback to legacy columns
        if manual.get("file_md5"):
            external_ids.append(f"urn:md5:{manual['file_md5']}")
        if manual.get("file_sha1"):
            external_ids.append(f"urn:sha1:{manual['file_sha1']}")
        # Include original (pre-watermark-strip) checksums if they differ from final
        if manual.get("original_file_md5") and manual.get("original_file_md5") != manual.get("file_md5"):
            external_ids.append(f"urn:original-md5:{manual['original_file_md5']}")
        if manual.get("original_file_sha1") and manual.get("original_file_sha1") != manual.get("file_sha1"):
            external_ids.append(f"urn:original-sha1:{manual['original_file_sha1']}")
    # Deduplicate while preserving order
    external_ids = list(dict.fromkeys(external_ids))
    if external_ids:
        metadata["external-identifier"] = external_ids

    return {
        "identifier": identifier,
        "title": title,
        "metadata": metadata,
        "local_file": file_path,
        "remote_filename": remote_filename,
    }


# IA's S3 endpoint answers "503 SlowDown" when it is busy; the client retries
# those after a pause instead of failing the upload.
UPLOAD_RETRIES = 5
UPLOAD_RETRIES_SLEEP = 30


def has_content_file(item) -> bool:
    """Whether an IA item holds an uploaded file, not just IA's own metadata files."""
    return any(f.get("source") == "original" and f.get("format") != "Metadata" for f in item.files)


def is_rejected_pdf(error: Exception) -> bool:
    """Whether an upload failed because IA's PDF check refused the file."""
    message = str(error)
    return "Uploaded content is unacceptable" in message and "pdf" in message.lower()


def repair_manual_file(file_path: str) -> bool:
    """
    Repair a damaged PDF and make the result the primary file of every manual
    using it, as a `repaired` variant. The damaged file stays as whichever
    variant it was (usually `original`), so its checksums are still recorded.
    """
    from manualslib_scraper import compute_checksums, get_sha1_storage_path

    src = Path(file_path)
    temp_path = src.with_suffix(".repair.tmp")
    if not repair_pdf(src, temp_path):
        return False
    sha1, md5 = compute_checksums(temp_path)
    # Files live at <download_dir>/ab/cd/<sha1>.pdf
    new_path = get_sha1_storage_path(src.parents[2], sha1)
    new_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.replace(new_path)
    size = new_path.stat().st_size

    old_sha1, _ = compute_checksums(src)
    conn = database.get_connection()
    manual_ids = [r[0] for r in conn.execute("SELECT id FROM manuals WHERE file_sha1 = ?", (old_sha1,))]
    conn.close()
    for manual_id in manual_ids:
        database.replace_primary_variant(manual_id, "repaired", str(new_path), sha1, md5, size)
    # Re-repairing a repaired file leaves the earlier attempt unused
    if src != new_path and not database.is_file_referenced(str(src)):
        src.unlink()
    logger.info(f"Repaired {src.name[:12]} -> {sha1[:8]} for {len(manual_ids)} manual(s)")
    return True


def upload_manual_to_ia(manual: dict, verbose: bool = True) -> str | None:
    """
    Upload a manual to Internet Archive.

    Args:
        manual: Database record dict with file_path, doc_description, brand, model, manualslib_id
        verbose: Show the client's per-file progress bar (off when uploading in parallel)

    Returns:
        Archive.org URL if successful, None otherwise
    """
    upload_info = build_upload_metadata(manual)

    file_path = upload_info["local_file"]
    if not file_path or not Path(file_path).exists():
        logger.error(f"File not found: {file_path}")
        return None

    identifier = upload_info["identifier"]
    remote_filename = upload_info["remote_filename"]
    metadata = upload_info["metadata"]

    logger.info(f"Uploading to Internet Archive: {identifier}")
    logger.info(f"  Title: {upload_info['title']}")
    logger.info(f"  Local file: {file_path}")
    logger.info(f"  Remote filename: {remote_filename}")

    # An identifier with characters like ':' or '/' makes the IA client hit the
    # wrong URL, whose error response looks like an existing item - refuse it
    # here rather than marking the manual archived without uploading anything.
    if not IA_IDENTIFIER_RE.match(identifier):
        logger.error(f"Invalid Internet Archive identifier {identifier!r} - skipping")
        return None

    try:
        # Check if item already exists
        item = ia.get_item(identifier)
        if item.item_metadata.get("error"):
            logger.error(f"Error looking up {identifier}: {item.item_metadata['error']}")
            return None
        existing = item.exists
        if existing:
            # A failed upload can leave an item with only IA's own metadata
            # files - fill it in rather than counting it as archived
            if has_content_file(item):
                logger.info(f"Item already exists: https://archive.org/details/{identifier}")
                return f"https://archive.org/details/{identifier}"
            logger.warning(f"Item {identifier} exists but has no file - uploading it")

        def upload(file_path, metadata):
            # files dict maps remote filename -> local path
            return ia.upload(
                identifier,
                files={remote_filename: file_path},
                metadata=metadata,
                verbose=verbose,
                retries=UPLOAD_RETRIES,
                retries_sleep=UPLOAD_RETRIES_SLEEP,
            )

        try:
            result = upload(file_path, metadata)
        except Exception as e:
            if not is_rejected_pdf(e):
                raise
            # IA creates the item before checking the file, so it exists now
            existing = True
            logger.warning(f"Internet Archive rejected {Path(file_path).name} as a damaged PDF - repairing it")
            if not repair_manual_file(file_path):
                return None
            upload_info = build_upload_metadata(manual)
            file_path, metadata = upload_info["local_file"], upload_info["metadata"]
            result = upload(file_path, metadata)

        # Check result
        if result and all(r.status_code == 200 for r in result):
            if existing and metadata.get("external-identifier"):
                # Metadata sent with a PUT into an existing item is ignored;
                # the checksums on it may describe a file that never landed
                ia.modify_metadata(identifier, {"external-identifier": metadata["external-identifier"]})
            archive_url = f"https://archive.org/details/{identifier}"
            logger.info(f"Upload successful: {archive_url}")
            return archive_url
        else:
            logger.error(f"Upload failed: {result}")
            return None

    except Exception as e:
        logger.error(f"Upload error: {e}")
        return None


def get_uploadable_manuals(source: str = None, limit: int = None) -> list[dict]:
    """
    Get manuals that are downloaded but not yet uploaded to Internet Archive.

    Args:
        source: Filter by source (default: manualslib)
        limit: Maximum number to return

    Returns:
        List of manual dicts
    """
    conn = database.get_connection()
    cursor = conn.cursor()

    query = """
        SELECT * FROM manuals
        WHERE downloaded = 1
          AND archived = 0
          AND file_path IS NOT NULL
    """
    params = []

    if source:
        query += " AND source = ?"
        params.append(source)

    query += " ORDER BY brand, model"

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    # One document can be listed under many models (sibling rows sharing an IA
    # identifier). Upload it once - update_archived marks the siblings too.
    manuals = {}
    for row in rows:
        manual = dict(row)
        # Rows without a source ID aren't siblings of anything - keep them all
        manuals.setdefault(build_identifier(manual, fallback=False) or manual["id"], manual)
    manuals = list(manuals.values())
    return manuals[:limit] if limit else manuals


def upload_all_pending(
    source: str = None,
    limit: int = None,
    workers: int = 1,
) -> tuple[int, int]:
    """
    Upload all pending manuals to Internet Archive.

    Args:
        source: Filter by source
        limit: Maximum number to upload
        workers: Number of uploads to run at once. A single upload stream to
            IA is usually far below the line speed, so a few in parallel
            finish the queue several times faster.

    Returns:
        Tuple of (successful_count, failed_count)
    """
    archived, _ = database.sync_sibling_manuals()
    if archived:
        logger.info(f"Marked {archived} manuals as archived (same document as an already-archived manual)")

    manuals = get_uploadable_manuals(source=source, limit=limit)
    total = len(manuals)
    workers = max(1, workers)
    logger.info(f"Found {total} manuals to upload" + (f" ({workers} at a time)" if workers > 1 else ""))

    success = 0
    failed = 0
    lock = threading.Lock()

    def upload_one(manual: dict) -> bool:
        # get_uploadable_manuals yields one manual per IA identifier, so no
        # two workers ever upload to the same item
        archive_url = upload_manual_to_ia(manual, verbose=workers == 1)
        if not archive_url:
            return False
        siblings = database.update_archived(manual["id"], archive_url)
        if siblings:
            logger.info(f"  Also marked {siblings} other model(s) covered by this document as archived")
        return True

    if workers == 1:
        for manual in manuals:
            if upload_one(manual):
                success += 1
            else:
                failed += 1
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(upload_one, m): m for m in manuals}
            for future in as_completed(futures):
                try:
                    ok = future.result()
                except Exception as e:
                    logger.error(f"Upload error for manual {futures[future]['id']}: {e}")
                    ok = False
                with lock:
                    if ok:
                        success += 1
                    else:
                        failed += 1
                    done = success + failed
                if done % 25 == 0 or done == total:
                    logger.info(f"Progress: {done}/{total} (success {success}, failed {failed})")

    logger.info(f"Upload complete. Success: {success}, Failed: {failed}")
    return success, failed


def print_upload_preview(manual: dict):
    """Print a detailed preview of what would be uploaded."""
    upload_info = build_upload_metadata(manual)

    print("=" * 70)
    print(f"Identifier:      {upload_info['identifier']}")
    print(f"Title:           {upload_info['title']}")
    print(f"Local file:      {upload_info['local_file']}")
    print(f"Remote filename: {upload_info['remote_filename']}")
    print(f"URL:             https://archive.org/details/{upload_info['identifier']}")
    print()
    print("Metadata:")
    for key, value in upload_info['metadata'].items():
        if isinstance(value, list):
            print(f"  {key}: {', '.join(value)}")
        else:
            print(f"  {key}: {value}")
    print()


def main():
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )

    parser = argparse.ArgumentParser(description="Upload manuals to Internet Archive")
    parser.add_argument("--source", default=None, help="Source to upload from (default: all sources)")
    parser.add_argument("--limit", type=int, help="Max number to upload")
    parser.add_argument("--dry-run", action="store_true", help="Show detailed preview of what would be uploaded")
    parser.add_argument("--workers", type=int, default=1, help="Uploads to run in parallel (default 1)")
    args = parser.parse_args()

    database.init_db()

    if args.dry_run:
        manuals = get_uploadable_manuals(source=args.source, limit=args.limit)
        print(f"Would upload {len(manuals)} manual(s):\n")
        for m in manuals:
            print_upload_preview(m)
    else:
        success, failed = upload_all_pending(
            source=args.source,
            limit=args.limit,
            workers=args.workers,
        )
        print(f"Done. Success: {success}, Failed: {failed}")


if __name__ == "__main__":
    main()
