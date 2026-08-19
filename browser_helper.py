#!/usr/bin/env python3
"""Browser helper for launching browsers with stealth and extension support."""

import logging
import os
import secrets
import tempfile
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Playwright, Page
from playwright_stealth import Stealth

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)


PROXY_SESSION_PLACEHOLDER = "{session}"


def new_proxy_session_id() -> str:
    """Generate a random session ID for proxy providers with session-based IP rotation."""
    return secrets.token_hex(6)


def proxy_supports_rotation() -> bool:
    """
    True if the proxy config contains a {session} placeholder, meaning a browser
    relaunch will get a fresh session ID and therefore a new exit IP.
    """
    return (
        PROXY_SESSION_PLACEHOLDER in os.environ.get("PROXY_URL", "")
        or PROXY_SESSION_PLACEHOLDER in os.environ.get("PROXY_USER", "")
    )


def resolve_proxy_user(user: str) -> str:
    """
    Substitute the {session} placeholder in a proxy username with a fresh
    random session ID. Providers assign a new exit IP per session ID, e.g.:
      Decodo:      user-USERNAME-session-{session}      (gate.decodo.com:7000)
      Oxylabs:     customer-USERNAME-sessid-{session}
      Bright Data: brd-customer-ID-zone-ZONE-session-{session}
    """
    if PROXY_SESSION_PLACEHOLDER in user:
        session_id = new_proxy_session_id()
        user = user.replace(PROXY_SESSION_PLACEHOLDER, session_id)
        logger.info(f"Using proxy session: {session_id}")
    return user


def get_proxy_config() -> dict | None:
    """
    Get proxy config from environment variables.

    Works with any HTTP proxy (Decodo, Oxylabs, Bright Data, etc.)
    Set PROXY_URL (e.g. http://user:pass@host:port) in .env, or the individual
    PROXY_HOST, PROXY_PORT, PROXY_USER, PROXY_PASS variables. PROXY_URL takes
    precedence over the individual variables.

    A {session} placeholder in the username is replaced with a fresh random
    session ID on every call, so each browser launch gets a new exit IP.

    Returns a proxy dict for Playwright, or None if not configured.
    """
    proxy_url = os.environ.get("PROXY_URL")
    if proxy_url:
        parsed = urllib.parse.urlsplit(proxy_url)
        if not parsed.hostname:
            logger.warning("PROXY_URL is set but has no hostname - ignoring it")
        else:
            server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
            if parsed.port:
                server += f":{parsed.port}"
            config = {"server": server}
            if parsed.username:
                config["username"] = resolve_proxy_user(urllib.parse.unquote(parsed.username))
                config["password"] = urllib.parse.unquote(parsed.password or "")
            return config

    host = os.environ.get("PROXY_HOST")
    port = os.environ.get("PROXY_PORT")
    user = os.environ.get("PROXY_USER")
    password = os.environ.get("PROXY_PASS")

    if host and port and user and password:
        return {
            "server": f"http://{host}:{port}",
            "username": resolve_proxy_user(user),
            "password": password,
        }

    return None

# Common ad-blocking patterns as fallback
AD_PATTERNS = [
    "*://*.doubleclick.net/*",
    "*://*.googlesyndication.com/*",
    "*://*.googleadservices.com/*",
    "*://*.google-analytics.com/*",
    "*://*.googletagmanager.com/*",
    "*://*.googletagservices.com/*",
    "*://*.adservice.google.com/*",
    "*://*.pagead2.googlesyndication.com/*",
    "*://pagead2.googlesyndication.com/*",
    "*://tpc.googlesyndication.com/*",
    "*://*.adsensecustomsearchads.com/*",
    "*://*.adnxs.com/*",
    "*://*.adsrvr.org/*",
    "*://*.amazon-adsystem.com/*",
    "*://*.facebook.net/*",
    "*://*.moatads.com/*",
    "*://*.rubiconproject.com/*",
    "*://*.pubmatic.com/*",
    "*://*.criteo.com/*",
    "*://*.outbrain.com/*",
    "*://*.taboola.com/*",
]


def launch_browser_with_extension(
    playwright: Playwright,
    extension_path: Path | str | None = None,
    headless: bool = False,
    user_data_dir: Path | str | None = None,
    viewport: dict = None,
    user_agent: str = None,
    use_proxy: bool = True,
    browser: str = "chromium",
) -> BrowserContext:
    """
    Launch a browser with optional extension and proxy support.

    To use extensions, Playwright requires a persistent context with Chromium.
    Extensions are NOT supported in Firefox or WebKit.

    Args:
        playwright: Playwright instance
        extension_path: Path to unpacked extension directory (must have manifest.json)
        headless: Run in headless mode (extensions may not work in headless)
        user_data_dir: Browser profile directory (created if None)
        viewport: Viewport dimensions dict {"width": 1280, "height": 800}
        user_agent: Custom user agent string
        use_proxy: If True, use Bright Data Web Unlocker proxy if configured
        browser: Browser to use - "chromium", "firefox", or "webkit"

    Returns:
        BrowserContext
    """
    if viewport is None:
        viewport = {"width": 1280, "height": 800}

    if user_agent is None:
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    # Create user data dir if not provided
    if user_data_dir is None:
        user_data_dir = Path(tempfile.mkdtemp(prefix="playwright_profile_"))
    else:
        user_data_dir = Path(user_data_dir)
        user_data_dir.mkdir(parents=True, exist_ok=True)

    # Get the browser type
    browser = browser.lower()
    if browser == "firefox":
        browser_type = playwright.firefox
    elif browser == "webkit":
        browser_type = playwright.webkit
    else:
        browser_type = playwright.chromium
        browser = "chromium"  # Normalize

    logger.info(f"Using browser: {browser}")

    # Build browser args (extensions only work with Chromium)
    browser_args = []
    extension_loaded = False

    if extension_path and browser == "chromium":
        extension_path = Path(extension_path)
        if extension_path.exists() and (extension_path / "manifest.json").exists():
            ext_path_str = str(extension_path.absolute())
            browser_args.extend([
                f"--disable-extensions-except={ext_path_str}",
                f"--load-extension={ext_path_str}",
            ])
            logger.info(f"Loading extension from: {ext_path_str}")
            extension_loaded = True
        else:
            logger.warning(f"Extension path not valid: {extension_path}")
    elif extension_path and browser != "chromium":
        logger.warning(f"Extensions are only supported in Chromium, not {browser}")

    # Get proxy configuration
    proxy = get_proxy_config() if use_proxy else None
    if proxy:
        logger.info(f"Browser using proxy: {proxy['server']}")

    # Launch persistent context
    context = browser_type.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=headless,
        args=browser_args if browser == "chromium" else [],
        viewport=viewport,
        user_agent=user_agent,
        proxy=proxy,
        # Grant permissions commonly needed
        permissions=["geolocation"],
        # Reduce detection
        ignore_https_errors=True,
        # Enable download interception
        accept_downloads=True,
    )

    return context, extension_loaded


def apply_stealth(page: Page) -> None:
    """
    Apply stealth patches to a page to avoid fingerprint detection.

    This patches various browser APIs to make automation less detectable:
    - Removes navigator.webdriver
    - Fixes Chrome runtime
    - Fixes permissions API
    - Fixes plugins/mimeTypes
    - And many other evasion techniques
    """
    stealth = Stealth()
    stealth.apply_stealth_sync(page)
    logger.info("Stealth patches applied to page")


def setup_route_ad_blocking(page: Page) -> None:
    """
    Set up route-based ad blocking on a page.

    This is a fallback when extension-based blocking isn't available.
    """
    def block_ads(route):
        route.abort()

    for pattern in AD_PATTERNS:
        page.route(pattern, block_ads)

    logger.info("Route-based ad blocking enabled")


def setup_bandwidth_saving(page: Page) -> None:
    """
    Block bandwidth-heavy domains and ads to save proxy costs.

    Blocks static content (images, thumbnails) and ad networks.
    """
    blocked_domains = [
        # ManualsLib static content
        "static-data2.manualslib.com",
        # Google Ads
        "doubleclick.net",
        "googlesyndication.com",
        "googleadservices.com",
        "google-analytics.com",
        "googletagmanager.com",
        "googletagservices.com",
        "adservice.google.com",
        "pagead2.googlesyndication.com",
        "adsensecustomsearchads.com",
        # Other ad networks
        "adnxs.com",
        "adsrvr.org",
        "amazon-adsystem.com",
        "facebook.net",
        "moatads.com",
        "rubiconproject.com",
        "pubmatic.com",
        "criteo.com",
        "outbrain.com",
        "taboola.com",
    ]

    def handle_route(route):
        url = route.request.url
        for domain in blocked_domains:
            if domain in url:
                logger.debug(f"Blocking: {url[:60]}...")
                route.abort()
                return
        route.continue_()

    # Match all requests and filter in the handler
    page.route("**/*", handle_route)

    logger.info("Bandwidth-saving mode enabled (blocking static content and ads)")


def get_extension_path(config: dict, project_dir: Path) -> Path | None:
    """
    Get the extension path from config or default location.

    Config options:
    - ublock_origin_path: Explicit path to extension
    - extensions_dir: Directory containing extensions (default: ./extensions)

    Returns path to uBlock Origin extension or None.
    """
    # Check explicit path first
    if config.get("ublock_origin_path"):
        path = Path(config["ublock_origin_path"])
        if path.exists():
            return path

    # Check extensions directory
    extensions_dir = Path(config.get("extensions_dir", project_dir / "extensions"))
    ublock_path = extensions_dir / "ublock_origin"

    if ublock_path.exists() and (ublock_path / "manifest.json").exists():
        return ublock_path

    return None
