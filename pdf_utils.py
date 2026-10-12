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

# Watermarks stamped by the sites a manual passed through before ManualsLib
# got it. Each is a short text object naming the site, either in a Form
# XObject drawn on every page or inline in the page's content stream.
THIRD_PARTY_STAMP_RE = re.compile(rb'cxema\.ru|esquemaseletricos\.com\.br|chinadse\.org|all-guides\.com', re.IGNORECASE)
# A text object (BT ... ET) that contains one of the stamps
STAMP_TEXT_OBJECT_RE = re.compile(rb'BT(?:(?!\bET\b).)*?(?:cxema\.ru|esquemaseletricos\.com\.br|chinadse\.org|all-guides\.com)(?:(?!\bET\b).)*?ET\s*', re.DOTALL | re.IGNORECASE)
# A stamp form is tiny; a real page drawn as a form is not
MAX_STAMP_FORM_BYTES = 2000

# Full-page advert images some sites prepend to manuals, recognised by the
# perceptual (difference) hash of the image: 64 bits, 8x8 gradient signs of
# the greyscale image. A page is removed if it is image-only, at the start or
# end of the file, and within JUNK_COVER_MAX_DISTANCE bits of a known hash.
JUNK_COVER_HASHES = {
    0x1da5d7e353d3a58e: "esquemaseletricos.com.br red advert page",
}
JUNK_COVER_MAX_DISTANCE = 10

# Logo/URL banners other sites draw over the manual's own pages as images
# (so text matching can't see them), recognised the same way by the dhash of
# the image. Only the drawing of the image is removed; the page stays.
STAMP_IMAGE_HASHES = {
    0x004565751939f2f2: "tecnicosaurios.com diagonal banner",
}
STAMP_IMAGE_MAX_DISTANCE = 8
# Banners are wide strips; only images this much wider than tall are hashed,
# which keeps the pass cheap on scanned manuals full of page-sized images
STAMP_IMAGE_MIN_ASPECT = 6


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


def _walk_xobjects(container, visited=None, depth=0):
    """Yield (name, xobject) for every XObject reachable from a page or form, nested forms included."""
    if visited is None:
        visited = set()
    if depth > 6:
        return
    resources = container.get('/Resources')
    if resources is None:
        return
    for name, xobj in dict(resources.get('/XObject', {})).items():
        key = xobj.objgen
        if key in visited:
            continue
        visited.add(key)
        yield name, xobj
        if xobj.get('/Subtype') == '/Form':
            yield from _walk_xobjects(xobj, visited, depth + 1)


def _has_fonts(container, depth=0) -> bool:
    resources = container.get('/Resources')
    if resources is None or depth > 6:
        return False
    if len(dict(resources.get('/Font', {}))):
        return True
    return any(xobj.get('/Subtype') == '/Form' and _has_fonts(xobj, depth + 1)
               for _, xobj in _walk_xobjects(container))


def _strip_third_party_stamps(pdf: pikepdf.Pdf) -> int:
    """
    Blank the text stamps of sites like cxema.ru. Returns the number of
    pages changed. A stamp drawn via a Form XObject is emptied (the page's
    invocation of it then draws nothing); one inline in the page content
    has its text object cut out.
    """
    changed = 0
    emptied: set = set()
    for page in pdf.pages:
        page_changed = False
        for _name, xobj in _walk_xobjects(page.obj):
            if xobj.get('/Subtype') != '/Form':
                continue
            if xobj.objgen in emptied:
                page_changed = True  # shares a stamp form already blanked
                continue
            try:
                if int(xobj.get('/Length', MAX_STAMP_FORM_BYTES + 1)) > MAX_STAMP_FORM_BYTES * 4:
                    continue
                data = xobj.read_bytes()
            except Exception:
                continue
            if len(data) <= MAX_STAMP_FORM_BYTES and THIRD_PARTY_STAMP_RE.search(data):
                xobj.write(b'')
                emptied.add(xobj.objgen)
                page_changed = True
        text = _page_content(page)
        if text and THIRD_PARTY_STAMP_RE.search(text.encode('latin-1')):
            cleaned = STAMP_TEXT_OBJECT_RE.sub(b'', text.encode('latin-1'))
            if cleaned != text.encode('latin-1'):
                page['/Contents'] = pdf.make_stream(cleaned)
                page_changed = True
        if page_changed:
            changed += 1
    return changed


def _stamp_image_label(xobj) -> str | None:
    try:
        width, height = int(xobj.get('/Width', 0)), int(xobj.get('/Height', 0))
        if not height or width / height < STAMP_IMAGE_MIN_ASPECT:
            return None
        h = image_dhash(pikepdf.PdfImage(xobj).as_pil_image())
    except Exception:
        return None
    for known, label in STAMP_IMAGE_HASHES.items():
        if bin(h ^ known).count('1') <= STAMP_IMAGE_MAX_DISTANCE:
            return label
    return None


def _strip_stamp_images(pdf: pikepdf.Pdf) -> int:
    """
    Stop drawing known banner images (STAMP_IMAGE_HASHES): every `/Name Do`
    that paints one is removed from the page or form that paints it.
    Returns the number of pages changed.
    """
    verdicts: dict = {}
    cleaned_forms: set = set()

    def stamp_names(container) -> list[bytes]:
        names = []
        resources = container.get('/Resources')
        if resources is None:
            return names
        for name, xobj in dict(resources.get('/XObject', {})).items():
            if xobj.get('/Subtype') != '/Image':
                continue
            if xobj.objgen not in verdicts:
                verdicts[xobj.objgen] = _stamp_image_label(xobj)
            if verdicts[xobj.objgen]:
                names.append(str(name).encode('latin-1'))
        return names

    def strip(data: bytes, names: list[bytes]) -> bytes:
        for name in names:
            data = re.sub(rb'(?<![^\s\[\]()<>{}/%])' + re.escape(name) + rb'\s+Do\b', b'', data)
        return data

    changed = 0
    for page in pdf.pages:
        page_changed = False
        names = stamp_names(page.obj)
        if names:
            text = _page_content(page)
            if text:
                new = strip(text.encode('latin-1'), names)
                if new != text.encode('latin-1'):
                    page['/Contents'] = pdf.make_stream(new)
                    page_changed = True
        for _name, xobj in _walk_xobjects(page.obj):
            if xobj.get('/Subtype') != '/Form':
                continue
            if xobj.objgen in cleaned_forms:
                page_changed = True
                continue
            names = stamp_names(xobj)
            if not names:
                continue
            try:
                data = xobj.read_bytes()
            except Exception:
                continue
            new = strip(data, names)
            if new != data:
                xobj.write(new)
                cleaned_forms.add(xobj.objgen)
                page_changed = True
        if page_changed:
            changed += 1
    for label in {v for v in verdicts.values() if v}:
        logger.info(f"Removed stamp image: {label}")
    return changed


def image_dhash(image) -> int:
    """64-bit difference hash of a PIL image."""
    from PIL import Image

    grey = image.convert('L').resize((9, 8), Image.LANCZOS)
    px = list(grey.tobytes())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def _sole_image(page) -> pikepdf.Object | None:
    """The page's only image, if the page is nothing but that image (no text anywhere)."""
    if _has_fonts(page.obj):
        return None
    images = [x for _, x in _walk_xobjects(page.obj) if x.get('/Subtype') == '/Image']
    return images[0] if len(images) == 1 else None


def page_image_hash(page) -> int | None:
    """dhash of an image-only page's image, else None."""
    image = _sole_image(page)
    if image is None:
        return None
    try:
        return image_dhash(pikepdf.PdfImage(image).as_pil_image())
    except Exception:
        return None


def _junk_cover(page) -> str | None:
    h = page_image_hash(page)
    if h is None:
        return None
    for known, label in JUNK_COVER_HASHES.items():
        if bin(h ^ known).count('1') <= JUNK_COVER_MAX_DISTANCE:
            return label
    return None


def _remove_junk_cover_pages(pdf: pikepdf.Pdf) -> int:
    """Drop known advert pages from the front and back of the file. Returns pages removed."""
    removed = 0
    while len(pdf.pages) > 1 and (label := _junk_cover(pdf.pages[0])):
        logger.info(f"Removed first page: {label}")
        del pdf.pages[0]
        removed += 1
    while len(pdf.pages) > 1 and (label := _junk_cover(pdf.pages[-1])):
        logger.info(f"Removed last page: {label}")
        del pdf.pages[-1]
        removed += 1
    return removed


def strip_manualslib_watermark(pdf_path: Path | str) -> bool:
    """
    Remove ManualsLib's additions from a PDF file in-place.

    These are:
    - Wrapper pages: a cover / table of contents at the front and an
      "Other ManualsLib Projects" page at the back (see
      find_manualslib_wrapper_pages)
    - On every page: the footer "Downloaded from www.Manualslib.com manuals
      search engine" and a link annotation to manualslib.com

    Also removes marks left by the sites the manual passed through before:
    - Text stamps naming the site on every page (THIRD_PARTY_STAMP_RE)
    - Known full-page advert images at the front or back (JUNK_COVER_HASHES)
    - Known logo/URL banner images drawn over pages (STAMP_IMAGE_HASHES)

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

        if _remove_junk_cover_pages(pdf):
            _prune_dangling_outlines(pdf)
            modified = True

        stamped = _strip_third_party_stamps(pdf)
        stamped += _strip_stamp_images(pdf)
        if stamped:
            modified = True
            logger.info(f"Removed third-party site stamps from {stamped} pages of {pdf_path}")

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
