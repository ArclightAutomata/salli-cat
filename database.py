import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "manuals.db"


def get_connection():
    # timeout + WAL let multiple scraper processes share the DB without
    # "database is locked" errors (WAL readers don't block the writer)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # Brands table - discovered brands with TV category
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS brands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            brand_url TEXT,
            tv_categories TEXT,
            tv_category_urls TEXT,
            all_categories TEXT,
            scraped INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migration for brands table
    for col, coltype in [
        ("tv_categories", "TEXT"),
        ("tv_category_urls", "TEXT"),
        ("all_categories", "TEXT"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE brands ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass

    # Manuals table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS manuals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand TEXT NOT NULL,
            model TEXT NOT NULL,
            model_url TEXT,
            model_id TEXT,
            doc_type TEXT,
            doc_description TEXT,
            manual_url TEXT UNIQUE NOT NULL,
            manualslib_id TEXT,
            downloaded INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0,
            file_path TEXT,
            archive_url TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migration: add columns if they don't exist (for existing databases)
    for col, coltype in [
        ("manualslib_id", "TEXT"),
        ("archived", "INTEGER DEFAULT 0"),
        ("archive_url", "TEXT"),
        ("model_url", "TEXT"),
        ("model_id", "TEXT"),
        ("doc_description", "TEXT"),
        ("source", "TEXT DEFAULT 'manualslib'"),
        ("source_id", "TEXT"),
        ("category", "TEXT"),
        ("file_sha1", "TEXT"),
        ("file_md5", "TEXT"),
        ("file_size", "INTEGER"),
        ("original_file_sha1", "TEXT"),
        ("original_file_md5", "TEXT"),
        ("scraped_at", "TEXT"),
        ("downloaded_at", "TEXT"),
        ("archive_checked_at", "TEXT"),
        ("original_filename", "TEXT"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE manuals ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Create indexes (after migrations so columns exist)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_brand ON manuals(brand)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloaded ON manuals(downloaded)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_archived ON manuals(archived)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_source ON manuals(source)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_brands_slug ON brands(slug)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_brands_scraped ON brands(scraped)")

    # File variants table - multiple versions of each file (original, stripped, etc.)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manual_id INTEGER NOT NULL,
            variant_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_sha1 TEXT NOT NULL,
            file_md5 TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            is_primary INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (manual_id) REFERENCES manuals(id)
        )
    """)
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_file_variants_manual_type ON file_variants(manual_id, variant_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_variants_manual_id ON file_variants(manual_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_variants_sha1 ON file_variants(file_sha1)")

    conn.commit()
    conn.close()

    # Migrate existing data to file_variants table
    _migrate_to_file_variants()


def _migrate_to_file_variants():
    """Migrate existing file_* columns to file_variants table (runs once on init)."""
    conn = get_connection()
    cursor = conn.cursor()

    # Get all downloaded manuals that don't have variants yet
    cursor.execute("""
        SELECT m.id, m.file_path, m.file_sha1, m.file_md5, m.file_size,
               m.original_file_sha1, m.original_file_md5
        FROM manuals m
        WHERE m.downloaded = 1 AND m.file_path IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM file_variants fv WHERE fv.manual_id = m.id)
    """)

    migrated = 0
    for row in cursor.fetchall():
        manual_id = row[0]
        file_path = row[1]
        file_sha1 = row[2]
        file_md5 = row[3]
        file_size = row[4]
        original_sha1 = row[5]
        original_md5 = row[6]

        if not file_sha1 or not file_md5 or not file_size:
            continue

        # Determine variant type based on whether original differs from final
        if original_sha1 and original_sha1 != file_sha1:
            # The stored file is the stripped version
            variant_type = "stripped"
        else:
            # Only original exists (no stripping or stripping didn't change file)
            variant_type = "original"

        try:
            cursor.execute("""
                INSERT INTO file_variants (manual_id, variant_type, file_path, file_sha1, file_md5, file_size, is_primary)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (manual_id, variant_type, file_path, file_sha1, file_md5, file_size))
            migrated += 1
        except sqlite3.IntegrityError:
            pass  # Already exists

    conn.commit()
    conn.close()

    if migrated > 0:
        print(f"Migrated {migrated} manuals to file_variants table")


# File variant functions

def add_file_variant(
    manual_id: int,
    variant_type: str,
    file_path: str,
    file_sha1: str,
    file_md5: str,
    file_size: int,
    is_primary: bool = False,
) -> int | None:
    """Add a file variant for a manual. Returns variant id or None if already exists."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO file_variants (manual_id, variant_type, file_path, file_sha1, file_md5, file_size, is_primary)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (manual_id, variant_type, file_path, file_sha1, file_md5, file_size, 1 if is_primary else 0))
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_file_variants(manual_id: int) -> list[dict]:
    """Get all file variants for a manual."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM file_variants
        WHERE manual_id = ?
        ORDER BY is_primary DESC, variant_type
    """, (manual_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_primary_variant(manual_id: int) -> dict | None:
    """Get the primary file variant for a manual."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM file_variants
        WHERE manual_id = ? AND is_primary = 1
    """, (manual_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_variant_by_type(manual_id: int, variant_type: str) -> dict | None:
    """Get a specific variant type for a manual."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM file_variants
        WHERE manual_id = ? AND variant_type = ?
    """, (manual_id, variant_type))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def set_primary_variant(manual_id: int, variant_type: str) -> bool:
    """Set which variant is primary for a manual. Returns True if successful."""
    conn = get_connection()
    cursor = conn.cursor()
    # Clear existing primary
    cursor.execute("UPDATE file_variants SET is_primary = 0 WHERE manual_id = ?", (manual_id,))
    # Set new primary
    cursor.execute("""
        UPDATE file_variants SET is_primary = 1
        WHERE manual_id = ? AND variant_type = ?
    """, (manual_id, variant_type))
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success


def get_variant_stats() -> dict:
    """Get statistics about file variants."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT variant_type, COUNT(*) as count
        FROM file_variants
        GROUP BY variant_type
    """)
    by_type = {row[0]: row[1] for row in cursor.fetchall()}
    cursor.execute("SELECT COUNT(*) FROM file_variants")
    total = cursor.fetchone()[0]
    conn.close()
    return {"total": total, "by_type": by_type}


def add_manual(
    brand: str,
    model: str,
    manual_url: str,
    manualslib_id: str = None,
    model_url: str = None,
    model_id: str = None,
    doc_type: str = None,
    doc_description: str = None,
    source: str = "manualslib",
    source_id: str = None,
    category: str = None,
) -> int | None:
    conn = get_connection()
    cursor = conn.cursor()
    scraped_at = datetime.now().isoformat()
    try:
        cursor.execute("""
            INSERT INTO manuals (brand, model, model_url, model_id, doc_type, doc_description, manual_url, manualslib_id, source, source_id, category, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (brand, model, model_url, model_id, doc_type, doc_description, manual_url, manualslib_id, source, source_id, category, scraped_at))
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        # Already exists
        return None
    finally:
        conn.close()


def get_manual_by_url(manual_url: str) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM manuals WHERE manual_url = ?", (manual_url,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


# Brand management functions

def add_brand(
    name: str,
    slug: str,
    brand_url: str = None,
    tv_categories: str = None,
    tv_category_urls: str = None,
    all_categories: str = None,
) -> int | None:
    """Add a discovered brand to the database. Returns id if new, None if exists."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO brands (name, slug, brand_url, tv_categories, tv_category_urls, all_categories)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (name, slug, brand_url, tv_categories, tv_category_urls, all_categories))
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        # Already exists
        return None
    finally:
        conn.close()


def get_all_brands(scraped: bool = None) -> list[dict]:
    """Get all discovered brands, optionally filtered by scraped status."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM brands WHERE 1=1"
    params = []

    if scraped is not None:
        query += " AND scraped = ?"
        params.append(1 if scraped else 0)

    query += " ORDER BY name"

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_unscraped_brands() -> list[dict]:
    """Get brands that haven't been scraped yet."""
    return get_all_brands(scraped=False)


def mark_brand_scraped(brand_id: int):
    """Mark a brand as scraped."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE brands SET scraped = 1 WHERE id = ?", (brand_id,))
    conn.commit()
    conn.close()


def get_brand_by_slug(slug: str) -> dict | None:
    """Get a brand by its slug."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM brands WHERE slug = ?", (slug,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_brand_stats() -> dict:
    """Get statistics about discovered brands."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as total FROM brands")
    total = cursor.fetchone()["total"]

    cursor.execute("SELECT COUNT(*) as scraped FROM brands WHERE scraped = 1")
    scraped = cursor.fetchone()["scraped"]

    conn.close()

    return {
        "total": total,
        "scraped": scraped,
        "pending": total - scraped
    }


def update_downloaded(
    manual_id: int,
    file_path: str,
    file_sha1: str = None,
    file_md5: str = None,
    file_size: int = None,
    original_filename: str = None,
    original_file_sha1: str = None,
    original_file_md5: str = None,
    original_file_path: str = None,
    original_file_size: int = None,
):
    """Update downloaded status and add file variants."""
    conn = get_connection()
    cursor = conn.cursor()
    downloaded_at = datetime.now().isoformat()

    # Update manuals table (backward compatibility)
    cursor.execute("""
        UPDATE manuals
        SET downloaded = 1, file_path = ?, file_sha1 = ?, file_md5 = ?, file_size = ?,
            downloaded_at = ?, original_filename = ?, original_file_sha1 = ?, original_file_md5 = ?
        WHERE id = ?
    """, (file_path, file_sha1, file_md5, file_size, downloaded_at, original_filename,
          original_file_sha1, original_file_md5, manual_id))

    # Add file variants
    if file_sha1 and file_md5 and file_size:
        # Check if we have both original and stripped (different checksums)
        if original_file_sha1 and original_file_sha1 != file_sha1 and original_file_path:
            # Add original variant
            try:
                cursor.execute("""
                    INSERT OR IGNORE INTO file_variants
                    (manual_id, variant_type, file_path, file_sha1, file_md5, file_size, is_primary)
                    VALUES (?, 'original', ?, ?, ?, ?, 0)
                """, (manual_id, original_file_path, original_file_sha1, original_file_md5,
                      original_file_size or file_size))
            except sqlite3.IntegrityError:
                pass

            # Add stripped variant (primary)
            try:
                cursor.execute("""
                    INSERT OR IGNORE INTO file_variants
                    (manual_id, variant_type, file_path, file_sha1, file_md5, file_size, is_primary)
                    VALUES (?, 'stripped', ?, ?, ?, ?, 1)
                """, (manual_id, file_path, file_sha1, file_md5, file_size))
            except sqlite3.IntegrityError:
                pass
        else:
            # Only one variant (original = final, or no stripping)
            try:
                cursor.execute("""
                    INSERT OR IGNORE INTO file_variants
                    (manual_id, variant_type, file_path, file_sha1, file_md5, file_size, is_primary)
                    VALUES (?, 'original', ?, ?, ?, ?, 1)
                """, (manual_id, file_path, file_sha1, file_md5, file_size))
            except sqlite3.IntegrityError:
                pass

    conn.commit()
    conn.close()


def update_archived(manual_id: int, archive_url: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE manuals
        SET archived = 1, archive_url = ?
        WHERE id = ?
    """, (archive_url, manual_id))
    conn.commit()
    conn.close()


def update_manualslib_id(manual_id: int, manualslib_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE manuals
        SET manualslib_id = ?
        WHERE id = ?
    """, (manualslib_id, manual_id))
    conn.commit()
    conn.close()


def get_manuals_needing_archive_check(limit: int = 100) -> list[dict]:
    """Get manuals that haven't been checked on archive.org recently.

    Returns manuals where:
    - Has an ID we can check (manualslib_id or source_id)
    - archived = 0 (not already marked as archived)
    - downloaded = 0 (not already downloaded locally)
    - archive_checked_at is NULL or older than 7 days
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM manuals
        WHERE (manualslib_id IS NOT NULL OR source_id IS NOT NULL)
          AND archived = 0
          AND downloaded = 0
          AND (archive_checked_at IS NULL
               OR datetime(archive_checked_at) < datetime('now', '-7 days'))
        ORDER BY archive_checked_at ASC NULLS FIRST
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def update_archive_checked(manual_id: int, is_archived: bool, archive_url: str = None):
    """Update the archive check status for a manual."""
    conn = get_connection()
    cursor = conn.cursor()
    checked_at = datetime.now().isoformat()
    if is_archived and archive_url:
        cursor.execute("""
            UPDATE manuals
            SET archived = 1, archive_url = ?, archive_checked_at = ?
            WHERE id = ?
        """, (archive_url, checked_at, manual_id))
    else:
        cursor.execute("""
            UPDATE manuals
            SET archive_checked_at = ?
            WHERE id = ?
        """, (checked_at, manual_id))
    conn.commit()
    conn.close()


def get_archive_check_stats() -> dict:
    """Get statistics about archive checking progress."""
    conn = get_connection()
    cursor = conn.cursor()

    # Total with a checkable ID (manualslib_id or source_id)
    cursor.execute("""
        SELECT COUNT(*) FROM manuals
        WHERE manualslib_id IS NOT NULL OR source_id IS NOT NULL
    """)
    total_checkable = cursor.fetchone()[0]

    # Already archived
    cursor.execute("SELECT COUNT(*) FROM manuals WHERE archived = 1")
    archived = cursor.fetchone()[0]

    # Checked but not archived
    cursor.execute("""
        SELECT COUNT(*) FROM manuals
        WHERE archive_checked_at IS NOT NULL AND archived = 0
    """)
    checked_not_archived = cursor.fetchone()[0]

    # Never checked
    cursor.execute("""
        SELECT COUNT(*) FROM manuals
        WHERE (manualslib_id IS NOT NULL OR source_id IS NOT NULL)
          AND archive_checked_at IS NULL
          AND archived = 0
    """)
    never_checked = cursor.fetchone()[0]

    conn.close()

    return {
        "total_checkable": total_checkable,
        "archived": archived,
        "checked_not_archived": checked_not_archived,
        "never_checked": never_checked,
    }


def get_all_manuals(brand: str = None, downloaded: bool = None, source: str = None) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM manuals WHERE 1=1"
    params = []

    if brand:
        query += " AND brand = ?"
        params.append(brand)

    if downloaded is not None:
        query += " AND downloaded = ?"
        params.append(1 if downloaded else 0)

    if source:
        query += " AND source = ?"
        params.append(source)

    query += " ORDER BY brand, model"

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_undownloaded_manuals(brand: str = None, include_archived: bool = False, source: str = None) -> list[dict]:
    """Get manuals that haven't been downloaded. By default excludes archived manuals."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM manuals WHERE downloaded = 0"
    params = []

    if not include_archived:
        query += " AND archived = 0"

    if brand:
        query += " AND brand = ?"
        params.append(brand)

    if source:
        query += " AND source = ?"
        params.append(source)

    query += " ORDER BY brand, model"

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_stats(source: str = None) -> dict:
    conn = get_connection()
    cursor = conn.cursor()

    source_filter = ""
    params = []
    if source:
        source_filter = " WHERE source = ?"
        params = [source]

    cursor.execute(f"SELECT COUNT(*) as total FROM manuals{source_filter}", params)
    total = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) as downloaded FROM manuals WHERE downloaded = 1{' AND source = ?' if source else ''}", params)
    downloaded = cursor.fetchone()["downloaded"]

    cursor.execute(f"SELECT COUNT(*) as archived FROM manuals WHERE archived = 1{' AND source = ?' if source else ''}", params)
    archived = cursor.fetchone()["archived"]

    by_brand_query = """
        SELECT brand,
               COUNT(*) as total,
               SUM(CASE WHEN downloaded = 1 THEN 1 ELSE 0 END) as downloaded,
               SUM(CASE WHEN archived = 1 THEN 1 ELSE 0 END) as archived
        FROM manuals
    """
    if source:
        by_brand_query += " WHERE source = ?"
    by_brand_query += " GROUP BY brand ORDER BY brand"

    cursor.execute(by_brand_query, params)
    by_brand = [dict(row) for row in cursor.fetchall()]

    # Also get stats by source
    cursor.execute("""
        SELECT COALESCE(source, 'manualslib') as source,
               COUNT(*) as total,
               SUM(CASE WHEN downloaded = 1 THEN 1 ELSE 0 END) as downloaded,
               SUM(CASE WHEN archived = 1 THEN 1 ELSE 0 END) as archived
        FROM manuals
        GROUP BY source
        ORDER BY source
    """)
    by_source = [dict(row) for row in cursor.fetchall()]

    conn.close()

    return {
        "total": total,
        "downloaded": downloaded,
        "archived": archived,
        "pending": total - downloaded - archived,
        "by_brand": by_brand,
        "by_source": by_source,
    }


def clear_all():
    """Delete all records from the manuals table."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM manuals")
    conn.commit()
    conn.close()


def clear_manuals_by_source(source: str):
    """Delete all manuals from a specific source."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM manuals WHERE source = ?", (source,))
    conn.commit()
    conn.close()


def clear_brands():
    """Delete all records from the brands table."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM brands")
    conn.commit()
    conn.close()


def clear_everything():
    """Delete all records from both manuals and brands tables."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM manuals")
    cursor.execute("DELETE FROM brands")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
