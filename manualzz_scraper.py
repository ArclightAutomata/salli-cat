#!/usr/bin/env python3
"""Scraper for manualzz.com CRT manuals."""

import argparse
import hashlib
import logging
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
from playwright.sync_api import sync_playwright, Page

import database
from browser_helper import launch_browser_with_extension, get_extension_path, setup_route_ad_blocking, apply_stealth
from turnstile_solver import is_solver_available, solve_cloudflare_with_api, SOLVER_API_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BASE_URL = "https://manualzz.com"
CAPTCHA_TIMEOUT = 300  # 5 minutes to solve captcha


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_config(config: dict, key: str, default=None, namespace: str = "manualzz"):
    """Get config value with namespace override support.

    Checks namespace-specific value first, falls back to global.
    e.g., get_config(config, "use_proxy") checks manualzz.use_proxy, then use_proxy
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
    """
    Get the trie-based storage path for a file based on its SHA1 hash.

    Uses git-style object storage: ab/cd/abcdef1234...
    First 2 chars as first directory, next 2 as second directory, full hash as filename.
    """
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


def extract_manualzz_id(url: str) -> str | None:
    """Extract the numeric ID from a manualzz URL like /doc/12345/..."""
    match = re.search(r'/doc/(\d+)', url)
    if match:
        return match.group(1)
    # Also try download URL format
    match = re.search(r'/download/(\d+)', url)
    return match.group(1) if match else None


def extract_category_from_url(url: str) -> str:
    """Extract category name from catalog URL."""
    # URL like: /catalog/computers+%26+electronics/TVs+%26+monitors/CRT+TVs
    parts = url.rstrip('/').split('/')
    if parts:
        return urllib.parse.unquote_plus(parts[-1])
    return "Unknown"


def scrape_catalog_page(page: Page, catalog_url: str) -> int:
    """Scrape all manual listings from a manualzz catalog page (with pagination).

    Adds manuals to database immediately as they're found for real-time progress.
    Returns the count of manuals found.
    """
    seen_urls = set()
    category = extract_category_from_url(catalog_url)
    page_num = 1
    manual_count = 0

    current_url = catalog_url

    while current_url:
        logger.info(f"Scraping catalog page {page_num}: {current_url}")
        page.goto(current_url, wait_until="domcontentloaded")
        random_delay(1, 2)

        # Check for Cloudflare challenge after navigation
        if check_cloudflare_challenge(page):
            if not wait_for_cloudflare_solved(page):
                logger.error("Could not pass Cloudflare challenge, stopping")
                return manual_count
            # Re-navigate after solving challenge
            page.goto(current_url, wait_until="domcontentloaded")
            random_delay(1, 2)

        # Wait for page content to be rendered by JS
        try:
            page.wait_for_selector(
                '.media, .media-similar, a[href*="/doc/"], .container, .catalog-list',
                timeout=45000
            )
            logger.info("Page content rendered")
        except Exception:
            logger.warning("Timeout waiting for page content selectors, will try anyway...")
            # Give extra time for slow JS
            time.sleep(3)

        # Find all manual/document listings
        # Structure: .media.media-similar contains a.media-link and .media-body with h4
        media_containers = page.query_selector_all('.media.media-similar, .media-similar')

        logger.info(f"Found {len(media_containers)} media containers")

        # Fallback: also try direct link selection if no containers found
        if not media_containers:
            doc_links = page.query_selector_all('a[href*="/doc/"]')
            logger.info(f"Fallback: found {len(doc_links)} direct doc links")
        else:
            doc_links = []

        # Helper function to add a manual to database
        def add_manual_to_db(manual_url: str, title: str):
            nonlocal manual_count

            # Extract manualzz ID
            manualzz_id = extract_manualzz_id(manual_url)

            # Try to extract brand from title (first word often is brand)
            brand = "Unknown"
            title_parts = title.split()
            if title_parts:
                brand = title_parts[0]

            # Add to database immediately for real-time progress
            manual_id = database.add_manual(
                brand=brand,
                model=title,  # Use title as model for manualzz
                manual_url=manual_url,
                source="manualzz",
                source_id=manualzz_id,
                category=category,
            )
            if manual_id:
                logger.info(f"Added: {title[:50]}...")
            manual_count += 1

        # Process media containers first (preferred method)
        for container in media_containers:
            link = container.query_selector('a.media-link, a[href*="/doc/"]')
            if not link:
                continue

            href = link.get_attribute("href")
            if not href:
                continue

            manual_url = href if href.startswith("http") else BASE_URL + href

            # Skip if already seen
            if manual_url in seen_urls:
                continue
            seen_urls.add(manual_url)

            # Get title from h4 in media-body
            title_elem = container.query_selector('h4, .media-heading h4, .media-body h4')
            title = title_elem.inner_text().strip() if title_elem else ""

            if not title:
                title = link.get_attribute("title") or "Unknown"

            add_manual_to_db(manual_url, title)

        # Process fallback direct links
        for link in doc_links:
            href = link.get_attribute("href")
            if not href:
                continue

            manual_url = href if href.startswith("http") else BASE_URL + href

            # Skip if already seen
            if manual_url in seen_urls:
                continue
            seen_urls.add(manual_url)

            # Try to get title from the link or nearby elements
            title = link.get_attribute("title") or link.inner_text().strip()

            # Clean up title
            if not title or len(title) < 3:
                # Try to find title in parent container
                parent = link.query_selector('xpath=..')
                if parent:
                    title_elem = parent.query_selector('h3, h4, .title, span')
                    if title_elem:
                        title = title_elem.inner_text().strip()

            if not title:
                title = "Unknown"

            add_manual_to_db(manual_url, title)

        # Check for next page in pagination
        # Look for pagination links at bottom
        next_link = page.query_selector('a.next, a[rel="next"], .pagination a:has-text("Next"), .pagination a:has-text(">")')
        if next_link:
            next_href = next_link.get_attribute("href")
            if next_href and next_href not in seen_urls:
                current_url = next_href if next_href.startswith("http") else BASE_URL + next_href
                page_num += 1
                random_delay()
            else:
                current_url = None
        else:
            # Try numbered pagination - look for next page number
            current_page_elem = page.query_selector('.pagination .active, .pagination .current')
            if current_page_elem:
                try:
                    current_page_num = int(current_page_elem.inner_text().strip())
                    next_page_link = page.query_selector(f'.pagination a:has-text("{current_page_num + 1}")')
                    if next_page_link:
                        next_href = next_page_link.get_attribute("href")
                        if next_href:
                            current_url = next_href if next_href.startswith("http") else BASE_URL + next_href
                            page_num += 1
                            random_delay()
                        else:
                            current_url = None
                    else:
                        current_url = None
                except (ValueError, TypeError):
                    current_url = None
            else:
                current_url = None

    logger.info(f"Found {manual_count} manuals in catalog")

    # Pause for debugging if no manuals found
    if manual_count == 0:
        print("\n" + "=" * 60)
        print("WARNING: No manuals found on this page!")
        print("Browser will stay open for you to inspect the HTML.")
        print("Press Enter to continue to the next catalog...")
        print("=" * 60)
        input()

    return manual_count


def check_cloudflare_challenge(page: Page) -> bool:
    """Check if Cloudflare challenge/hCaptcha is present on the page."""
    # Check for hCaptcha iframe
    hcaptcha_frame = page.query_selector('iframe[src*="hcaptcha.com"], iframe[src*="hcaptcha"]')
    if hcaptcha_frame:
        return True

    # Check for Cloudflare challenge page indicators
    cf_challenge = page.query_selector('#cf-challenge-running, .cf-browser-verification, #challenge-running')
    if cf_challenge:
        return True

    # Check page title for Cloudflare
    try:
        title = page.title()
        if 'just a moment' in title.lower() or 'cloudflare' in title.lower():
            return True
    except Exception:
        pass

    # Check for challenge form
    challenge_form = page.query_selector('form#challenge-form, form[action*="challenge"]')
    if challenge_form:
        return True

    return False


def wait_for_cloudflare_solved(page: Page, timeout: int = CAPTCHA_TIMEOUT, use_solver: bool = True) -> bool:
    """Wait for Cloudflare challenge/hCaptcha to be solved. Returns True if solved, False if timeout."""
    logger.info("Cloudflare challenge detected...")

    # Try automatic solver first if available
    if use_solver and is_solver_available():
        logger.info("Turnstile solver API available, attempting automatic solve...")
        print("\n" + "=" * 60)
        print("CLOUDFLARE DETECTED - Attempting automatic solve via Turnstile API...")
        print("=" * 60 + "\n")

        if solve_cloudflare_with_api(page, timeout=60):
            # Wait a moment and check if challenge is gone
            time.sleep(3)
            if not check_cloudflare_challenge(page):
                logger.info("Cloudflare challenge solved automatically!")
                # Wait for page content
                try:
                    page.wait_for_selector(
                        '.media, .container, nav, header, .content, .catalog, '
                        'a[href*="/doc/"], .media-similar',
                        timeout=45000
                    )
                    logger.info("Page content loaded")
                    time.sleep(3)
                    return True
                except Exception:
                    time.sleep(3)
                    return True
        else:
            logger.warning("Automatic solve failed, falling back to manual...")
    elif use_solver:
        logger.info("Turnstile solver API not available at " + SOLVER_API_URL)
        logger.info("Start the solver with: cd turnstile-solver && python api_solver.py")

    # Fall back to manual solving
    print("\n" + "=" * 60)
    print("CLOUDFLARE/HCAPTCHA DETECTED - Please solve it in the browser window")
    print("=" * 60 + "\n")

    start_time = time.time()

    while time.time() - start_time < timeout:
        # Check if we're still on a challenge page
        if not check_cloudflare_challenge(page):
            logger.info("Cloudflare challenge appears to be solved, waiting for page content...")

            # Wait for actual page content to load (not just challenge gone)
            # Look for elements that indicate the real Manualzz page has rendered
            try:
                # Wait for any of these indicators that the real page loaded
                page.wait_for_selector(
                    'body:not(:has(title:has-text("Just a moment"))), '
                    '.media, .container, nav, header, .content, .catalog, '
                    'a[href*="/doc/"], .media-similar',
                    timeout=45000
                )
                logger.info("Page content loaded")
                # Extra wait for JS rendering
                time.sleep(3)
                return True
            except Exception as e:
                logger.warning(f"Timeout waiting for page content: {e}")
                # Still return True since challenge is solved, page might just be slow
                time.sleep(3)
                return True

        time.sleep(2)

    logger.warning("Cloudflare challenge timeout")
    return False


def wait_for_hcaptcha_solved(page: Page, timeout: int = CAPTCHA_TIMEOUT) -> bool:
    """Wait for human to solve hCaptcha. Returns True if solved, False if timeout."""
    logger.info("Waiting for hCaptcha to be solved...")
    print("\n" + "=" * 60)
    print("HCAPTCHA DETECTED - Please solve it in the browser window")
    print("=" * 60 + "\n")

    start_time = time.time()

    while time.time() - start_time < timeout:
        # Check if hCaptcha iframe is still present
        hcaptcha_frame = page.query_selector('iframe[src*="hcaptcha.com"]')
        if not hcaptcha_frame:
            logger.info("hCaptcha appears to be solved (iframe gone)")
            return True

        # Check if hCaptcha has a response token
        try:
            hcaptcha_response = page.evaluate("""
                () => {
                    const response = document.querySelector('[name="h-captcha-response"], [name="g-recaptcha-response"]');
                    return response && response.value.length > 0;
                }
            """)
            if hcaptcha_response:
                logger.info("hCaptcha solved (response token present)")
                return True
        except Exception:
            pass

        time.sleep(2)

    logger.warning("hCaptcha timeout - skipping this manual")
    return False


def wait_for_captcha_solved(page: Page, timeout: int = CAPTCHA_TIMEOUT) -> bool:
    """Wait for human to solve captcha. Returns True if solved, False if timeout."""
    logger.info("Waiting for captcha to be solved...")
    print("\n" + "=" * 60)
    print("CAPTCHA DETECTED - Please solve it in the browser window")
    print("=" * 60 + "\n")

    start_time = time.time()

    while time.time() - start_time < timeout:
        # Check if captcha iframe is still present and visible
        captcha_frame = page.query_selector('iframe[src*="recaptcha"]')
        if not captcha_frame:
            logger.info("Captcha appears to be solved (iframe gone)")
            return True

        # Check if captcha is in solved state
        try:
            captcha_response = page.evaluate("""
                () => {
                    const response = document.querySelector('[name="g-recaptcha-response"]');
                    return response && response.value.length > 0;
                }
            """)
            if captcha_response:
                logger.info("Captcha solved (response token present)")
                return True
        except Exception:
            pass

        time.sleep(2)

    logger.warning("Captcha timeout - skipping this manual")
    return False


def download_manual(page: Page, manual: dict, download_dir: Path) -> tuple[str, str, str, int, str] | None:
    """
    Download a single manual from manualzz using content-addressable storage.

    Files are stored based on SHA1 hash in a trie structure: downloads/ab/cd/abcdef...pdf
    The original filename is preserved in the database for display purposes.

    Returns (file_path, sha1, md5, file_size, original_filename) if successful, None otherwise.
    """
    logger.info(f"Downloading: {manual['title']} - {manual['manual_url']}")

    page.goto(manual["manual_url"], wait_until="domcontentloaded")
    random_delay(1, 2)

    # Check for Cloudflare challenge after navigation
    if check_cloudflare_challenge(page):
        if not wait_for_cloudflare_solved(page):
            logger.warning("Could not pass Cloudflare challenge, skipping")
            return None
        # Re-navigate after solving challenge
        page.goto(manual["manual_url"], wait_until="domcontentloaded")
        random_delay(1, 2)

    # Wait for download button to appear (JS rendering)
    download_btn_selector = "[title='Download PDF'], a.bi-download, button.bi-download, [class*='bi-download'], a:has-text('Download')"
    try:
        page.wait_for_selector(download_btn_selector, timeout=30000)
        logger.info("Download button appeared")
    except Exception:
        logger.warning("Timeout waiting for download button")

    # Look for download button
    download_btn = page.query_selector(download_btn_selector)

    if not download_btn:
        logger.warning(f"No download button found for {manual['title']}")
        return None

    # Click the download button
    download_btn.click()
    random_delay(1, 2)

    # Check if a "reminder" popup appeared
    reminder_link = page.query_selector('a[href*="/download/"]:has-text("still want to look it up")')
    if reminder_link:
        logger.info("Reminder popup detected, clicking through...")
        reminder_link.click()
        random_delay(1, 2)
    else:
        # Maybe we need to navigate directly to download page
        manualzz_id = manual.get("manualzz_id") or extract_manualzz_id(manual["manual_url"])
        if manualzz_id:
            download_page_url = f"{BASE_URL}/download/{manualzz_id}"
            logger.info(f"Navigating to download page: {download_page_url}")
            page.goto(download_page_url, wait_until="domcontentloaded")
            random_delay(1, 2)

            # Check for Cloudflare challenge on download page
            if check_cloudflare_challenge(page):
                if not wait_for_cloudflare_solved(page):
                    logger.warning("Could not pass Cloudflare challenge on download page")
                    return None
                page.goto(download_page_url, wait_until="domcontentloaded")
                random_delay(1, 2)

    # Now we should be on the download page with captcha
    # Check for reCAPTCHA
    captcha_frame = page.query_selector('iframe[src*="recaptcha"]')
    if captcha_frame:
        if not wait_for_captcha_solved(page):
            return None
        random_delay(1, 2)

    # Check for hCaptcha (separate from Cloudflare challenge)
    hcaptcha_frame = page.query_selector('iframe[src*="hcaptcha.com"]')
    if hcaptcha_frame:
        if not wait_for_hcaptcha_solved(page):
            return None
        random_delay(1, 2)

    # Default original filename based on title
    default_filename = sanitize_filename(manual.get("title", "manual"))[:100] + ".pdf"

    # After captcha, look for the download link
    # The download link uses javascript:download_source()
    # We need to intercept the actual download or find the direct URL

    # Try to find the actual download link in .formats
    format_link = page.query_selector('.formats a.format, .formats a[onclick*="download_source"]')

    if format_link:
        # We need to click and capture the download
        logger.info("Found download format link, initiating download...")

        # Set up download handling
        with page.expect_download(timeout=60000) as download_info:
            try:
                format_link.click()
                download = download_info.value

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

    # Fallback: try any PDF link
    pdf_link = page.query_selector('a[href*=".pdf"]')
    if pdf_link:
        pdf_url = pdf_link.get_attribute("href")
        if pdf_url:
            # Handle protocol-relative URLs (starting with //)
            if pdf_url.startswith("//"):
                pdf_url = "https:" + pdf_url
            logger.info(f"Found PDF link: {pdf_url}")
            try:
                req = urllib.request.Request(pdf_url)
                req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

                with urllib.request.urlopen(req, timeout=120) as response:
                    # Get original filename from Content-Disposition or URL
                    content_disp = response.headers.get('Content-Disposition', '')
                    original_filename = None

                    if 'filename=' in content_disp:
                        match = re.search(r'filename[*]?=["\']?([^"\';\n]+)["\']?', content_disp)
                        if match:
                            original_filename = match.group(1).strip()
                            if original_filename.startswith("UTF-8''"):
                                original_filename = urllib.parse.unquote(original_filename[7:])
                            else:
                                original_filename = urllib.parse.unquote(original_filename)

                    if not original_filename:
                        url_path = urllib.parse.urlparse(pdf_url).path
                        original_filename = urllib.parse.unquote(url_path.split('/')[-1])

                    if not original_filename or len(original_filename) < 3:
                        original_filename = default_filename

                    if not original_filename.lower().endswith('.pdf'):
                        original_filename += '.pdf'

                    # Download to temp file
                    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                        tmp.write(response.read())
                        temp_path = Path(tmp.name)

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
                logger.error(f"Direct download failed: {e}")

    logger.warning(f"Could not download {manual['title']}")
    return None


def scrape_manualzz(catalog_urls: list[str], download_dir: Path, download: bool = True, extension_path: Path = None, browser: str = "chromium", headless: bool = False, use_stealth: bool = False, use_proxy: bool = False):
    """Main scraping function for manualzz."""
    database.init_db()

    with sync_playwright() as p:
        # Launch browser with extension support (requires persistent context)
        context, extension_loaded = launch_browser_with_extension(
            p,
            extension_path=extension_path,
            headless=headless,
            browser=browser,
            use_proxy=use_proxy,
        )

        # Persistent context may already have pages open, use the first one or create new
        if context.pages:
            page = context.pages[0]
        else:
            page = context.new_page()

        # Apply stealth patches to avoid fingerprint detection (if enabled)
        if use_stealth:
            apply_stealth(page)

        # If no extension loaded, use route-based ad blocking as fallback
        if not extension_loaded:
            setup_route_ad_blocking(page)
        else:
            logger.info("uBlock Origin extension loaded for ad blocking")

        try:
            total_count = 0
            for catalog_url in catalog_urls:
                logger.info(f"Scraping catalog: {catalog_url}")

                # Scrape all manual listings (adds to DB immediately for real-time progress)
                manual_count = scrape_catalog_page(page, catalog_url)
                total_count += manual_count

                random_delay(2, 4)

            if not download:
                logger.info(f"Scraping complete. Found {total_count} manuals. Skipping downloads.")
                context.close()
                return

            # Download pending manuals
            pending = database.get_undownloaded_manuals(source="manualzz")
            logger.info(f"Found {len(pending)} manuals to download")

            for manual_record in pending:
                try:
                    result = download_manual(
                        page,
                        {
                            "title": manual_record["model"],
                            "brand": manual_record["brand"],
                            "manual_url": manual_record["manual_url"],
                            "manualzz_id": manual_record.get("source_id"),
                        },
                        download_dir
                    )
                    if result:
                        file_path, sha1, md5, file_size, original_filename = result
                        database.update_downloaded(manual_record["id"], file_path, sha1, md5, file_size, original_filename)
                    random_delay()
                except Exception as e:
                    logger.error(f"Error downloading {manual_record['model']}: {e}")
                    continue

        finally:
            context.close()

    stats = database.get_stats(source="manualzz")
    logger.info(f"Manualzz scraping complete. Total: {stats['total']}, Downloaded: {stats['downloaded']}, Archived: {stats['archived']}, Pending: {stats['pending']}, Pending archive: {stats['pending_archive']}")


def main():
    parser = argparse.ArgumentParser(description="Scrape CRT manuals from Manualzz")
    parser.add_argument("--urls", nargs="*", help="Specific catalog URLs to scrape (overrides config)")
    parser.add_argument("--index-only", action="store_true", help="Only build index, don't download")
    parser.add_argument("--download-only", action="store_true", help="Only download pending manuals")
    parser.add_argument("--clear", action="store_true", help="Clear all manualzz records from database before scraping")
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

    if args.clear:
        logger.info("Clearing all manualzz records from database...")
        database.clear_manuals_by_source("manualzz")
        logger.info("Manualzz records cleared.")

    catalog_urls = args.urls or get_config(config, "urls", [])

    if not catalog_urls:
        logger.error("No catalog URLs specified. Add manualzz.urls to config.yaml or use --urls")
        return

    # Get extension path for ad blocking
    project_dir = Path(__file__).parent
    extension_path = get_extension_path(config, project_dir)
    if extension_path:
        logger.info(f"Using uBlock Origin extension: {extension_path}")
    else:
        logger.info("No uBlock Origin extension found - will use route-based ad blocking")
        logger.info("To use uBlock Origin, set 'ublock_origin_path' in config.yaml or place extension in ./extensions/ublock_origin/")

    # Get browser settings from config (with namespace override support)
    browser_type = get_config(config, "browser", "chromium")
    headless = get_config(config, "headless", False)
    use_stealth = get_config(config, "stealth", False)
    use_proxy = get_config(config, "use_proxy", False)

    if args.download_only:
        # Only download pending manuals
        with sync_playwright() as p:
            # Launch browser with extension support
            context, extension_loaded = launch_browser_with_extension(
                p,
                extension_path=extension_path,
                headless=headless,
                browser=browser_type,
                use_proxy=use_proxy,
            )

            # Persistent context may already have pages open
            if context.pages:
                page = context.pages[0]
            else:
                page = context.new_page()

            # Apply stealth patches to avoid fingerprint detection (if enabled)
            if use_stealth:
                apply_stealth(page)

            # If no extension loaded, use route-based ad blocking as fallback
            if not extension_loaded:
                setup_route_ad_blocking(page)

            try:
                pending = database.get_undownloaded_manuals(source="manualzz")
                logger.info(f"Found {len(pending)} pending manualzz downloads")

                for manual_record in pending:
                    try:
                        result = download_manual(
                            page,
                            {
                                "title": manual_record["model"],
                                "brand": manual_record["brand"],
                                "manual_url": manual_record["manual_url"],
                                "manualzz_id": manual_record.get("source_id"),
                            },
                            download_dir
                        )
                        if result:
                            file_path, sha1, md5, file_size, original_filename = result
                            database.update_downloaded(manual_record["id"], file_path, sha1, md5, file_size, original_filename)
                        random_delay()
                    except Exception as e:
                        logger.error(f"Error downloading {manual_record['model']}: {e}")
            finally:
                context.close()
    else:
        scrape_manualzz(catalog_urls, download_dir, download=not args.index_only, extension_path=extension_path, browser=browser_type, headless=headless, use_stealth=use_stealth, use_proxy=use_proxy)


if __name__ == "__main__":
    main()
