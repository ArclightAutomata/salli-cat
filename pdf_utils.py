"""PDF utility functions."""

import logging
import re
from pathlib import Path

import pikepdf

logger = logging.getLogger(__name__)

# The footer ManualsLib stamps on every page of the actual manual: a q...Q
# block drawing "Downloaded from www.Manualslib.com manuals search engine".
MANUALSLIB_FOOTER_RE = re.compile(r'q\s*\n0 0 \d+ \d+ re.*?manuals search engine.*?Q\s*\n?', re.DOTALL)

# Sanity cap on how many wrapper pages we'll remove at either end. The front
# wrapper's table of contents runs to 16+ pages for large service manuals.
MAX_WRAPPER_PAGES = 40

# ManualsLib renders its wrapper pages with wkhtmltopdf (Qt), whose content
# streams begin with this graphics-state preamble (the cover page wraps it in
# an extra q); the manual's own pages never do.
WKHTMLTOPDF_PREAMBLE_RE = re.compile(r'^\s*(?:q\s+)?/GSa gs')


def _page_content(page) -> str:
    """
    The page's decoded content stream, or '' if it has none or cannot be
    decoded (a damaged stream in the source file). An unreadable page is
    left exactly as it is; the rest of the file is still processed.
    """
    contents = page.get('/Contents')
    if contents is None:
        return ''
    try:
        if isinstance(contents, pikepdf.Array):
            data = b''.join(c.read_bytes() for c in contents)
        else:
            data = contents.read_bytes()
    except Exception as e:
        logger.warning(f"Skipping page with undecodable content stream: {e}")
        return ''
    return data.decode('latin-1')


def _is_manualslib_url(uri) -> bool:
    # Any manualslib domain: the wrapper's back page links .de, .es, .fr, ...
    return 'manualslib.' in str(uri).lower()


def _links_to_manualslib(page) -> bool:
    for annot in page.get('/Annots', []):
        try:
            if _is_manualslib_url(annot['/A']['/URI']):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def find_manualslib_wrapper_pages(pdf: pikepdf.Pdf) -> tuple[int, int]:
    """
    Count the wrapper pages ManualsLib adds around a manual: a cover with
    "Quick Links", a table of contents (together a few pages) at the front,
    and an "Other ManualsLib Projects" page at the back.

    Detection is structural rather than textual. A page is a wrapper
    candidate if it was rendered with wkhtmltopdf (see WKHTMLTOPDF_PREAMBLE_RE)
    or, in files where ManualsLib stamped its footer watermark on the
    manual's pages, if it lacks that footer. Candidates are only removed as a
    run at the very start (which must begin with a page linking to
    manualslib) or very end (which must contain such a link), and never the
    whole file. Returns (leading, trailing) page counts.
    """
    contents = [_page_content(page) for page in pdf.pages]
    footer = [bool(MANUALSLIB_FOOTER_RE.search(text)) for text in contents]
    has_footers = any(footer)
    candidate = [
        bool(WKHTMLTOPDF_PREAMBLE_RE.match(text)) or (has_footers and not footer[i])
        for i, text in enumerate(contents)
    ]
    n = len(candidate)

    leading = 0
    while leading < n and candidate[leading]:
        leading += 1
    trailing = 0
    while trailing < n - leading and candidate[n - 1 - trailing]:
        trailing += 1
    if leading + trailing >= n:
        return 0, 0  # the whole file matches - this isn't a wrapped manual

    if not (0 < leading <= MAX_WRAPPER_PAGES and _links_to_manualslib(pdf.pages[0])):
        leading = 0
    if not (0 < trailing <= MAX_WRAPPER_PAGES and any(_links_to_manualslib(page) for page in pdf.pages[n - trailing:])):
        trailing = 0
    return leading, trailing


def _prune_dangling_outlines(pdf: pikepdf.Pdf) -> None:
    """Drop bookmarks that no longer point at a page (the wrapper adds one for its cover)."""
    if '/Outlines' not in pdf.Root:
        return
    pages = {page.objgen for page in pdf.pages}

    def resolves(item) -> bool:
        try:
            dest = item.destination
            if dest is None and item.action is not None and item.action.get('/S') == '/GoTo':
                dest = item.action.get('/D')
            return isinstance(dest, pikepdf.Array) and len(dest) > 0 and dest[0].objgen in pages
        except Exception:
            return False

    def prune(items) -> None:
        items[:] = [item for item in items if resolves(item)]
        for item in items:
            prune(item.children)

    with pdf.open_outline() as outline:
        prune(outline.root)
        empty = not outline.root
    if empty:
        del pdf.Root['/Outlines']


def strip_manualslib_watermark(pdf_path: Path | str) -> bool:
    """
    Remove ManualsLib's additions from a PDF file in-place.

    These are:
    - Wrapper pages: a cover / table of contents at the front and an
      "Other ManualsLib Projects" page at the back (see
      find_manualslib_wrapper_pages)
    - On every page: the footer "Downloaded from www.Manualslib.com manuals
      search engine" and a link annotation to manualslib.com

    Args:
        pdf_path: Path to the PDF file

    Returns:
        True if watermark was found and removed, False otherwise
    """
    pdf_path = Path(pdf_path)
    modified = False

    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)

        leading, trailing = find_manualslib_wrapper_pages(pdf)
        if trailing:
            del pdf.pages[len(pdf.pages) - trailing:]
        if leading:
            del pdf.pages[:leading]
        if leading or trailing:
            _prune_dangling_outlines(pdf)
            modified = True
            logger.info(f"Removed {leading} leading and {trailing} trailing ManualsLib wrapper pages from {pdf_path}")

        for page in pdf.pages:
            # Remove link annotations to manualslib.com
            if '/Annots' in page:
                new_annots = []
                for annot in list(page['/Annots']):
                    a = annot.get_object() if hasattr(annot, 'get_object') else annot
                    # Check if it's a link to manualslib
                    if '/A' in a and '/URI' in a['/A']:
                        if _is_manualslib_url(a['/A']['/URI']):
                            modified = True
                            continue  # Skip this annotation
                    new_annots.append(annot)

                if new_annots:
                    page['/Annots'] = pikepdf.Array(new_annots)
                else:
                    del page['/Annots']

            # Remove the footer watermark block from the content stream
            text = _page_content(page)
            if text:
                cleaned = MANUALSLIB_FOOTER_RE.sub('', text)

                if cleaned != text:
                    modified = True
                    page['/Contents'] = pdf.make_stream(cleaned.encode('latin-1'))

        if modified:
            # deterministic_id: same input -> byte-identical output, so re-stripping
            # an unchanged original is recognisable by checksum
            pdf.save(pdf_path, deterministic_id=True)
            logger.info(f"Stripped ManualsLib watermark from {pdf_path}")

        pdf.close()

    except Exception as e:
        logger.error(f"Error stripping watermark from {pdf_path}: {e}")
        return False

    return modified
