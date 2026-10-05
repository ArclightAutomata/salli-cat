#!/usr/bin/env python3
"""Scraper for manualsbase.com TV/monitor manuals."""

import argparse
import hashlib
import logging
import os
import random
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
from dotenv import load_dotenv
from patchright.sync_api import sync_playwright, Page

load_dotenv()

import database
from browser_helper import launch_browser_with_extension, get_extension_path, setup_bandwidth_saving, apply_stealth
from captcha_solver import TwoCaptchaSolver, extract_sitekey_from_page, inject_captcha_response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BASE_URL = "https://www.manualsbase.com"
ARCHIVE_ORG_BASE = "https://archive.org/details/manualsbase-id-"


def check_archive_org(source_id: str) -> tuple[bool, str]:
    """Check if a manual exists on archive.org. Returns (exists, archive_url)."""
    archive_url = f"{ARCHIVE_ORG_BASE}{source_id}"
    try:
        req = urllib.request.Request(archive_url, method='HEAD')
        req.add_header('User-Agent', 'Mozilla/5.0 (compatible; Salli-Cat/1.0)')
        with urllib.request.urlopen(req, timeout=10) as response:
            return True, archive_url
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, archive_url
        logger.warning(f"HTTP error checking archive.org: {e.code}")
        return False, archive_url
    except Exception as e:
        logger.warning(f"Error checking archive.org: {e}")
        return False, archive_url

# Default categories to look for (can be overridden in config.yaml)
DEFAULT_TARGET_CATEGORIES = ["tv", "television", "monitor", "crt", "remote"]

# Global captcha solver (initialized in main if API key available)
captcha_solver: TwoCaptchaSolver | None = None
CAPTCHA_TIMEOUT = 300  # 5 minutes for manual solving


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_config(config: dict, key: str, default=None, namespace: str = "manualsbase"):
    """Get config value with namespace override support.

    Checks namespace-specific value first, falls back to global.
    e.g., get_config(config, "use_proxy") checks manualsbase.use_proxy, then use_proxy
    """
    ns_config = config.get(namespace, {})
    if key in ns_config:
        return ns_config[key]
    return config.get(key, default)


# Global delay settings (updated from config in main())
DELAY_MIN = 2.0
DELAY_MAX = 5.0


def random_delay(min_sec: float = None, max_sec: float = None):
    """Sleep for a random delay. Uses global DELAY_MIN/MAX if not specified."""
    min_sec = min_sec if min_sec is not None else DELAY_MIN
    max_sec = max_sec if max_sec is not None else DELAY_MAX
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def get_sha1_storage_path(download_dir: Path, sha1: str, extension: str = ".pdf") -> Path:
    """Get the trie-based storage path for a file based on its SHA1 hash."""
    if len(sha1) < 4:
        raise ValueError(f"SHA1 hash too short: {sha1}")
    dir1 = sha1[:2]
    dir2 = sha1[2:4]
    filename = sha1 + extension
    return download_dir / dir1 / dir2 / filename


def compute_checksums(file_path: Path) -> tuple[str, str]:
    """Compute SHA1 and MD5 checksums for a file. Returns (sha1, md5)."""
    sha1 = hashlib.sha1()
    md5 = hashlib.md5()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            sha1.update(chunk)
            md5.update(chunk)
    return sha1.hexdigest(), md5.hexdigest()


def extract_manualsbase_id(url: str) -> str | None:
    """Extract an ID from a manualsbase URL.

    Handles both patterns:
    - /manual/454301/tv_mount/sony/... (numeric ID)
    - /manual/lcd-tvs/sony/model-name/ (slug-based, use model as ID)
    """
    # Try numeric ID first
    match = re.search(r'/manual/(\d+)/', url)
    if match:
        return match.group(1)

    # Fall back to extracting model slug as identifier
    # Pattern: /manual/{category}/{brand}/{model}/
    match = re.search(r'/manual/[^/]+/[^/]+/([^/]+)/', url)
    if match:
        return match.group(1)

    return None


def get_target_categories() -> list[str]:
    """Get target categories from config or use defaults."""
    config = load_config()
    return get_config(config, "categories", DEFAULT_TARGET_CATEGORIES)


def matches_target_category(category_name: str) -> bool:
    """Check if a category name matches our target categories."""
    category_lower = category_name.lower()
    for target in get_target_categories():
        if target.lower() in category_lower:
            return True
    return False


def scrape_all_brands(page: Page) -> list[dict]:
    """Scrape the all brands page to get list of brands with their URLs."""
    brands_url = f"{BASE_URL}/brand/allbrands/"
    logger.info(f"Scraping all brands from: {brands_url}")

    page.goto(brands_url, wait_until="domcontentloaded")
    random_delay(1, 2)

    # Wait for content to load
    try:
        page.wait_for_selector('a[href*="/brand/details/"]', timeout=30000)
    except Exception:
        logger.warning("Timeout waiting for brand links")

    # Extract all brand links
    brand_links = page.query_selector_all('a[href*="/brand/details/"]')

    brands = []
    seen_urls = set()

    for link in brand_links:
        href = link.get_attribute("href")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        brand_name = link.inner_text().strip()
        brand_url = href if href.startswith("http") else BASE_URL + href

        # Extract brand ID from URL
        match = re.search(r'/brand/details/(\d+)/([^/]+)/', href)
        if match:
            brand_id = match.group(1)
            brand_slug = match.group(2)
            brands.append({
                "name": brand_name,
                "url": brand_url,
                "id": brand_id,
                "slug": brand_slug,
            })

    logger.info(f"Found {len(brands)} brands")
    return brands


def scrape_brand_categories(page: Page, brand: dict) -> list[dict]:
    """Scrape a brand page to find TV-related categories."""
    logger.info(f"Checking brand: {brand['name']} ({brand['url']})")

    page.goto(brand["url"], wait_until="domcontentloaded")
    random_delay(1, 2)

    # Wait for content
    try:
        page.wait_for_selector('a[href*="/manuals/"]', timeout=15000)
    except Exception:
        logger.debug(f"No category links found for {brand['name']}")
        return []

    # Find "Show all user manuals" links
    show_all_links = page.query_selector_all('a[href*="/manuals/"]')

    matching_categories = []
    seen_urls = set()

    for link in show_all_links:
        href = link.get_attribute("href")
        if not href or href in seen_urls:
            continue

        link_text = link.inner_text().strip()

        # Check if this is a "Show all" link for a target category
        # The link text is like "Show all user manuals Sony from the TV category"
        if "show all" in link_text.lower():
            # Extract category from link text or URL
            # URL pattern: /manuals/{brand-id}/{category-id}/{brand-slug}/{category-slug}/
            match = re.search(r'/manuals/\d+/(\d+)/[^/]+/([^/]+)/', href)
            if match:
                category_id = match.group(1)
                category_slug = match.group(2)
                category_name = category_slug.replace("_", " ").title()

                if matches_target_category(category_name) or matches_target_category(category_slug):
                    category_url = href if href.startswith("http") else BASE_URL + href
                    if category_url not in seen_urls:
                        seen_urls.add(category_url)
                        matching_categories.append({
                            "name": category_name,
                            "url": category_url,
                            "id": category_id,
                            "slug": category_slug,
                            "brand": brand["name"],
                            "brand_id": brand["id"],
                        })
                        logger.info(f"  Found matching category: {category_name}")

    return matching_categories


def scrape_category_manuals(page: Page, category: dict) -> list[dict]:
    """Scrape all manuals from a category page."""
    logger.info(f"Scraping category: {category['brand']} - {category['name']}")

    page.goto(category["url"], wait_until="domcontentloaded")
    random_delay(1, 2)

    # Wait for manual links to appear (JS rendering)
    try:
        page.wait_for_selector('a[href*="/manual/"]', timeout=30000)
        logger.debug("Manual links appeared")
    except Exception:
        logger.warning(f"Timeout waiting for manual links for {category['name']}")

    # Extra wait for JS to finish rendering
    time.sleep(2)

    # Find all manual links - broader selector to catch all patterns
    # Pattern 1: /manual/{id}/... (numeric)
    # Pattern 2: /manual/{category}/{brand}/{model}/
    manual_links = page.query_selector_all('a[href*="/manual/"]')

    manuals = []
    seen_urls = set()

    for link in manual_links:
        href = link.get_attribute("href")
        if not href or href in seen_urls:
            continue

        # Skip download links and non-manual pages
        if "/download/" in href:
            continue
        if "/manuals/" in href:  # This is a category link, not a manual
            continue

        seen_urls.add(href)

        title = link.inner_text().strip()
        if not title or len(title) < 3:
            continue

        manual_url = href if href.startswith("http") else BASE_URL + href

        # Extract manual ID
        manual_id = extract_manualsbase_id(href)

        if manual_id:
            manuals.append({
                "title": title,
                "url": manual_url,
                "id": manual_id,
                "brand": category["brand"],
                "category": category["name"],
            })

    logger.info(f"  Found {len(manuals)} manuals")

    # Debug: if no manuals found, log some info
    if len(manuals) == 0:
        all_links = page.query_selector_all('a[href*="/manual/"]')
        logger.warning(f"  Debug: Found {len(all_links)} raw /manual/ links on page")
        for link in all_links[:5]:
            logger.warning(f"    - {link.get_attribute('href')}")

    return manuals


def add_manual_to_database(manual: dict) -> int | None:
    """Add a manual to the database."""
    # Extract model from title (usually "Brand Model Document Type")
    title = manual.get("title", "")
    brand = manual.get("brand", "Unknown")

    # Try to extract doc_type from title
    doc_type = "User Manual"
    for dt in ["User manual", "Operating instructions", "User guide", "Installation manual", "Quick start guide"]:
        if dt.lower() in title.lower():
            doc_type = dt
            break

    manual_id = database.add_manual(
        brand=brand,
        model=title,
        manual_url=manual["url"],
        source="manualsbase",
        source_id=manual["id"],
        category=manual.get("category", ""),
        doc_type=doc_type,
    )

    # Check if already on archive.org
    if manual_id and manual.get("id"):
        is_archived, archive_url = check_archive_org(manual["id"])
        if is_archived:
            logger.info(f"Already on archive.org: {title}")
            database.update_archived(manual_id, archive_url)

    return manual_id


def wait_for_recaptcha_solved(page: Page, timeout: int = CAPTCHA_TIMEOUT) -> bool:
    """Wait for reCAPTCHA to be solved (manually or via 2captcha)."""
    global captcha_solver

    # Wait for reCAPTCHA iframe to appear
    logger.info("Waiting for reCAPTCHA to load...")
    try:
        page.wait_for_selector('iframe[src*="recaptcha"]', timeout=15000)
        logger.info("reCAPTCHA iframe detected")
    except Exception:
        # Check if button is already enabled (no captcha needed?)
        submit_btn = page.query_selector('input.get-manual-btn:not([disabled])')
        if submit_btn:
            logger.info("No reCAPTCHA found and button already enabled")
            return True
        logger.warning("No reCAPTCHA iframe found, but button is disabled")
        # Continue anyway in case captcha loads later

    # Check if there's a reCAPTCHA on the page
    recaptcha_frame = page.query_selector('iframe[src*="recaptcha"]')
    if not recaptcha_frame:
        logger.debug("No reCAPTCHA found on page")
        return True

    # Wait for reCAPTCHA to fully initialize
    # The iframe needs time to load its content and the checkbox to become interactive
    logger.info("Waiting for reCAPTCHA to fully initialize...")
    try:
        # Wait for the anchor iframe (the one with the checkbox) to have proper dimensions
        page.wait_for_function("""
            () => {
                const iframe = document.querySelector('iframe[src*="recaptcha/api2/anchor"], iframe[src*="recaptcha/enterprise/anchor"]');
                if (!iframe) return false;
                const rect = iframe.getBoundingClientRect();
                return rect.width > 50 && rect.height > 50;
            }
        """, timeout=10000)
        logger.info("reCAPTCHA anchor iframe ready")
    except Exception:
        logger.warning("Timeout waiting for reCAPTCHA to initialize, proceeding anyway")

    # Extra wait for reCAPTCHA JS to fully load
    time.sleep(2)

    # Verify the sitekey is available before proceeding
    sitekey = extract_sitekey_from_page(page)
    if not sitekey:
        logger.warning("Could not extract sitekey, waiting longer...")
        time.sleep(3)
        sitekey = extract_sitekey_from_page(page)

    # Try automatic solving with 2captcha if available
    if captcha_solver and sitekey:
        logger.info(f"Attempting automatic reCAPTCHA solve with 2captcha (sitekey: {sitekey[:20]}...)")
        token = captcha_solver.solve_recaptcha(sitekey, page.url)
        if token:
            logger.info("Got token from 2captcha, injecting response...")
            inject_captcha_response(page, token)
            # Wait for the page to process the token and enable the button
            time.sleep(3)
            # Check if button is now enabled
            submit_btn = page.query_selector('input.get-manual-btn:not([disabled])')
            if submit_btn:
                logger.info("Button enabled after 2captcha solve!")
                return True
            else:
                logger.warning("Button still disabled after token injection, waiting for manual verification...")
        else:
            logger.warning("2captcha failed, falling back to manual solving")
    elif captcha_solver and not sitekey:
        logger.warning("Could not extract sitekey for 2captcha")

    # Fall back to manual solving
    logger.info("Waiting for manual reCAPTCHA solve...")
    print("\n" + "=" * 60)
    print("RECAPTCHA DETECTED - Please solve it in the browser window")
    print("=" * 60 + "\n")

    start_time = time.time()

    while time.time() - start_time < timeout:
        # Check if captcha is solved (response token present)
        try:
            captcha_response = page.evaluate("""
                () => {
                    const response = document.querySelector('[name="g-recaptcha-response"]');
                    return response && response.value.length > 0;
                }
            """)
            if captcha_response:
                logger.info("reCAPTCHA solved")
                return True
        except Exception:
            pass

        # Also check if the submit button is enabled (indicates captcha solved)
        # When enabled, the disabled attribute is removed entirely
        submit_btn = page.query_selector('input.get-manual-btn:not([disabled])')
        if submit_btn:
            logger.info("Submit button enabled - reCAPTCHA appears solved")
            return True

        time.sleep(2)

    logger.warning("reCAPTCHA timeout")
    return False


def download_manual(page: Page, manual: dict, download_dir: Path) -> tuple[str, str, str, int, str] | None:
    """Download a single manual from manualsbase."""
    logger.info(f"Downloading: {manual['title'][:60]}...")

    # Step 1: Navigate to manual page
    page.goto(manual["url"], wait_until="domcontentloaded")
    random_delay(1, 2)

    # Wait for page to fully load
    try:
        page.wait_for_selector('a[href*="/manual/download/"], a.button.red', timeout=15000)
    except Exception:
        logger.warning("Timeout waiting for download button on manual page")

    # Step 2: Find and click the download button to go to download page
    # The button looks like: <a href="/manual/download/..." class="button medium red">
    download_btn = page.query_selector('a[href*="/manual/download/"].button, a[href*="/manual/download/"]')

    if not download_btn:
        logger.warning(f"No download button found for {manual['title']}")
        return None

    download_href = download_btn.get_attribute("href")
    download_page_url = download_href if download_href.startswith("http") else BASE_URL + download_href

    logger.info(f"Navigating to download page: {download_page_url}")
    page.goto(download_page_url, wait_until="domcontentloaded")
    random_delay(1, 2)

    # Step 3: Wait for the download page with reCAPTCHA
    try:
        page.wait_for_selector('.get-manual-btn, iframe[src*="recaptcha"]', timeout=15000)
    except Exception:
        logger.warning("Timeout waiting for download page elements")

    # Step 4: Handle reCAPTCHA
    if not wait_for_recaptcha_solved(page):
        logger.warning("Could not solve reCAPTCHA, skipping")
        return None

    # Step 5: Wait for submit button to be enabled and click it
    try:
        # Wait for button to become enabled (disabled attribute removed)
        page.wait_for_selector('input.get-manual-btn:not([disabled])', timeout=30000)
        logger.info("Download button enabled")
    except Exception:
        logger.warning("Download button did not become enabled")
        return None

    # Default filename
    default_filename = sanitize_filename(manual.get("title", "manual"))[:100] + ".pdf"

    # Step 6: Click download and capture the file
    logger.info("Clicking download button and waiting for file...")
    try:
        with page.expect_download(timeout=60000) as download_info:
            submit_btn = page.query_selector('input.get-manual-btn')
            if submit_btn:
                submit_btn.click()
                logger.info("Clicked submit button, waiting for download...")
            else:
                # Try form submit
                page.click('input[type="submit"].get-manual-btn')
                logger.info("Clicked form submit, waiting for download...")

            download = download_info.value

        logger.info(f"Download captured: {download.suggested_filename}")

        # Get original filename from download
        original_filename = download.suggested_filename or default_filename
        if not original_filename.lower().endswith('.pdf'):
            original_filename += '.pdf'

        # Save to temp file first
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            temp_path = Path(tmp.name)
        download.save_as(temp_path)

        # Compute checksums
        sha1, md5 = compute_checksums(temp_path)
        file_size = temp_path.stat().st_size

        # Move to SHA1-based storage path
        final_path = get_sha1_storage_path(download_dir, sha1)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        if final_path.exists():
            logger.info(f"File already exists at {final_path} (duplicate content)")
            temp_path.unlink()
        else:
            shutil.move(str(temp_path), str(final_path))

        logger.info(f"Downloaded: {final_path} ({file_size} bytes, SHA1: {sha1[:8]}...)")
        logger.info(f"Original filename: {original_filename}")
        return str(final_path), sha1, md5, file_size, original_filename

    except Exception as e:
        logger.error(f"Download failed: {e}")
        return None


def scrape_manualsbase(
    page: Page,
    download_dir: Path,
    download: bool = True,
    limit_brands: int = None,
    specific_brands: list[str] = None,
):
    """Main scraping function for manualsbase."""

    # Step 1: Get all brands
    brands = scrape_all_brands(page)

    if specific_brands:
        # Filter to only matching brand names (case-insensitive)
        specific_brands_lower = [b.lower() for b in specific_brands]
        filtered_brands = [b for b in brands if b["name"].lower() in specific_brands_lower]
        if not filtered_brands:
            logger.warning(f"No brands found matching: {specific_brands}")
        else:
            logger.info(f"Filtered to {len(filtered_brands)} brands: {[b['name'] for b in filtered_brands]}")
        brands = filtered_brands

    if limit_brands:
        brands = brands[:limit_brands]
        logger.info(f"Limited to {limit_brands} brands")

    # Step 2: For each brand, find TV-related categories
    all_categories = []
    for brand in brands:
        categories = scrape_brand_categories(page, brand)
        all_categories.extend(categories)
        random_delay()

    logger.info(f"Found {len(all_categories)} matching categories across all brands")

    # Step 3: For each category, scrape all manuals
    total_manuals = 0
    for category in all_categories:
        manuals = scrape_category_manuals(page, category)

        # Add to database
        for manual in manuals:
            manual_id = add_manual_to_database(manual)
            if manual_id:
                total_manuals += 1

        random_delay()

    logger.info(f"Added {total_manuals} manuals to database")

    if not download:
        logger.info("Scraping complete. Skipping downloads.")
        return

    # Step 4: Download pending manuals
    pending = database.get_undownloaded_manuals(source="manualsbase")
    logger.info(f"Found {len(pending)} manuals to download")

    for manual_record in pending:
        try:
            source_id = manual_record.get("source_id")

            # Check if already archived on archive.org
            if source_id:
                logger.info(f"Checking archive.org for {manual_record['model']} (ID: {source_id})...")
                is_archived, archive_url = check_archive_org(source_id)
                if is_archived:
                    logger.info(f"Already archived: {archive_url}")
                    database.update_archived(manual_record["id"], archive_url)
                    continue

            result = download_manual(
                page,
                {
                    "title": manual_record["model"],
                    "url": manual_record["manual_url"],
                    "id": source_id,
                    "brand": manual_record["brand"],
                },
                download_dir
            )
            if result:
                file_path, sha1, md5, file_size, original_filename = result
                database.update_downloaded(
                    manual_record["id"], file_path, sha1, md5, file_size, original_filename
                )
            random_delay()
        except Exception as e:
            logger.error(f"Error downloading {manual_record['model']}: {e}")
            continue


def main():
    parser = argparse.ArgumentParser(description="Scrape CRT manuals from ManualsBase")
    parser.add_argument("--index-only", action="store_true", help="Only build index, don't download")
    parser.add_argument("--download-only", action="store_true", help="Only download pending manuals")
    parser.add_argument("--limit-brands", type=int, help="Limit number of brands to process")
    parser.add_argument("--brands", nargs="*", help="Specific brand names to scrape (e.g., jvc sony)")
    parser.add_argument("--clear", action="store_true", help="Clear all manualsbase records from database")
    args = parser.parse_args()

    config = load_config()
    download_dir = Path(config.get("download_dir", "./downloads")).resolve()
    download_dir.mkdir(parents=True, exist_ok=True)

    # Set delay values from config
    global DELAY_MIN, DELAY_MAX
    DELAY_MIN = config.get("delay_min", 2.0)
    DELAY_MAX = config.get("delay_max", 5.0)
    logger.info(f"Request delays: {DELAY_MIN}-{DELAY_MAX} seconds")

    database.init_db()

    # Initialize 2captcha solver if API key is available
    global captcha_solver
    twocaptcha_key = os.environ.get("TWOCAPTCHA_API_KEY")
    if twocaptcha_key:
        captcha_solver = TwoCaptchaSolver(twocaptcha_key)
        balance = captcha_solver.get_balance()
        if balance is not None:
            logger.info(f"2captcha enabled (balance: ${balance:.2f})")
        else:
            logger.warning("2captcha API key set but could not get balance")
    else:
        logger.info("2captcha not configured - will use manual captcha solving")

    if args.clear:
        logger.info("Clearing all manualsbase records from database...")
        database.clear_manuals_by_source("manualsbase")
        logger.info("ManualsBase records cleared.")

    # Get browser settings from config (with namespace override support)
    browser_type = get_config(config, "browser", "chromium")
    headless = get_config(config, "headless", False)
    use_stealth = get_config(config, "stealth", False)
    use_proxy = get_config(config, "use_proxy", False)

    # Get extension path
    project_dir = Path(__file__).parent
    extension_path = get_extension_path(config, project_dir)

    with sync_playwright() as p:
        context, extension_loaded = launch_browser_with_extension(
            p,
            extension_path=extension_path,
            headless=headless,
            browser=browser_type,
            use_proxy=use_proxy,
        )

        if context.pages:
            page = context.pages[0]
        else:
            page = context.new_page()

        if use_stealth:
            apply_stealth(page)

        if not extension_loaded:
            setup_bandwidth_saving(page)

        try:
            if args.download_only:
                # Only download pending manuals
                pending = database.get_undownloaded_manuals(source="manualsbase")
                logger.info(f"Found {len(pending)} pending manualsbase downloads")

                for manual_record in pending:
                    try:
                        source_id = manual_record.get("source_id")

                        # Check if already archived on archive.org
                        if source_id:
                            logger.info(f"Checking archive.org for {manual_record['model']} (ID: {source_id})...")
                            is_archived, archive_url = check_archive_org(source_id)
                            if is_archived:
                                logger.info(f"Already archived: {archive_url}")
                                database.update_archived(manual_record["id"], archive_url)
                                continue

                        result = download_manual(
                            page,
                            {
                                "title": manual_record["model"],
                                "url": manual_record["manual_url"],
                                "id": source_id,
                                "brand": manual_record["brand"],
                            },
                            download_dir
                        )
                        if result:
                            file_path, sha1, md5, file_size, original_filename = result
                            database.update_downloaded(
                                manual_record["id"], file_path, sha1, md5, file_size, original_filename
                            )
                        random_delay()
                    except Exception as e:
                        logger.error(f"Error downloading {manual_record['model']}: {e}")
            else:
                scrape_manualsbase(
                    page,
                    download_dir,
                    download=not args.index_only,
                    limit_brands=args.limit_brands,
                    specific_brands=args.brands,
                )
        finally:
            context.close()

    stats = database.get_stats(source="manualsbase")
    logger.info(f"ManualsBase scraping complete. Total: {stats['total']}, Downloaded: {stats['downloaded']}, Archived: {stats['archived']}, Pending: {stats['pending']}, Pending archive: {stats['pending_archive']}")


if __name__ == "__main__":
    main()
