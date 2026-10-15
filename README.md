# Salli Cat

<a href="https://www.youtube.com/watch?v=nVBalxt3NEU" target="_blank"><img src="nedm-cat.jpg" alt="Salli Cat"></a>

*From "Salvator Librorum Cathodicorum" - Savior of Cathode Books*

A Python-based tool for preserving CRT manuals on the [Internet Archive](https://archive.org). Scrapes manuals from multiple sources, downloads PDFs locally, and uploads them to archive.org for long-term preservation and public access.

[![asciicast](https://asciinema.org/a/XKBnNY8aKwODqJhFiXRbTAzyo.svg)](https://asciinema.org/a/XKBnNY8aKwODqJhFiXRbTAzyo?speed=1.7&idleTimeLimit=1)

## Features

- **Multi-source scraping**: Download manuals from ManualsLib, ManualsBase, and Manualzz
- **Automatic CAPTCHA solving**: Integration with 2captcha for hands-free operation
- **Internet Archive uploading**: Bulk upload to archive.org with proper metadata and deduplication
- **Web dashboard**: Monitor scraping progress, filter by source/brand, track upload status
- **Content-addressable storage**: PDFs stored by SHA1 hash to avoid duplicates
- **Resume capability**: Interrupted scrapes and uploads can be resumed

## Supported Sources

| Site | Status | Notes |
|------|--------|-------|
| **ManualsLib** (manualslib.com) | :white_check_mark: Full automation | Brand discovery and CRT manual scraping. Recommended: use both a reCAPTCHA solver (e.g. 2captcha) and a residential proxy. |
| **ManualsBase** (manualsbase.com) | :white_check_mark: Full automation | Scrapes all brands with TV-related categories. Recommended: use a reCAPTCHA solver. Proxy not necessary. |
| **Manualzz** (manualzz.com) | :warning: Partial support | CRT TV and monitor manual scraping. Works with manual captcha solving, but Cloudflare managed challenges prevent full automation. |
| **Manualzilla** (manualzilla.com) | :x: Not yet implemented | TODO. Likely to have same Cloudflare challenges as Manualzz. |

## Project Structure

```
salli-cat/
├── pyproject.toml        # Project config and dependencies
├── cli.py                # Main CLI entry point (salli command)
├── config.yaml           # Brand list and URL configuration
├── .env                  # Environment variables (API keys) - not in git
├── .env.example          # Example environment file
├── Procfile              # Process definitions for honcho
├── manualslib_scraper.py # ManualsLib Playwright scraper
├── manualsbase_scraper.py # ManualsBase Playwright scraper
├── manualzz_scraper.py   # Manualzz Playwright scraper
├── archive_checker.py    # Background archive.org checker
├── ia_uploader.py        # Internet Archive uploader
├── captcha_solver.py     # 2captcha integration for auto-solving
├── browser_helper.py     # Browser launch helper with extension support
├── database.py           # SQLite database layer
├── dashboard.py          # Flask web dashboard
├── templates/
│   └── index.html        # Dashboard UI
├── downloads/            # Downloaded PDFs (SHA1-based storage)
└── manuals.db            # SQLite database (created on first run)
```

## Setup

This project uses [uv](https://docs.astral.sh/uv/) for Python version and dependency management.

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Sync dependencies (creates .venv automatically)
uv sync

# Install Playwright browsers
uv run patchright install chromium
```

## Usage

All commands are run through the `salli` CLI. Use `uv run salli` to run commands:

```bash
# Show all available commands
uv run salli --help

# Show current status
uv run salli status
```

### Running the Dashboard

```bash
uv run salli dashboard
```

Then open http://localhost:5000 in your browser to monitor scraping progress, view manuals, and track uploads.

Options:
- `--host` - Host to bind to (default: 127.0.0.1)
- `--port` - Port to bind to (default: 5000)
- `--debug` - Run in debug mode

### Brand Discovery

Automatically discover all brands on ManualsLib that have CRT manuals:

```bash
# Discover all brands with TV category (saves to database)
uv run salli scrape manualslib --discover-brands

# Then scrape all discovered brands
uv run salli scrape manualslib --use-discovered --index-only
```

### Scraping ManualsLib

```bash
# Scrape all brands in config.yaml
uv run salli scrape manualslib

# Scrape specific brands only
uv run salli scrape manualslib --brands rca --brands sharp --brands panasonic

# Scrape all discovered brands (from discover-brands)
uv run salli scrape manualslib --use-discovered

# Build index only (populate database, no downloads)
uv run salli scrape manualslib --index-only

# Download pending manuals only (skip scraping)
uv run salli scrape manualslib --download-only
```

### Scraping ManualsBase

```bash
# Scrape all brands with TV-related categories
uv run salli scrape manualsbase

# Build index only (populate database, no downloads)
uv run salli scrape manualsbase --index-only

# Download pending manuals only (skip scraping)
uv run salli scrape manualsbase --download-only
```

### Scraping Manualzz

```bash
# Scrape catalog URLs from config.yaml
uv run salli scrape manualzz

# Scrape specific catalog URLs
uv run salli scrape manualzz --urls "https://manualzz.com/catalog/..."

# Build index only (populate database, no downloads)
uv run salli scrape manualzz --index-only

# Download pending manuals only (skip scraping)
uv run salli scrape manualzz --download-only
```

### Uploading to Internet Archive

After downloading manuals, upload them to archive.org for public preservation:

```bash
# Preview what would be uploaded (dry run)
uv run salli upload --source manualsbase --dry-run

# Upload all pending manuals from a source
uv run salli upload --source manualsbase
uv run salli upload --source manualslib

# Several uploads at once (a single stream to IA is slow; 4-8 is a good range)
uv run salli upload --workers 6
uv run salli upload --source manualzz

# Limit number of uploads
uv run salli upload --source manualsbase --limit 50
```

**Note**: Requires Internet Archive credentials. Run `uv run ia configure` to set up authentication.

### Checking Archive.org

The archive checker runs as a background process, slowly checking if manuals already exist on archive.org. This pre-identifies archived manuals so they can be skipped during downloads.

```bash
# Check all pending manuals once (with default rate limiting)
uv run salli check-archive

# Run continuously, checking new manuals as they appear
uv run salli check-archive --continuous

# Check with faster rate (be careful not to hit rate limits)
uv run salli check-archive --delay-min 2 --delay-max 5

# Just show current statistics
uv run salli check-archive --stats
```

### Clearing Database Records

```bash
# Clear all records (with confirmation prompt)
uv run salli clear all

# Clear manual records from a specific source
uv run salli clear manuals --source manualslib

# Clear all manual records
uv run salli clear manuals

# Clear discovered brands
uv run salli clear brands
```

### Running Multiple Processes with Honcho

Use the `Procfile` to run the dashboard and archive checker together:

```bash
# Run all processes defined in Procfile
uv run honcho start

# Or run individual processes
uv run honcho start dashboard
uv run honcho start archive_checker
```

## Configuration

Edit `config.yaml` to configure brands, categories, and catalog URLs:

```yaml
# Global settings
download_dir: ./downloads
browser: chromium       # chromium, firefox, or webkit
headless: false         # Run without visible window (manualslib and manual captcha need false:
                        # headless browsers never pass Cloudflare's managed challenge)
stealth: false          # Stealth JS patches - keep off; Cloudflare's managed challenge detects them
virtual_display: true   # Run the headful browser on an Xvfb display (needs `apt install xvfb`):
                        # invisible and never steals focus, yet passes Cloudflare like a real window
use_proxy: true         # Use proxy (requires PROXY_* in .env)
delay_min: 0            # Min delay between requests (seconds)
delay_max: 0            # Max delay between requests (seconds)

# ManualsLib settings
manualslib:
  brands:
    - rca
    - sharp
    - panasonic
  categories:
    - tv              # standalone TVs
    - tv-dvd-combo    # TV/DVD combos
    - tv-vcr-combo    # TV/VCR combos
  # Override global settings for this scraper:
  # use_proxy: false
  # browser: firefox

# ManualsBase settings
manualsbase:
  categories:         # Keywords to match category names
    - tv
    - television
    - monitor
    - crt
    - remote

# Manualzz settings
manualzz:
  urls:
    - https://manualzz.com/catalog/computers+%26+electronics/TVs+%26+monitors/CRT+TVs
    - https://manualzz.com/catalog/computers+%26+electronics/TVs+%26+monitors/monitors+CRT
```

Each scraper has its own namespace (`manualslib`, `manualsbase`, `manualzz`) where you can:
- Set scraper-specific options (brands, categories, urls)
- Override global settings like `use_proxy`, `browser`, `stealth` for that scraper only

## Captcha Handling

ManualsLib and ManualsBase use reCAPTCHA to protect manual downloads. The scrapers support three modes, tried in this order:

### Local Solving (Free, Default)

With `local_captcha: true` (the default) the ManualsLib scraper first tries to pass the reCAPTCHA itself, inside the browser session (`local_captcha.py`):

1. **Checkbox**: it clicks "I'm not a robot". From a home IP in a real headful browser, Google usually accepts this with no challenge at all. Proxy IPs are challenged every time.
2. **Image challenge** (`captcha_image_solver`): "Select all images with a bus" grids are solved with local vision models via [reCognizer](https://github.com/Vinyzu/recognizer) (YOLOv8-seg for the object classes it knows, CLIP for the rest). Each tile is screenshotted and classified; 3x3, 3x3 dynamic (tiles are replaced until none match) and 4x4 "select all squares" grids are handled, and "Please try again" gets a fresh grid, up to 6 per captcha. About 1-2 s per grid on CPU; the ~1 GB of weights are downloaded on first use. Typically 20-25 s per manual through the proxy, no cost.
3. **Audio challenge** (`captcha_audio_solver`): transcribed locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`whisper_model`, default `small.en`; `whisper_device: auto` uses CUDA when available). Google refuses the audio challenge to most proxy IPs ("Try again later"), so this layer mostly matters off-proxy.

Only if all of that fails does it fall through to 2captcha. Note that reCognizer is GPL-3.0 licensed.

### Automatic Solving with 2captcha (Fallback)

Configure 2captcha so that captchas the local solver can't pass are still handled hands-free:

1. Get an API key from [2captcha.com](https://2captcha.com/enterpage)
2. Copy `.env.example` to `.env` and add your API key:
   ```bash
   cp .env.example .env
   # Edit .env and set TWOCAPTCHA_API_KEY=your_api_key_here
   ```
3. The scraper will automatically solve captchas (~$2.99 per 1000 captchas)

When 2captcha is configured, the scraper will:
- Extract the reCAPTCHA sitekey from the page
- Submit to 2captcha and wait for solution (~10-30 seconds)
- Inject the solution token and continue automatically
- Fall back to manual solving if 2captcha fails

### Manual Solving (Last Resort)

If neither local solving nor 2captcha succeeds (or 2captcha is not configured), the scraper falls back to manual solving:

1. **Visible Browser**: Playwright runs in headed mode (you see the browser window)
2. **Detection**: When a captcha appears, the scraper detects it and pauses
3. **Manual Solving**: A message prints to the terminal - solve the captcha in the browser window
4. **Auto-Continue**: Once solved, the scraper automatically detects this and continues
5. **Timeout**: If not solved within 5 minutes, the manual is skipped

```
============================================================
CAPTCHA DETECTED - Please solve it in the browser window
============================================================
```

## Proxy Support

ManualsLib may block your IP after too many requests. The scrapers support HTTP proxies to avoid IP bans.

Configure in `.env`:

```bash
PROXY_HOST=your.proxy.host
PROXY_PORT=7777
PROXY_USER=your_username
PROXY_PASS=your_password
```

Then enable in `config.yaml`:

```yaml
use_proxy: true
```

### Circuit Breaker

The scraper will automatically stop after 3 consecutive download failures to avoid wasting money on CAPTCHA solutions when there's an IP ban or site issue.

## Ad Blocking

The scrapers use Playwright route-based blocking to intercept requests to known ad domains (Google Ads, DoubleClick, etc.). This prevents ads from interfering with the scraping process and reduces bandwidth usage. No configuration required.

## Dashboard Features

- **Summary Stats**: Total manuals, downloaded count, archived count, pending count
- **Stats by Source**: Breakdown of manuals by source (ManualsLib, ManualsBase, Manualzz)
- **Progress by Brand**: Visual progress bars showing completion
- **Manual Table**: Filterable list of all manuals with source badges
- **Filters**: Filter by source, brand, and download status
- **Actions**: Links to view on source site, download locally, or view on archive.org
- **Database Management**: Clear buttons for specific sources or all data
- **Auto-Refresh**: Updates every 30 seconds

## Database Schema

SQLite database (`manuals.db`) with two tables:

### brands table
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| name | TEXT | Brand display name |
| slug | TEXT | URL slug (e.g., "rca") |
| brand_url | TEXT | URL to brand page |
| categories | TEXT | Comma-separated categories |
| scraped | INTEGER | 0 = pending, 1 = scraped |

### manuals table
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| source | TEXT | Source site ("manualslib", "manualsbase", or "manualzz") |
| source_id | TEXT | ID from source site |
| brand | TEXT | Brand name/slug |
| model | TEXT | Model name/number |
| model_url | TEXT | URL to model page |
| model_id | TEXT | ManualsLib model ID |
| doc_type | TEXT | Document type (e.g., "User Manual") |
| doc_description | TEXT | Document description |
| category | TEXT | Category (for manualzz) |
| manual_url | TEXT | URL to manual page |
| manualslib_id | TEXT | ManualsLib document ID |
| downloaded | INTEGER | 0 = pending, 1 = downloaded |
| archived | INTEGER | 0 = not archived, 1 = on archive.org |
| file_path | TEXT | Local path to downloaded PDF (SHA1-based) |
| original_filename | TEXT | Original filename for display/download |
| archive_url | TEXT | URL on archive.org |

## Archive.org Integration

### Checking for Existing Archives

Before downloading, the scraper checks if the manual already exists on archive.org at:
- ManualsLib: `https://archive.org/details/manualslib-id-{manualslib_id}`
- ManualsBase: `https://archive.org/details/manualsbase-id-{source_id}`
- Manualzz: `https://archive.org/details/manualzz-id-{source_id}`

If archived, it skips the download and records the archive URL.

### Uploading to Internet Archive

See [Uploading to Internet Archive](#uploading-to-internet-archive) in the Usage section for commands.

The uploader:
- Creates source-specific identifiers (e.g., `manualsbase-id-12345`)
- Includes checksums (MD5/SHA1) as external identifiers for deduplication
- Skips items that already exist on archive.org - unless the item holds no file
  (left behind by an upload IA refused), which it fills in
- Repairs PDFs that IA rejects as damaged ("Syntax error detected in pdf data"):
  qpdf rebuilds the file and empty form/layer dictionaries are dropped. The result
  becomes the `repaired` variant; the damaged file stays as `original`
- Updates the database with archive URLs after successful upload

**Note**: Requires `internetarchive` library and IA credentials configured via `ia configure`.

## Downloaded Files

PDFs are stored using content-addressable storage based on SHA1 hash (similar to git's object storage). This prevents filename collisions when different manuals have the same suggested filename.

**Storage structure**: `downloads/{sha1[:2]}/{sha1[2:4]}/{sha1}.pdf`

Example:
```
downloads/
├── 3a/
│   └── 7f/
│       └── 3a7f2b9c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a.pdf
├── a1/
│   └── b2/
│       └── a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0.pdf
└── f9/
    └── e8/
        └── f9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0.pdf
```

The original filename (from the server's Content-Disposition header) is preserved in the database. When downloading through the dashboard, files are served with their original filenames.

## Resume Capability

The scraper tracks progress in the database:
- Already-scraped manuals won't be re-added
- Already-downloaded manuals won't be re-downloaded
- Archived manuals are skipped
- Use `--download-only` to resume downloading after interruption
- If the proxy itself refuses requests, for example Decodo's "You've reached
  your current traffic limit", the ManualsLib and Elektrotanya scrapers pause
  and re-check every 5 minutes instead of failing manual after manual. Top up
  the allowance and the run resumes on its own; Ctrl-C stops it as usual.
- A manual whose PDF link is dead on ManualsLib (HTTP 404) is marked
  `unavailable` in the database, together with its sibling rows, and left out
  of pending work so later runs don't spend a captcha on it again. `salli
  status` counts them; `--retry-unavailable` includes them again, and a
  successful download clears the flag.

## Rate Limiting

The scraper includes random delays between requests (2-5 seconds) to avoid overwhelming the server.

## Roadmap

- **Re-strip already-downloaded ManualsLib manuals.** Since mid-February 2026
  ManualsLib has wrapped its PDFs in extra pages (a cover with "Quick Links"
  and a table of contents at the front, an "Other ManualsLib Projects" page at
  the back). The stripper removes them now, but the stripped variants produced
  before this - and the copies uploaded to the Internet Archive - still carry
  them: a sample of the ~5,600 originals downloaded since 2026-02-16 found
  wrapper pages in about 60%. The untouched originals are kept as the
  `original` file variant, so the fix is a `restrip` command that re-derives
  the stripped variant from each original (`pdf_utils.strip_manualslib_watermark`
  is idempotent), updates the variant's checksums, and re-uploads the file to
  the existing IA item. The same applies to marks the stripper learned about
  later: text stamps from the sites a manual passed through before ManualsLib
  (`http://cxema.ru`, `www.chinadse.org` and `all-guides.com` on every page) and full-page advert images prepended to
  the manual (esquemaseletricos.com.br), see `THIRD_PARTY_STAMP_RE`,
  `JUNK_COVER_HASHES` and `STAMP_IMAGE_HASHES` (logo banners drawn as images,
  such as tecnicosaurios.com) in `pdf_utils.py`. New stamps and covers are added
  there as they turn up; `salli restrip` then re-derives the affected files.
