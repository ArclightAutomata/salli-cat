#!/usr/bin/env python3
"""Scraper for elektrotanya.com service manuals.

Unlike the ManualsLib/ManualsBase/Manualzz scrapers, elektrotanya serves plain
HTML with no Cloudflare challenge, so this scraper uses stdlib urllib rather
than Playwright. Download pages are gated by a reCAPTCHA v2 form (added 2026);
with elektrotanya's agreement we solve it through the shared 2captcha client
(TWOCAPTCHA_API_KEY in .env), the same way the ManualsBase scraper does.

Flow:
  1. Index: GET /showresult?what=<query>&kategoria=<cat>&kat2=all
     Each result row is
       <a href="/<slug>/download.html" title="[<Category> - <DocType>]: <desc>">
     We record one row per result (manual_url = the absolute download-page URL).
  2. Download: fetch the download page. If it shows the reCAPTCHA form
     (hidden `ckey` + a grecaptcha sitekey), solve it via 2captcha and POST
     ckey + g-recaptcha-response back to the page; the response carries the
     JS-built form. Regex out its `fid`, `file` and one-time `dk` token, then
     GET /cgi-bin/download2.cgi?dk=<dk>&fid=<fid>&file=<file> with a
     Referer header. The site allows 10 files/day per IP, enforced at
     download2.cgi (a link fetched through one IP can't be downloaded from
     another), so with `use_proxy` the whole exchange goes through the proxy
     and we rotate to a fresh exit IP every `daily_limit` downloads - and
     whenever the limit page shows up anyway. Without a proxy the run stops
     at the limit.
  3. Service modes: /show_servicemodes is one page listing ~700 service-mode /
     service-menu documents grouped by brand; indexed too when `servicemodes`
     is on.

Config (config.yaml, namespace `elektrotanya`, all optional):
  queries:      list of free-text search terms (max 30 chars each; elektrotanya
                caps the search box at 30). Defaults to the manualslib brand list.
  categories:   list of `kategoria` filter slugs (e.g. tv, monitor). Default ["tv"].
  daily_limit:  the site's per-IP ceiling (default 10). Without a proxy it caps
                the run; with one it sets the rotation cadence.
  use_proxy:    route every request through PROXY_URL (see .env.example); with
                a {session} placeholder the exit IP rotates.
  servicemodes: also index /show_servicemodes (default true).
"""

import argparse
import os
import hashlib
import html
import http.cookiejar
import logging
import random
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
from dotenv import load_dotenv

import database
from browser_helper import get_proxy_url, proxy_supports_rotation
from captcha_solver import TwoCaptchaSolver

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://elektrotanya.com"
SOURCE = "elektrotanya"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
SEARCH_MAXLEN = 30  # the site's search box maxlength

# One result row: href to a /<slug>/download.html page, plus a title carrying
# "[<Category> - <DocType>]: <description>".
ROW_RE = re.compile(
    r'<a href="(/[^"]+?/download\.html)"[^>]*title="\[([^\]]+)\]:\s*([^"]+?)"',
    re.IGNORECASE,
)
# The download page builds its form in JS: fid + file live in hidden inputs.
FID_RE = re.compile(r"name=\\?[\"']fid\\?[\"']\s+value=\\?[\"'](\d+)\\?[\"']")
FILE_RE = re.compile(r"name=\\?[\"']file\\?[\"']\s+value=\\?[\"']([^\"'\\]+)")
# Since the 2026 reCAPTCHA gate the unlocked form also carries a one-time dk
# token; download2.cgi answers "Client error!" without it.
DK_RE = re.compile(r"name=\\?[\"']dk\\?[\"']\s+value=\\?[\"']([^\"'\\]*)")

# The reCAPTCHA gate on download pages: a hidden ckey input and the sitekey
# passed to grecaptcha.render().
CKEY_RE = re.compile(r"name=['\"]ckey['\"]\s+value=['\"]([^'\"]+)['\"]")
SITEKEY_RE = re.compile(r'"sitekey"\s*:\s*"([^"]+)"')

SERVICEMODES_URL = f"{BASE_URL}/show_servicemodes"
# The service-modes page is one table: brand header rows (<b>brand</b>)
# followed by one link row per file, hrefs relative without a leading slash.
SERVICEMODES_RE = re.compile(r"<b>([^<]+)</b>|<a href=\"/?([^\"/]+)/download\.html\"", re.IGNORECASE)

# Give up after this many consecutive downloads hit the limit page on freshly
# rotated IPs - at that point it isn't the per-IP limit.
MAX_CONSECUTIVE_ROTATION_FAILURES = 5

DELAY_MIN = 2.0
DELAY_MAX = 5.0
USE_PROXY = False


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_config(config: dict, key: str, default=None, namespace: str = SOURCE):
    """Namespace override then global fallback (matches the other scrapers)."""
    ns_config = config.get(namespace, {})
    if key in ns_config:
        return ns_config[key]
    return config.get(key, default)


def random_delay(min_sec: float = None, max_sec: float = None):
    min_sec = DELAY_MIN if min_sec is None else min_sec
    max_sec = DELAY_MAX if max_sec is None else max_sec
    time.sleep(random.uniform(min_sec, max_sec))


class HttpSession:
    """
    A cookie jar plus opener, optionally bound to one proxy exit IP.

    The download endpoint (/cgi-bin/download2.cgi) rejects requests that don't
    carry the session cookies (_captchaq, _experience, be) set when the download
    page is fetched, and only serves files to the IP that fetched the page - so
    get_download_info() and the subsequent cgi GET must share one session.
    With a {session} placeholder in the proxy username each HttpSession gets
    its own exit IP; `downloads` counts files pulled through it.
    """

    def __init__(self, use_proxy: bool):
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        self.proxy_url = get_proxy_url() if use_proxy else None
        if use_proxy and not self.proxy_url:
            logger.warning("use_proxy is on but no PROXY_URL/PROXY_* configured - connecting directly")
        if self.proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy_url, "https": self.proxy_url}))
        self.opener = urllib.request.build_opener(*handlers)
        self.downloads = 0


_session: HttpSession | None = None


def get_session() -> HttpSession:
    global _session
    if _session is None:
        _session = HttpSession(USE_PROXY)
    return _session


def new_session() -> HttpSession:
    """Drop cookies and, with a rotating proxy, move to a fresh exit IP."""
    global _session
    _session = HttpSession(USE_PROXY)
    return _session


_captcha_solver: TwoCaptchaSolver | None = None


def get_captcha_solver() -> TwoCaptchaSolver | None:
    """The shared 2captcha client, or None when TWOCAPTCHA_API_KEY is unset."""
    global _captcha_solver
    if _captcha_solver is None and os.environ.get("TWOCAPTCHA_API_KEY"):
        _captcha_solver = TwoCaptchaSolver(os.environ["TWOCAPTCHA_API_KEY"])
    return _captcha_solver


def _http_post(url: str, fields: dict, referer: str = None, timeout: int = 30) -> tuple[bytes, str]:
    """POST form fields through the shared cookie jar. Returns (body_bytes, content_type)."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": BASE_URL,
    }
    if referer:
        headers["Referer"] = referer
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with get_session().opener.open(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def _http_get(url: str, referer: str = None, timeout: int = 30) -> tuple[bytes, str]:
    """GET a URL through the shared cookie jar. Returns (body_bytes, content_type)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with get_session().opener.open(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def get_sha1_storage_path(download_dir: Path, sha1: str, extension: str = ".pdf") -> Path:
    """git-style trie path ab/cd/abcdef... — identical to the other scrapers."""
    if len(sha1) < 4:
        raise ValueError(f"SHA1 hash too short: {sha1}")
    return download_dir / sha1[:2] / sha1[2:4] / (sha1 + extension)


def compute_checksums_bytes(data: bytes) -> tuple[str, str]:
    return hashlib.sha1(data).hexdigest(), hashlib.md5(data).hexdigest()


def parse_doc_type(meta: str) -> str:
    """From a '[TV - Service Manual]' bracket, take the part after the dash."""
    if " - " in meta:
        return meta.split(" - ", 1)[1].strip()
    return meta.strip()


def parse_category(meta: str) -> str:
    return meta.split(" - ", 1)[0].strip() if " - " in meta else meta.strip()


def guess_brand(description: str) -> str:
    """First token of the description is reliably the brand on elektrotanya."""
    desc = description.strip()
    return desc.split()[0].lower() if desc else "unknown"


def clean_model(description: str) -> str:
    """Human-readable model/title: drop a trailing file extension."""
    return re.sub(r"\.[a-z0-9]{1,5}$", "", description.strip(), flags=re.I)


def search(query: str, kategoria: str = "tv") -> list[dict]:
    """Run one search and return parsed result rows."""
    params = {"what": query[:SEARCH_MAXLEN], "kategoria": kategoria, "kat2": "all"}
    url = f"{BASE_URL}/showresult?" + urllib.parse.urlencode(params)
    try:
        body, _ = _http_get(url)
    except Exception as e:  # noqa: BLE001
        logger.warning("search failed for %r (%s): %s", query, kategoria, e)
        return []

    text = body.decode("utf-8", errors="replace")
    results = []
    seen = set()
    for href, meta, desc in ROW_RE.findall(text):
        if href in seen:
            continue
        seen.add(href)
        desc = html.unescape(desc)
        # The slug (the site's file name) is elektrotanya's only stable key for
        # a manual; it becomes source_id and the archive.org identifier.
        slug = href.removeprefix("/").removesuffix("/download.html")
        results.append(
            {
                "manual_url": BASE_URL + href,
                "slug": slug,
                "category": parse_category(meta),
                "doc_type": parse_doc_type(meta),
                "brand": guess_brand(desc),
                "model": clean_model(desc),
            }
        )
    return results


def get_download_info(download_page_url: str) -> tuple[str, str, str] | None:
    """Fetch a download page and pull (fid, filename, dk) from its JS form."""
    try:
        body, _ = _http_get(download_page_url)
    except Exception as e:  # noqa: BLE001
        logger.warning("download page fetch failed %s: %s", download_page_url, e)
        return None
    text = body.decode("utf-8", errors="replace")
    fid = FID_RE.search(text)
    fname = FILE_RE.search(text)
    if not (fid and fname):
        text = _pass_captcha(download_page_url, text)
        if text is None:
            return None
        fid = FID_RE.search(text)
        fname = FILE_RE.search(text)
    if not fid or not fname:
        logger.warning("no fid/file on %s", download_page_url)
        return None
    dk = DK_RE.search(text)
    return fid.group(1), fname.group(1), (dk.group(1) if dk else "")


def _pass_captcha(download_page_url: str, text: str) -> str | None:
    """Solve the page's reCAPTCHA gate via 2captcha and return the unlocked page.

    Returns None when the page has no recognisable gate, no solver is
    configured, or the solve/submit fails.
    """
    ckey = CKEY_RE.search(text)
    sitekey = SITEKEY_RE.search(text)
    if not ckey or not sitekey:
        return None
    solver = get_captcha_solver()
    if solver is None:
        logger.warning("reCAPTCHA on %s but TWOCAPTCHA_API_KEY is not set", download_page_url)
        return None
    logger.info("solving reCAPTCHA for %s via 2captcha", download_page_url)
    token = solver.solve_recaptcha(sitekey.group(1), download_page_url)
    if not token:
        logger.warning("2captcha returned no token for %s", download_page_url)
        return None
    try:
        body, _ = _http_post(
            download_page_url,
            {"ckey": ckey.group(1), "g-recaptcha-response": token},
            referer=download_page_url,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("captcha submit failed %s: %s", download_page_url, e)
        return None
    return body.decode("utf-8", errors="replace")


def parse_servicemodes(text: str) -> list[dict]:
    """Rows for every file on the service-modes page, brand taken from the headers."""
    rows, brand = [], "unknown"
    for header, slug in SERVICEMODES_RE.findall(text):
        if header:
            brand = html.unescape(header).strip().lower() or brand
            continue
        slug = html.unescape(slug)
        rows.append(
            {
                "manual_url": f"{BASE_URL}/{slug}/download.html",
                "slug": slug,
                "category": "TV",
                "doc_type": "Service Mode",
                "brand": brand,
                "model": clean_model(slug).replace("_", " "),
            }
        )
    return rows


def index_servicemodes() -> int:
    """Index the /show_servicemodes list. Returns new count."""
    try:
        body, _ = _http_get(SERVICEMODES_URL)
    except Exception as e:  # noqa: BLE001
        logger.warning("service-modes page fetch failed: %s", e)
        return 0
    rows = parse_servicemodes(body.decode("utf-8", errors="replace"))
    added = sum(1 for row in rows if _add_row(row))
    logger.info("service modes: %d listed, %d new", len(rows), added)
    return added


def _add_row(row: dict) -> int | None:
    return database.add_manual(
        brand=row["brand"],
        model=row["model"],
        manual_url=row["manual_url"],
        doc_type=row["doc_type"],
        source=SOURCE,
        source_id=row["slug"],
        category=row["category"],
    )


def index(queries: list[str], categories: list[str]) -> int:
    """Search every query×category, add new rows to the DB. Returns new count."""
    added = 0
    for kategoria in categories:
        for query in queries:
            rows = search(query, kategoria)
            logger.info("search %r [%s]: %d results", query, kategoria, len(rows))
            added += sum(1 for row in rows if _add_row(row))
            random_delay()
    logger.info("indexed %d new elektrotanya manuals", added)
    return added


def download_manual(manual: dict, download_dir: Path) -> bool:
    """Download one manual's PDF. Returns True on success, False on limit/error.

    A False return from a genuine daily-limit hit is signalled by raising
    DailyLimitReached so the caller can stop the whole run.
    """
    info = get_download_info(manual["manual_url"])
    if not info:
        return False
    fid, filename, dk = info
    random_delay()

    dl_url = f"{BASE_URL}/cgi-bin/download2.cgi?" + urllib.parse.urlencode(
        {"dk": dk, "fid": fid, "file": filename}
    )
    try:
        data, content_type = _http_get(dl_url, referer=manual["manual_url"], timeout=120)
    except Exception as e:  # noqa: BLE001
        logger.warning("download failed for fid=%s: %s", fid, e)
        return False

    # A PDF starts with %PDF; djvu/zip/rar are also valid manual payloads. An
    # HTML body means the daily limit (or an error) — stop the run.
    is_binary = "text/html" not in content_type.lower() and not data[:512].lstrip().startswith(b"<")
    looks_like_doc = data[:5] == b"%PDF-" or data[:4] in (b"AT&T", b"PK\x03\x04", b"Rar!") or is_binary
    if not looks_like_doc:
        raise DailyLimitReached(
            f"non-file response for fid={fid} (content-type {content_type!r}); "
            "likely the 10-files/day limit"
        )

    # Keep the site's extension (pdf, djvu, zip, rar, txt, doc, hex, ...)
    ext = Path(filename).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        ext = ".pdf" if data[:5] == b"%PDF-" else ".bin"

    sha1, md5 = compute_checksums_bytes(data)
    final_path = get_sha1_storage_path(download_dir, sha1, ext)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(data)

    database.update_downloaded(
        manual["id"],
        str(final_path),
        file_sha1=sha1,
        file_md5=md5,
        file_size=len(data),
        original_filename=filename,
    )
    logger.info("downloaded %s (%s, %d bytes)", filename, sha1[:8], len(data))
    return True


class DailyLimitReached(Exception):
    """Raised when elektrotanya's 10-files/day limit is hit."""


def main():
    parser = argparse.ArgumentParser(description="Scrape elektrotanya.com manuals")
    parser.add_argument("--index-only", action="store_true", help="Only build the index, don't download")
    parser.add_argument("--download-only", action="store_true", help="Only download pending manuals")
    parser.add_argument("--query", nargs="*", help="Search terms (overrides config queries)")
    parser.add_argument("--kategoria", nargs="*", help="Category filters (overrides config)")
    parser.add_argument("--limit", type=int, default=None, help="Max downloads this run (default: daily_limit)")
    parser.add_argument("--clear", action="store_true", help="Clear elektrotanya records before scraping")
    args = parser.parse_args()

    database.init_db()
    config = load_config()

    global DELAY_MIN, DELAY_MAX, USE_PROXY
    DELAY_MIN = float(get_config(config, "delay_min", 2.0))
    DELAY_MAX = float(get_config(config, "delay_max", 5.0)) or DELAY_MIN
    USE_PROXY = bool(get_config(config, "use_proxy", False))
    rotate = USE_PROXY and proxy_supports_rotation()
    if USE_PROXY:
        logger.info("proxy enabled for all elektrotanya requests%s", " with IP rotation" if rotate else "")
        if not rotate:
            logger.info("Tip: put {session} in the proxy username to rotate past the per-IP daily limit")

    if args.clear:
        database.clear_manuals_by_source(SOURCE)
        logger.info("cleared existing elektrotanya records")

    # Default queries: the CRT-relevant brand list already configured for
    # ManualsLib, so the two sources cover the same brands.
    default_queries = get_config(config, "brands", [], namespace="manualslib") or [
        "sony", "panasonic", "rca", "jvc", "toshiba", "sharp", "hitachi", "zenith",
    ]
    queries = args.query or get_config(config, "queries", default_queries)
    categories = args.kategoria or get_config(config, "categories", ["tv"])
    per_ip_limit = int(get_config(config, "daily_limit", 10))
    # Without rotation the per-IP limit is the run limit; with it, only --limit caps the run
    run_limit = args.limit if args.limit is not None else (None if rotate else per_ip_limit)

    download_dir = Path(get_config(config, "download_dir", "./downloads"))

    if not args.download_only:
        index(queries, categories)
        if get_config(config, "servicemodes", True):
            index_servicemodes()

    if args.index_only:
        return

    pending = database.get_undownloaded_manuals(source=SOURCE)
    logger.info("%d elektrotanya manuals pending download; run limit %s", len(pending),
                run_limit if run_limit is not None else "none (rotating IPs)")

    done = 0
    limit_hits = 0  # consecutive limit pages, each on a freshly rotated IP
    stop = False
    for manual in pending:
        if run_limit is not None and done >= run_limit:
            logger.info("hit run limit of %d downloads; stopping", run_limit)
            break
        if rotate and get_session().downloads >= per_ip_limit:
            logger.info("%d downloads on this IP; rotating to a fresh one", per_ip_limit)
            new_session()
        # Retry the same manual on a new IP each time the limit page shows up
        while True:
            try:
                if download_manual(manual, download_dir):
                    done += 1
                    limit_hits = 0
                    get_session().downloads += 1
                break
            except DailyLimitReached as e:
                if not rotate:
                    logger.warning("%s — stopping (try again tomorrow or enable use_proxy)", e)
                    stop = True
                    break
                limit_hits += 1
                if limit_hits >= MAX_CONSECUTIVE_ROTATION_FAILURES:
                    logger.error("%d consecutive limit pages on fresh IPs - not the per-IP limit; stopping", limit_hits)
                    stop = True
                    break
                logger.warning("%s — rotating proxy IP and retrying (%d/%d)", e, limit_hits, MAX_CONSECUTIVE_ROTATION_FAILURES)
                new_session()
                random_delay()
        if stop:
            break
        random_delay()

    logger.info("elektrotanya run complete: %d downloaded", done)


if __name__ == "__main__":
    main()
