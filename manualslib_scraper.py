#!/usr/bin/env python3
import argparse
import hashlib
import logging
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
from dotenv import load_dotenv
from patchright.sync_api import sync_playwright, Page, Error as PlaywrightError

from pdf_utils import strip_manualslib_watermark

# Load environment variables from .env file
load_dotenv()

import database
from browser_helper import launch_browser_with_extension, get_extension_path, setup_route_ad_blocking, apply_stealth, setup_bandwidth_saving, proxy_supports_rotation, resolve_proxy_user, wait_for_cloudflare, get_proxy_url
from captcha_solver import TwoCaptchaSolver, extract_sitekey_from_page, inject_captcha_response
from local_captcha import LocalSolver, ImageChallengeSolver, WhisperTranscriber

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BASE_URL = "https://www.manualslib.com"

# Circuit breaker settings
MAX_CONSECUTIVE_FAILURES = 3
# With proxy rotation, every failed download retries on a fresh exit IP, so an
# IP ban self-heals; allow more attempts before concluding the site is down
MAX_CONSECUTIVE_ROTATION_FAILURES = 5


class DownloadCircuitBreakerError(Exception):
    """Raised when too many consecutive download failures occur."""
    pass


class DownloadFailureTracker:
    """
    Tracks consecutive download failures and decides between rotating the
    proxy IP and tripping the circuit breaker.

    With rotation available, each failure rotates to a fresh exit IP and the
    breaker only trips after MAX_CONSECUTIVE_ROTATION_FAILURES consecutive
    failures (each already on a different IP). Without rotation, it trips
    after MAX_CONSECUTIVE_FAILURES.
    """

    def __init__(self, rotate_page=None):
        self.rotate_page = rotate_page
        self.consecutive_failures = 0
        self.max_failures = MAX_CONSECUTIVE_ROTATION_FAILURES if rotate_page else MAX_CONSECUTIVE_FAILURES

    def success(self):
        self.consecutive_failures = 0

    def failure(self, page: Page) -> Page:
        """
        Record one failed download. Raises DownloadCircuitBreakerError at the
        limit; otherwise rotates the proxy IP (when available) and returns the
        page to use for the next attempt.
        """
        self.consecutive_failures += 1
        logger.warning(f"Download failed ({self.consecutive_failures}/{self.max_failures} consecutive failures)")
        if self.consecutive_failures >= self.max_failures:
            if self.rotate_page:
                raise DownloadCircuitBreakerError(
                    f"Stopping after {self.max_failures} consecutive download failures, "
                    "each retried on a freshly rotated proxy IP. "
                    "This likely indicates a site issue rather than an IP ban."
                )
            raise DownloadCircuitBreakerError(
                f"Stopping after {self.max_failures} consecutive download failures. "
                "This may indicate an IP ban or site issue."
            )
        if self.rotate_page:
            return self.rotate_page()
        return page


class DownloadLimitReached(Exception):
    """Raised when the download limit is reached."""
    pass


ARCHIVE_ORG_BASE = "https://archive.org/details/manualslib-id-"
CAPTCHA_TIMEOUT = 300  # 5 minutes to solve captcha


def extract_manualslib_id(url: str) -> str | None:
    """Extract the numeric ID from a manualslib URL like /manual/331384/..."""
    match = re.search(r'/manual/(\d+)/', url)
    return match.group(1) if match else None


def extract_model_id(url: str) -> str | None:
    """Extract the model ID from a product URL like /products/Rca-35v432t-328381.html"""
    match = re.search(r'-(\d+)\.html', url)
    return match.group(1) if match else None


class ManualUnavailableError(Exception):
    """The site no longer has the file (its PDF link 404s) - not an IP or session problem."""


# manualslib_ids whose file turned out to be gone during this run, so the
# other models listed under the same document aren't each tried (and a
# captcha solved) just to hit the same dead link.
_unavailable_ids: set[str] = set()


class CloudflareChallengeError(Exception):
    """Raised when a page stays behind Cloudflare's challenge interstitial."""


# Set in main() when local captcha solving is enabled: reCAPTCHAs are first
# attempted in the browser (checkbox click, image challenge via local vision
# models, audio challenge via Whisper) and 2captcha is only paid for when
# that fails.
_local_solver: LocalSolver | None = None


# Transient network failures (a proxy hop dropping the TLS handshake, a
# reset connection) are retried this many times before the error propagates.
NAVIGATE_RETRIES = 3


def navigate(page: Page, url: str) -> None:
    """Go to a manualslib URL and wait out Cloudflare's challenge if it appears.

    Chromium network errors (net::ERR_*) and navigation timeouts are retried
    with a short backoff; anything else is raised immediately.
    """
    for attempt in range(1, NAVIGATE_RETRIES + 1):
        try:
            page.goto(url, wait_until="domcontentloaded")
            break
        except PlaywrightError as e:
            transient = "net::ERR_" in str(e) or "Timeout" in type(e).__name__
            if not transient or attempt == NAVIGATE_RETRIES:
                raise
            delay = 5 * attempt
            logger.warning(f"Navigation to {url} failed ({str(e).splitlines()[0]}); retry {attempt}/{NAVIGATE_RETRIES - 1} in {delay}s")
            time.sleep(delay)
    if not wait_for_cloudflare(page):
        raise CloudflareChallengeError(f"Cloudflare challenge not passed for {url}")


def check_archive_org(manualslib_id: str) -> tuple[bool, str]:
    """Check if a manual exists on archive.org. Returns (exists, archive_url)."""
    archive_url = f"{ARCHIVE_ORG_BASE}{manualslib_id}"
    try:
        req = urllib.request.Request(archive_url, method='HEAD')
        req.add_header('User-Agent', 'Mozilla/5.0 (compatible; ManualsLibScraper/1.0)')
        with urllib.request.urlopen(req, timeout=10) as response:
            # 200 means it exists
            return True, archive_url
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, archive_url
        logger.warning(f"HTTP error checking archive.org: {e.code}")
        return False, archive_url
    except Exception as e:
        logger.warning(f"Error checking archive.org: {e}")
        return False, archive_url


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_config(config: dict, key: str, default=None, namespace: str = "manualslib"):
    """Get config value with namespace override support.

    Checks namespace-specific value first, falls back to global.
    e.g., get_config(config, "use_proxy") checks manualslib.use_proxy, then use_proxy
    """
    ns_config = config.get(namespace, {})
    if key in ns_config:
        return ns_config[key]
    return config.get(key, default)


# Global settings (updated from config in main())
DELAY_MIN = 2.0
DELAY_MAX = 5.0
STRIP_WATERMARKS = True
DOWNLOAD_LIMIT = None
DOWNLOAD_COUNT = 0


def check_download_limit():
    """Check if download limit reached. Raises DownloadLimitReached if so."""
    global DOWNLOAD_COUNT
    if DOWNLOAD_LIMIT and DOWNLOAD_COUNT >= DOWNLOAD_LIMIT:
        raise DownloadLimitReached(f"Download limit reached ({DOWNLOAD_LIMIT})")


def increment_download_count():
    """Increment download count and log progress if limit is set."""
    global DOWNLOAD_COUNT
    DOWNLOAD_COUNT += 1
    if DOWNLOAD_LIMIT:
        logger.info(f"Downloaded {DOWNLOAD_COUNT}/{DOWNLOAD_LIMIT}")


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

    Example: downloads/ab/cd/abcdef1234567890...pdf
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


def extract_slug_from_url(url: str) -> str | None:
    """Extract brand slug from URL like /brand/hitachi/ or /brand/hitachi/tv.html"""
    match = re.search(r'/brand/([^/]+)/?', url)
    return match.group(1) if match else None


def is_tv_category(cat_name: str) -> bool:
    """Check if a category name matches our TV criteria.

    Matches:
    - Exact "TV" (case-insensitive)
    - "TV * Combo" pattern (e.g., "TV DVD Combo", "TV VCR Combo")
    """
    name_lower = cat_name.lower().strip()

    # Exact match for "TV"
    if name_lower == "tv":
        return True

    # Pattern: "TV <something> Combo"
    if name_lower.startswith("tv ") and name_lower.endswith(" combo"):
        return True

    return False


def discover_brands(page: Page) -> tuple[list[dict], set[str]]:
    """Discover all brands that have TV in their categories.

    Returns:
        tuple: (list of brand dicts, set of all categories containing 'tv')

    1. Visit https://www.manualslib.com/brand/
    2. Find all letter/number links in the header
    3. Visit each page (with pagination) and find brands with TV category
    """
    brands = []
    seen_slugs = set()
    all_tv_related_categories = set()  # Track all categories with "tv" in name

    # First, get all the letter/number index links
    logger.info("Discovering brands with TV category...")
    navigate(page, f"{BASE_URL}/brand/")
    random_delay(1, 2)

    # Find all index links in the bmap div
    index_links = page.query_selector_all('div.bmap a')
    index_urls = []
    for link in index_links:
        href = link.get_attribute("href")
        if href:
            url = href if href.startswith("http") else BASE_URL + href
            index_urls.append(url)

    logger.info(f"Found {len(index_urls)} index pages to scan")

    # Visit each index page
    for index_url in index_urls:
        current_url = index_url
        page_num = 1

        while current_url:
            logger.info(f"Scanning: {current_url} (page {page_num})")
            navigate(page, current_url)
            random_delay(1, 2)

            # Find all brand rows
            brand_rows = page.query_selector_all('div.row.tabled')

            for row in brand_rows:
                # Get brand info from col1
                brand_link = row.query_selector('div.col1 a, div.col-xs-3 a')
                if not brand_link:
                    continue

                brand_name = brand_link.inner_text().strip()
                brand_href = brand_link.get_attribute("href")
                brand_url = brand_href if brand_href.startswith("http") else BASE_URL + brand_href
                slug = extract_slug_from_url(brand_url)

                if not slug or slug in seen_slugs:
                    continue

                # Get categories from catel div
                category_links = row.query_selector_all('div.catel a, div.col-xs-9 a')
                all_categories = []
                tv_categories = []
                tv_category_urls = []

                for cat_link in category_links:
                    cat_name = cat_link.inner_text().strip()
                    cat_href = cat_link.get_attribute("href")
                    cat_url = cat_href if cat_href and cat_href.startswith("http") else BASE_URL + (cat_href or "")

                    all_categories.append(cat_name)

                    # Track all categories with "tv" in them for review
                    if 'tv' in cat_name.lower():
                        all_tv_related_categories.add(cat_name)

                    # Check if this matches our exact TV criteria
                    if is_tv_category(cat_name):
                        tv_categories.append(cat_name)
                        tv_category_urls.append(cat_url)

                if tv_categories:
                    seen_slugs.add(slug)
                    brand_info = {
                        "name": brand_name,
                        "slug": slug,
                        "brand_url": brand_url,
                        "tv_categories": ", ".join(tv_categories),
                        "tv_category_urls": ", ".join(tv_category_urls),
                        "all_categories": ", ".join(all_categories),
                    }
                    brands.append(brand_info)

                    # Add to database immediately so progress is visible
                    brand_id = database.add_brand(
                        name=brand_info["name"],
                        slug=brand_info["slug"],
                        brand_url=brand_info["brand_url"],
                        tv_categories=brand_info["tv_categories"],
                        tv_category_urls=brand_info["tv_category_urls"],
                        all_categories=brand_info["all_categories"],
                    )
                    if brand_id:
                        logger.info(f"Added TV brand: {brand_name} ({slug}) - Categories: {', '.join(tv_categories)}")
                    else:
                        logger.info(f"Found TV brand (already in DB): {brand_name} ({slug})")

            # Check for next page in pagination
            next_page_link = page.query_selector('ul.pagination li.active + li a.plink')
            if next_page_link:
                next_href = next_page_link.get_attribute("href")
                if next_href:
                    current_url = next_href if next_href.startswith("http") else BASE_URL + next_href
                    page_num += 1
                    random_delay(1, 2)
                else:
                    current_url = None
            else:
                current_url = None

    logger.info(f"Discovered {len(brands)} brands with TV category")
    return brands, all_tv_related_categories


def scrape_category_listing(page: Page, brand: str, category_url: str, category_name: str = None) -> int:
    """Scrape all manual links from a brand's category listing pages.

    Args:
        page: Playwright page object
        brand: Brand name/slug for database records
        category_url: Full URL to the category page (e.g., /brand/magnavox/tv-dvd-combo.html)
        category_name: Optional category name for logging

    Adds manuals to database immediately as they're found for real-time progress.
    Returns the count of manuals found.

    HTML structure:
    <div class="row tabled">
        <div class="col-sm-2 mname">
            <a href="/products/Rca-35v432t-328381.html">35V432T</a>
        </div>
        <div class="col-sm-10 mlinks">
            <div class="mdiv">
                <a href="/manual/138703/Rca-35v432t.html" title="2 pages...">Specification sheet</a>
                <a href="/manual/313150/Rca-35v432t.html" title="64 pages...">user manual</a>
            </div>
        </div>
    </div>
    """
    seen_urls = set()
    page_num = 1
    manual_count = 0

    cat_display = category_name or category_url.split('/')[-1].replace('.html', '')
    current_url = category_url  # Start with the provided URL

    while current_url:
        logger.info(f"Scraping {brand} [{cat_display}] page {page_num}: {current_url}")
        navigate(page, current_url)
        random_delay(1, 2)

        # Find all model rows
        model_rows = page.query_selector_all('div.row.tabled')

        if not model_rows:
            logger.info(f"No more models found for {brand} [{cat_display}] on page {page_num}")
            current_url = None  # Exit the loop
            continue

        for row in model_rows:
            # Get model info from the mname column
            model_link_elem = row.query_selector('div.mname a')
            if not model_link_elem:
                continue

            model_name = model_link_elem.inner_text().strip()
            model_href = model_link_elem.get_attribute("href")
            model_url = model_href if model_href.startswith("http") else BASE_URL + model_href
            model_id = extract_model_id(model_url)

            # Find all manual links in the mlinks column
            manual_links = row.query_selector_all('div.mlinks a[href*="/manual/"]')

            for link in manual_links:
                href = link.get_attribute("href")
                if not href:
                    continue

                manual_url = href if href.startswith("http") else BASE_URL + href

                # Skip if we've already seen this URL
                if manual_url in seen_urls:
                    continue
                seen_urls.add(manual_url)

                # Document type is the link text
                doc_type = link.inner_text().strip()

                # Document description is in the title attribute
                doc_description = link.get_attribute("title") or ""

                # Extract manualslib ID from the manual URL
                manualslib_id = extract_manualslib_id(manual_url)

                # Add to database immediately for real-time progress
                manual_id = database.add_manual(
                    brand=brand,
                    model=model_name,
                    model_url=model_url,
                    model_id=model_id,
                    doc_type=doc_type,
                    doc_description=doc_description,
                    manual_url=manual_url,
                    manualslib_id=manualslib_id,
                )
                if manual_id:
                    logger.info(f"Added: {model_name} - {doc_type}")
                manual_count += 1

        # Check for next page
        # Pagination structure: <ul class="pagination"><li class="active">...</li><li><a class="plink" href="...">2</a></li></ul>
        next_page_link = page.query_selector('ul.pagination li.active + li a.plink')
        if next_page_link:
            next_href = next_page_link.get_attribute("href")
            if next_href:
                # Use the full URL from the link
                current_url = next_href if next_href.startswith("http") else BASE_URL + next_href
                page_num += 1
                random_delay()
                continue

        # No more pages
        logger.info(f"Reached last page for {brand} [{cat_display}]")
        current_url = None  # Exit the loop

    logger.info(f"Found {manual_count} manuals for {brand} [{cat_display}]")
    return manual_count


def wait_for_captcha_solved(page: Page, timeout: int = CAPTCHA_TIMEOUT, captcha_solver: TwoCaptchaSolver = None) -> bool:
    """
    Wait for captcha to be solved.

    If captcha_solver is provided, attempts automatic solving via 2captcha.
    Otherwise, waits for human to solve in the browser window.

    Returns True if solved, False if timeout.
    """
    # Free first: checkbox click, then audio challenge + local Whisper
    if _local_solver is not None:
        logger.info("Attempting local captcha solving...")
        if _local_solver.solve(page):
            time.sleep(1)
            return True
        logger.info("Local captcha solving did not succeed" + (", trying 2captcha" if captcha_solver else ""))

    # Try automatic solving with 2captcha if configured
    if captcha_solver:
        logger.info("Attempting automatic captcha solving with 2captcha...")

        # Extract sitekey from page
        sitekey = extract_sitekey_from_page(page)
        if sitekey:
            logger.info(f"Found sitekey: {sitekey[:20]}...")
            page_url = page.url

            # Solve with 2captcha
            token = captcha_solver.solve_recaptcha(sitekey, page_url)
            if token:
                # Inject the token into the page
                if inject_captcha_response(page, token):
                    logger.info("Captcha token injected successfully")
                    # Give the page a moment to process
                    time.sleep(1)
                    return True
                else:
                    logger.warning("Failed to inject captcha token, falling back to manual")
            else:
                logger.warning("2captcha failed, falling back to manual solving")
        else:
            logger.warning("Could not extract sitekey, falling back to manual solving")

    # Fall back to manual solving
    logger.info("Waiting for captcha to be solved manually...")
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
            # Look for the checkmark that appears when solved
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


def download_file_to_temp(url: str, use_proxy: bool = False) -> tuple[Path, str] | None:
    """
    Download a file to a temp location.

    Returns (temp_path, original_filename) if successful, None otherwise.
    The original_filename is extracted from Content-Disposition header or URL.

    Args:
        url: URL to download
        use_proxy: If True, use configured proxy. Defaults to False since signed
                   download URLs work without proxy and proxies are metered by bandwidth.
    """
    import tempfile

    # Handle protocol-relative URLs (starting with //)
    if url.startswith("//"):
        url = "https:" + url

    try:
        # Set up proxy if configured
        proxy_url = get_proxy_url() if use_proxy else None
        if proxy_url:
            proxy_handler = urllib.request.ProxyHandler({
                'http': proxy_url,
                'https': proxy_url,
            })
            opener = urllib.request.build_opener(proxy_handler)
        else:
            opener = urllib.request.build_opener()

        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

        with opener.open(req, timeout=120) as response:
            # Try to get filename from Content-Disposition header
            content_disp = response.headers.get('Content-Disposition', '')
            original_filename = None

            if 'filename=' in content_disp:
                # Parse filename from header like: attachment; filename="file.pdf"
                match = re.search(r'filename[*]?=["\']?([^"\';\n]+)["\']?', content_disp)
                if match:
                    original_filename = match.group(1).strip()
                    # Handle URL-encoded filenames
                    if original_filename.startswith("UTF-8''"):
                        original_filename = urllib.parse.unquote(original_filename[7:])
                    else:
                        original_filename = urllib.parse.unquote(original_filename)

            # Fallback: extract from URL
            if not original_filename:
                url_path = urllib.parse.urlparse(url).path
                original_filename = urllib.parse.unquote(url_path.split('/')[-1])

            # Ensure we have a .pdf extension
            if not original_filename.lower().endswith('.pdf'):
                original_filename += '.pdf'

            # Download to temp file
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                tmp.write(response.read())
                temp_path = Path(tmp.name)

        return temp_path, original_filename

    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ManualUnavailableError(f"PDF link is dead (404): {url}") from e
        logger.error(f"Failed to download {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to download {url}: {e}")
        return None


def download_manual(page: Page, manual: dict, download_dir: Path, brand: str, captcha_solver: TwoCaptchaSolver = None) -> tuple[str, str, str, int, str, str | None, str | None, str | None, int | None] | None:
    """
    Download a single manual using content-addressable storage.

    Files are stored based on SHA1 hash in a trie structure: downloads/ab/cd/abcdef...pdf
    The original filename is preserved in the database for display purposes.

    Returns (file_path, sha1, md5, file_size, original_filename) if successful, None otherwise.
    """
    logger.info(f"Downloading: {manual['model']} - {manual['url']}")

    navigate(page, manual["url"])
    random_delay(1, 2)

    # Look for download button
    download_btn = page.query_selector('a:has-text("Download"), button:has-text("Download")')
    if not download_btn:
        # Try alternative selectors
        download_btn = page.query_selector('.download-btn, .btn-download, [class*="download"]')

    if not download_btn:
        logger.warning(f"No download button found for {manual['model']}")
        return None

    # Click download button
    download_btn.click()
    random_delay(1, 2)

    # Wait for reCAPTCHA to fully load before proceeding
    logger.info("Waiting for reCAPTCHA to load...")
    try:
        # Wait for the reCAPTCHA iframe to appear and be ready
        page.wait_for_selector('iframe[src*="recaptcha"]', timeout=30000)
        # Give it a moment to fully initialize
        random_delay(1, 2)
        logger.info("reCAPTCHA loaded")
    except Exception as e:
        logger.warning(f"reCAPTCHA did not load within timeout: {e}")
        # Continue anyway - maybe there's no captcha on this page

    # Check for captcha and solve it
    captcha_frame = page.query_selector('iframe[src*="recaptcha"]')
    if captcha_frame:
        if not wait_for_captcha_solved(page, captcha_solver=captcha_solver):
            return None

    # Flow: Captcha solved → "Get Manual" button → "Download PDF" button

    # Step 1: Wait for and click "Get Manual" button
    logger.info("Waiting for Get Manual button...")
    try:
        get_manual_btn = page.wait_for_selector(
            'a:has-text("Get Manual"), button:has-text("Get Manual")',
            timeout=10000
        )
        if get_manual_btn:
            logger.info("Found Get Manual button, clicking...")
            get_manual_btn.click()
            random_delay(1, 2)
    except Exception as e:
        logger.debug(f"No Get Manual button found: {e}")

    # Step 2: Wait for "Download PDF" link and extract URL
    logger.info("Waiting for Download PDF link...")
    pdf_url = None
    try:
        pdf_link = page.wait_for_selector(
            'a[href*="manualslib.com/pdf"], a[href*=".pdf"][href*="take=binary"], a:has-text("Download PDF")',
            timeout=10000
        )
        if pdf_link:
            pdf_url = pdf_link.get_attribute("href")
    except Exception as e:
        logger.debug(f"No Download PDF link found: {e}")

    # Fallback: look for any direct PDF link
    if not pdf_url:
        pdf_link = page.query_selector('a[href*=".pdf"]')
        if pdf_link:
            pdf_url = pdf_link.get_attribute("href")
            if pdf_url:
                logger.info(f"Found fallback PDF URL: {pdf_url}")

    if not pdf_url:
        logger.warning(f"Could not find download mechanism for {manual['model']}")
        return None

    logger.info(f"Found PDF URL: {pdf_url}")

    # Download to temp file
    result = download_file_to_temp(pdf_url)
    if not result:
        return None

    temp_path, original_filename = result

    # Compute original checksums
    original_sha1, original_md5 = compute_checksums(temp_path)
    original_file_size = temp_path.stat().st_size
    original_file_path = None

    if STRIP_WATERMARKS:
        # Store original file BEFORE stripping
        original_storage_path = get_sha1_storage_path(download_dir, original_sha1)
        original_storage_path.parent.mkdir(parents=True, exist_ok=True)

        if not original_storage_path.exists():
            # Copy original to storage (keep temp for stripping)
            shutil.copy2(str(temp_path), str(original_storage_path))
            logger.info(f"Stored original: {original_storage_path} (SHA1: {original_sha1[:8]}...)")
        else:
            logger.info(f"Original already exists at {original_storage_path}")

        original_file_path = str(original_storage_path)

        # Strip watermark on temp file
        modified = strip_manualslib_watermark(temp_path)

        if modified:
            # Compute final checksums after stripping
            sha1, md5 = compute_checksums(temp_path)
            file_size = temp_path.stat().st_size

            # Move stripped file to its own storage path
            final_path = get_sha1_storage_path(download_dir, sha1)
            final_path.parent.mkdir(parents=True, exist_ok=True)

            if final_path.exists():
                logger.info(f"Stripped file already exists at {final_path}")
                temp_path.unlink()
            else:
                shutil.move(str(temp_path), str(final_path))
                logger.info(f"Stored stripped: {final_path} (SHA1: {sha1[:8]}...)")

            logger.info(f"Original filename: {original_filename}")
            return str(final_path), sha1, md5, file_size, original_filename, original_sha1, original_md5, original_file_path, original_file_size
        else:
            # Stripping didn't change file - remove temp and use original path
            temp_path.unlink()
            logger.info(f"Watermark stripping made no changes, using original")
            logger.info(f"Original filename: {original_filename}")
            return original_file_path, original_sha1, original_md5, original_file_size, original_filename, None, None, None, None
    else:
        # No stripping - just store as original
        final_path = get_sha1_storage_path(download_dir, original_sha1)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        if final_path.exists():
            logger.info(f"File already exists at {final_path} (duplicate content)")
            temp_path.unlink()
        else:
            shutil.move(str(temp_path), str(final_path))

        logger.info(f"Downloaded: {final_path} ({original_file_size} bytes, SHA1: {original_sha1[:8]}...)")
        logger.info(f"Original filename: {original_filename}")
        return str(final_path), original_sha1, original_md5, original_file_size, original_filename, None, None, None, None


def download_pending(page: Page, brand: str, download_dir: Path, captcha_solver, failures: DownloadFailureTracker) -> Page:
    """
    Download a brand's pending manuals (not downloaded, not archived).

    ManualsLib lists one document under every model it covers, so many pending
    rows are siblings sharing a manualslib_id. Each document is only fetched
    once: siblings reuse the first download (or its archive.org status).

    Returns the page to keep using (it changes when the proxy IP is rotated).
    """
    archived, reused = database.sync_sibling_manuals()
    if reused or archived:
        logger.info(f"Resolved from sibling manuals: {reused} reuse an existing download, {archived} already archived")

    pending = database.get_undownloaded_manuals(brand, source="manualslib")
    logger.info(f"Found {len(pending)} manuals to download for {brand}")

    for manual_record in pending:
        try:
            # The pending list is a snapshot: a sibling handled earlier in this
            # run may have already taken care of this document
            resolved = database.resolve_from_siblings(manual_record["id"])
            if resolved:
                logger.info(f"Skipping {manual_record['model']}: same document as a sibling manual ({resolved})")
                continue

            # Check download limit before each download
            check_download_limit()

            # Extract manualslib_id if not already in DB
            manualslib_id = manual_record.get("manualslib_id")
            if not manualslib_id:
                manualslib_id = extract_manualslib_id(manual_record["manual_url"])
                if manualslib_id:
                    database.update_manualslib_id(manual_record["id"], manualslib_id)

            if manualslib_id and str(manualslib_id) in _unavailable_ids:
                logger.info(f"Skipping {manual_record['model']}: document {manualslib_id} is unavailable on manualslib")
                continue

            # Check if already archived on archive.org
            if manualslib_id:
                logger.info(f"Checking archive.org for {manual_record['model']} (ID: {manualslib_id})...")
                is_archived, archive_url = check_archive_org(manualslib_id)
                if is_archived:
                    logger.info(f"Already archived: {archive_url}")
                    database.update_archived(manual_record["id"], archive_url)
                    continue

            # Not archived, proceed with download
            result = download_manual(
                page,
                {"model": manual_record["model"], "url": manual_record["manual_url"], "doc_type": manual_record["doc_type"]},
                download_dir,
                brand,
                captcha_solver=captcha_solver
            )
            if result:
                file_path, sha1, md5, file_size, original_filename, original_sha1, original_md5, original_file_path, original_file_size = result
                database.update_downloaded(
                    manual_record["id"], file_path, sha1, md5, file_size, original_filename,
                    original_sha1, original_md5, original_file_path, original_file_size
                )
                failures.success()
                increment_download_count()
            else:
                page = failures.failure(page)
            random_delay()
        except (DownloadCircuitBreakerError, DownloadLimitReached):
            raise  # Re-raise to stop
        except ManualUnavailableError as e:
            # The file is gone on manualslib's side. The browser session is
            # fine, so don't count it against the breaker or rotate away a
            # session that has already passed Cloudflare.
            logger.warning(f"Skipping {manual_record['model']}: {e}")
            if manualslib_id:
                _unavailable_ids.add(str(manualslib_id))
        except Exception as e:
            logger.error(f"Error downloading {manual_record['model']}: {e}")
            page = failures.failure(page)

    return page


def scrape_brand(page: Page, brand: str, download_dir: Path, download: bool = True, category_urls: list[str] = None, categories: list[str] = None, captcha_solver: TwoCaptchaSolver = None, rotate_page=None):
    """Scrape all CRT manuals for a brand.

    Args:
        page: Playwright page object
        brand: Brand slug
        download_dir: Directory to download files to
        download: Whether to download files after scraping
        category_urls: List of full category URLs to scrape (from discovered brands)
        categories: List of category slugs like ['tv', 'tv-dvd-combo'] to build URLs from
        captcha_solver: Optional 2captcha solver for automatic captcha solving
        rotate_page: Optional callable that relaunches the browser with a fresh
                     proxy session (new exit IP) and returns the new page. Called
                     after each download failure so the circuit breaker only trips
                     if multiple different IPs fail.
    """
    logger.info(f"Starting scrape for brand: {brand}")

    # Determine which category URLs to scrape
    urls_to_scrape = []
    if category_urls:
        # Use provided category URLs (from discovered brands)
        urls_to_scrape = [(url, None) for url in category_urls]
    elif categories:
        # Build URLs from category slugs
        for cat in categories:
            url = f"{BASE_URL}/brand/{brand}/{cat}.html"
            urls_to_scrape.append((url, cat))
    else:
        # Default to just "tv" category
        urls_to_scrape = [(f"{BASE_URL}/brand/{brand}/tv.html", "tv")]

    # Scrape all category listings
    total_manual_count = 0
    for cat_url, cat_name in urls_to_scrape:
        manual_count = scrape_category_listing(page, brand, cat_url, cat_name)
        total_manual_count += manual_count
        random_delay(1, 2)

    if not download:
        logger.info(f"Scraping complete for {brand}. Found {total_manual_count} manuals. Skipping downloads.")
        return

    download_pending(page, brand, download_dir, captcha_solver, DownloadFailureTracker(rotate_page))


def main():
    parser = argparse.ArgumentParser(description="Scrape CRT manuals from ManualsLib")
    parser.add_argument("--brands", nargs="*", help="Specific brands to scrape (overrides config and discovered brands)")
    parser.add_argument("--discover-brands", action="store_true", help="Discover all brands with TV category")
    parser.add_argument("--use-discovered", action="store_true", help="Scrape all discovered brands (instead of config)")
    parser.add_argument("--index-only", action="store_true", help="Only build index, don't download")
    parser.add_argument("--download-only", action="store_true", help="Only download pending manuals")
    parser.add_argument("--limit", type=int, help="Limit number of downloads")
    parser.add_argument("--upload-to-ia", action="store_true", help="Upload downloaded manuals to Internet Archive")
    parser.add_argument("--ia-limit", type=int, help="Limit number of uploads to Internet Archive")
    parser.add_argument("--clear", action="store_true", help="Clear all manual records from database before scraping")
    parser.add_argument("--clear-brands", action="store_true", help="Clear all discovered brands from database")
    parser.add_argument("--clear-all", action="store_true", help="Clear both manuals and brands from database")
    args = parser.parse_args()

    config = load_config()
    download_dir = Path(config.get("download_dir", "./downloads")).resolve()
    download_dir.mkdir(parents=True, exist_ok=True)

    # Set global values from config/args
    global DELAY_MIN, DELAY_MAX, STRIP_WATERMARKS, DOWNLOAD_LIMIT
    DELAY_MIN = config.get("delay_min", 2.0)
    DELAY_MAX = config.get("delay_max", 5.0)
    STRIP_WATERMARKS = config.get("strip_watermarks", True)
    DOWNLOAD_LIMIT = args.limit
    logger.info(f"Request delays: {DELAY_MIN}-{DELAY_MAX} seconds")
    logger.info(f"Strip watermarks: {STRIP_WATERMARKS}")
    if DOWNLOAD_LIMIT:
        logger.info(f"Download limit: {DOWNLOAD_LIMIT}")

    # Initialize 2captcha solver if API key is configured
    # Check environment variable first, then fall back to config.yaml
    captcha_solver = None
    twocaptcha_key = os.environ.get("TWOCAPTCHA_API_KEY") or config.get("twocaptcha_api_key")
    if twocaptcha_key:
        captcha_solver = TwoCaptchaSolver(twocaptcha_key)
        balance = captcha_solver.get_balance()
        if balance is not None:
            logger.info(f"2captcha enabled (balance: ${balance:.2f})")
        else:
            logger.warning("2captcha API key configured but could not verify balance")
    else:
        logger.info("2captcha not configured - will use manual captcha solving")

    global _local_solver
    if get_config(config, "local_captcha", True):
        _local_solver = LocalSolver(
            image=ImageChallengeSolver() if get_config(config, "captcha_image_solver", True) else None,
            audio=WhisperTranscriber(
                model_size=get_config(config, "whisper_model", "small.en"),
                device=get_config(config, "whisper_device", "auto"),
            ) if get_config(config, "captcha_audio_solver", True) else None,
        )
        logger.info(f"Local captcha solving enabled: {_local_solver.describe()}; 2captcha is the fallback")

    database.init_db()

    if args.clear_all:
        logger.info("Clearing all records from database (manuals and brands)...")
        database.clear_everything()
        logger.info("Database cleared.")
    elif args.clear_brands:
        logger.info("Clearing all discovered brands from database...")
        database.clear_brands()
        logger.info("Brands cleared.")
    elif args.clear:
        logger.info("Clearing all manual records from database...")
        database.clear_all()
        logger.info("Manuals cleared.")

    # Handle Internet Archive upload (no browser needed)
    if args.upload_to_ia:
        from ia_uploader import upload_all_pending, get_uploadable_manuals

        manuals = get_uploadable_manuals(source="manualslib", limit=args.ia_limit)
        logger.info(f"Found {len(manuals)} manuals ready for Internet Archive upload")

        if manuals:
            success, failed = upload_all_pending(
                source="manualslib",
                limit=args.ia_limit,
            )
            logger.info(f"Internet Archive upload complete. Success: {success}, Failed: {failed}")
        else:
            logger.info("No manuals to upload")
        return

    # Get browser and extension settings (with namespace override support)
    project_dir = Path(__file__).parent
    browser_type = get_config(config, "browser", "chromium")
    headless = get_config(config, "headless", False)
    virtual_display = get_config(config, "virtual_display", False)
    use_stealth = get_config(config, "stealth", False)
    if use_stealth:
        # Cloudflare's managed challenge on manualslib detects the stealth JS
        # patches and never clears; patchright already hides automation.
        logger.warning("Ignoring stealth: true - stealth patches break manualslib's Cloudflare challenge")
        use_stealth = False
    use_proxy = get_config(config, "use_proxy", False)
    extension_path = get_extension_path(config, project_dir)

    # Log proxy configuration (proxy is only used for browser, not file downloads)
    if use_proxy:
        proxy_url_env = os.environ.get("PROXY_URL")
        if proxy_url_env:
            parsed = urllib.parse.urlsplit(proxy_url_env)
            host, port = parsed.hostname, parsed.port
        else:
            host = os.environ.get("PROXY_HOST")
            port = os.environ.get("PROXY_PORT")
        if host and port:
            logger.info(f"Proxy configured for browser: {host}:{port}")
            logger.info("Note: File downloads use direct connection (no proxy) to save bandwidth")
            if proxy_supports_rotation():
                logger.info(
                    "Proxy IP rotation enabled: browser will relaunch with a fresh session after each download failure "
                    f"(circuit breaker trips after {MAX_CONSECUTIVE_ROTATION_FAILURES} consecutive failures instead of {MAX_CONSECUTIVE_FAILURES})"
                )
            else:
                logger.info("Tip: add a {session} placeholder to the proxy username in .env to enable automatic IP rotation on download failures")

    if extension_path:
        logger.info(f"Using uBlock Origin extension: {extension_path}")
    else:
        logger.info("No uBlock Origin extension found - will use route-based ad blocking")

    with sync_playwright() as p:
        def launch_and_setup():
            # Launch browser with extension support (requires persistent context)
            ctx, ext_loaded = launch_browser_with_extension(
                p,
                extension_path=extension_path,
                headless=headless,
                browser=browser_type,
                use_proxy=use_proxy,
                virtual_display=virtual_display,
            )

            # Persistent context may already have pages open, use the first one or create new
            if ctx.pages:
                pg = ctx.pages[0]
            else:
                pg = ctx.new_page()

            # Apply stealth patches to avoid fingerprint detection (if enabled)
            if use_stealth:
                apply_stealth(pg)

            # If no extension loaded, use route-based ad blocking as fallback
            if not ext_loaded:
                setup_route_ad_blocking(pg)
            else:
                logger.info("uBlock Origin extension loaded for ad blocking")

            # Block bandwidth-heavy static content to save proxy costs
            setup_bandwidth_saving(pg)
            return ctx, pg

        context, page = launch_and_setup()

        def rotate_browser():
            """Relaunch the browser with a fresh proxy session to force a new exit IP."""
            nonlocal context, page
            logger.info("Rotating proxy IP: relaunching browser with a fresh session...")
            try:
                context.close()
            except Exception as e:
                logger.warning(f"Error closing browser during IP rotation: {e}")
            context, page = launch_and_setup()
            return page

        rotate_page = rotate_browser if use_proxy and proxy_supports_rotation() else None

        try:
            # Brand discovery mode
            if args.discover_brands:
                # Brands are added to DB inside discover_brands() for real-time progress
                discovered_brands, all_tv_related_categories = discover_brands(page)

                brand_stats = database.get_brand_stats()
                logger.info(f"Brand discovery complete. Total: {brand_stats['total']}, Pending: {brand_stats['pending']}")

                # Log all TV-related categories for review
                if all_tv_related_categories:
                    logger.info("\n" + "=" * 60)
                    logger.info("ALL CATEGORIES WITH 'TV' IN NAME (for review):")
                    logger.info("=" * 60)
                    for cat in sorted(all_tv_related_categories):
                        logger.info(f"  - {cat}")
                    logger.info("=" * 60)
                    logger.info(f"Total: {len(all_tv_related_categories)} unique TV-related categories")
                    logger.info("Note: Only exact 'TV' and 'TV * Combo' patterns were included in brand discovery.")

                context.close()
                return

            # Determine which brands to scrape
            if args.brands:
                brands = args.brands
                use_discovered_urls = False
            elif args.use_discovered:
                # Get discovered brands (will use full category URL support for scraping)
                discovered_brands_list = database.get_unscraped_brands()
                brands = [b["slug"] for b in discovered_brands_list]
                use_discovered_urls = True
                logger.info(f"Using {len(brands)} discovered brands")
            else:
                brands = get_config(config, "brands", [])
                use_discovered_urls = False

            if args.download_only:
                # Only download pending manuals. With no brand given, work
                # through every brand that has any, not just the configured ones
                if not args.brands:
                    brands = database.get_brands_with_pending()
                    logger.info(f"Downloading pending manuals for all brands: {', '.join(brands)}")
                failures = DownloadFailureTracker(rotate_page)
                for brand in brands:
                    page = download_pending(page, brand, download_dir, captcha_solver, failures)
            else:
                # Get configured categories (defaults to just "tv")
                configured_categories = get_config(config, "categories", ["tv"])

                if use_discovered_urls:
                    # Use discovered brands from database with their saved category URLs
                    for brand_record in discovered_brands_list:
                        brand = brand_record["slug"]
                        # Parse saved category URLs
                        cat_urls_str = brand_record.get("tv_category_urls", "")
                        category_urls = [url.strip() for url in cat_urls_str.split(",") if url.strip()]

                        scrape_brand(page, brand, download_dir, download=not args.index_only, category_urls=category_urls, captcha_solver=captcha_solver, rotate_page=rotate_page)
                        database.mark_brand_scraped(brand_record["id"])
                        random_delay(3, 6)
                else:
                    # Use brands from config or CLI with configured categories
                    for brand in brands:
                        scrape_brand(page, brand, download_dir, download=not args.index_only, categories=configured_categories, captcha_solver=captcha_solver, rotate_page=rotate_page)
                        random_delay(3, 6)
        except DownloadLimitReached:
            logger.info(f"Download limit reached ({DOWNLOAD_LIMIT}). Stopping.")
        finally:
            context.close()

    stats = database.get_stats()
    logger.info(f"ManualsLib scraping complete. Total: {stats['total']}, Downloaded: {stats['downloaded']}, Archived: {stats['archived']}, Pending: {stats['pending']}, Pending archive: {stats['pending_archive']}")


if __name__ == "__main__":
    main()
