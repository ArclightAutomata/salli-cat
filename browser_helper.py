#!/usr/bin/env python3
"""Browser helper for launching browsers with stealth and extension support."""

import logging
import os
import secrets
import atexit
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv
# patchright is a drop-in Playwright fork that hides the CDP automation leaks
# (Runtime.enable etc.) Cloudflare's managed challenge detects. With stock
# Playwright the "Just a moment..." page never resolves, even when a human
# clicks the checkbox.
from patchright.sync_api import BrowserContext, Playwright, Page
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


VIRTUAL_DISPLAY_SIZE = "1920x1080x24"
_virtual_display: str | None = None


def start_virtual_display() -> str | None:
    """
    Start an Xvfb server for this process (once) and return its DISPLAY string.

    A headful browser on a virtual display is indistinguishable from one on the
    real desktop to the sites it visits (Cloudflare's managed challenge needs a
    headful browser), but nothing appears on screen and no window steals focus.
    Returns None if Xvfb isn't installed.
    """
    global _virtual_display
    if _virtual_display:
        return _virtual_display
    if not shutil.which("Xvfb"):
        logger.warning("Xvfb not found (apt install xvfb) - browser will use the real display")
        return None

    # Pick a display number that no X server is using
    for num in range(90, 200):
        if Path(f"/tmp/.X{num}-lock").exists() or Path(f"/tmp/.X11-unix/X{num}").exists():
            continue
        display = f":{num}"
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", VIRTUAL_DISPLAY_SIZE, "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Wait for the server socket; if it exits, the number was taken after all
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and proc.poll() is None:
            if Path(f"/tmp/.X11-unix/X{num}").exists():
                atexit.register(proc.terminate)
                _virtual_display = display
                logger.info(f"Started virtual display {display} ({VIRTUAL_DISPLAY_SIZE})")
                return display
            time.sleep(0.1)
        proc.terminate()
    logger.warning("Could not start Xvfb - browser will use the real display")
    return None


def launch_browser_with_extension(
    playwright: Playwright,
    extension_path: Path | str | None = None,
    headless: bool = False,
    user_data_dir: Path | str | None = None,
    viewport: dict = None,
    user_agent: str = None,
    use_proxy: bool = True,
    browser: str = "chromium",
    virtual_display: bool = False,
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
        viewport: Viewport dimensions dict; None (default) lets the window size
            rule, which is what a real browser looks like
        user_agent: Custom user agent string; None (default) keeps the browser's
            own. A UA that doesn't match the real Chrome build is a fingerprint
            red flag for Cloudflare, so only set this deliberately.
        use_proxy: If True, use Bright Data Web Unlocker proxy if configured
        browser: Browser to use - "chromium", "firefox", or "webkit"
        virtual_display: Run a headful browser on an Xvfb virtual display so it
            stays off the real desktop (ignored when headless)

    Returns:
        BrowserContext
    """
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

    # Only the browser process gets pointed at the virtual display
    env = None
    if virtual_display and not headless:
        display = start_virtual_display()
        if display:
            env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
            env["DISPLAY"] = display
            if browser == "chromium":
                # No window manager on Xvfb to size the window - do it ourselves
                browser_args.append("--window-size=1920,1080")

    # Launch persistent context
    context = browser_type.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=headless,
        env=env,
        args=browser_args if browser == "chromium" else [],
        viewport=viewport,
        no_viewport=viewport is None,
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


CLOUDFLARE_CHALLENGE_TITLE = "just a moment"


def is_cloudflare_challenge(page: Page) -> bool:
    """True if the page is currently showing Cloudflare's challenge interstitial."""
    try:
        return CLOUDFLARE_CHALLENGE_TITLE in page.title().lower()
    except Exception:
        # Title lookup races with the redirect that follows a passed challenge
        return False


def wait_for_cloudflare(page: Page, timeout: float = 45.0) -> bool:
    """
    Wait for a Cloudflare managed challenge ("Just a moment...") to clear.

    patchright passes the challenge on its own within a few seconds in a headful
    browser; headless browsers never pass it. Returns False if the challenge is
    still up after `timeout` seconds.
    """
    if not is_cloudflare_challenge(page):
        return True
    logger.info("Cloudflare challenge detected, waiting for it to clear...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        if not is_cloudflare_challenge(page):
            # The challenge redirects to the real page; let it settle
            page.wait_for_load_state("domcontentloaded")
            logger.info("Cloudflare challenge cleared")
            return True
    logger.warning(
        f"Cloudflare challenge still showing after {timeout:.0f}s "
        "(headless browsers and stealth patches both fail it - check the "
        "headless and stealth settings)"
    )
    return False


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
