"""
Local reCAPTCHA v2 solving, so the paid 2captcha service is only a fallback.

Free layers, tried in order inside the real browser session:

1. Click the "I'm not a robot" checkbox. A home IP in a headful browser is
   usually let through with no challenge at all. Proxy IPs always get one.
2. Image challenge ("Select all images with a bus"): each tile is
   screenshotted and classified locally with reCognizer (YOLOv8-seg for the
   object classes it knows, CLIP for the rest; GPU when available). Handles
   3x3 static, 3x3 dynamic (tiles are replaced until none match) and 4x4
   "select all squares" grids, and retries through "Please try again".
3. Audio challenge, transcribed with Whisper. Google refuses this one to
   most proxy IPs ("Try again later"), so it rarely applies there.

The solve is reported through the normal g-recaptcha-response textarea,
exactly as a human solve would be, so the page's own flow continues.
"""
import logging
import re
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ANCHOR_FRAME = 'iframe[src*="recaptcha"][src*="anchor"]'
CHALLENGE_FRAME = 'iframe[src*="recaptcha"][src*="bframe"]'

# How long to give the checkbox click to resolve on its own before treating
# the challenge frame as the next step
CHECKBOX_WAIT = 6
# How long to wait for the response token after submitting an answer
VERIFY_WAIT = 8
# Image challenges (new grids) attempted before giving up on this captcha
IMAGE_ATTEMPTS = 6
# Rounds of "click matches, wait for replacements" in a dynamic grid
DYNAMIC_ROUNDS = 6


class AudioChallengeBlocked(Exception):
    """Google refused the audio challenge for this IP ("Try again later")."""


class ImageChallengeSolver:
    """Lazy-loaded reCognizer detector: tile images + label -> which tiles match."""

    def __init__(self):
        self._detector = None

    def _load(self):
        if self._detector is None:
            start = time.time()
            # Importing recognizer loads YOLO + CLIP + CLIPSeg (~1 GB of weights,
            # downloaded on first use), so only pay for it when a challenge shows
            from recognizer import Detector

            # transformers' hub lookups log every HEAD request at INFO
            logging.getLogger("httpx").setLevel(logging.WARNING)
            self._detector = Detector()
            models = self._detector.detection_models
            models.check_loaded()
            # transformers 5 loads the CLIP checkpoint in bfloat16; the tile
            # tensors are float32 and the matmul refuses mixed dtypes
            models.vit_model.float()
            models.seg_model.float()
            logger.info(f"Loaded image challenge models in {time.time() - start:.1f}s")
        return self._detector

    def classify(self, label: str, tiles: list[bytes]) -> list[bool]:
        """Which of the 9 or 16 tile screenshots show `label` (e.g. "bus")."""
        response, _coords = self._load().detect(label, tiles, area_captcha=len(tiles) == 16)
        return [bool(x) for x in response]


class WhisperTranscriber:
    """Lazy-loaded faster-whisper model. `device="auto"` prefers CUDA."""

    def __init__(self, model_size: str = "small.en", device: str = "auto"):
        self.model_size = model_size
        self.device = device
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        import ctranslate2
        from faster_whisper import WhisperModel

        device = self.device
        if device == "auto":
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        start = time.time()
        try:
            self._model = WhisperModel(self.model_size, device=device, compute_type=compute_type)
        except Exception as e:
            if device == "cuda":
                # Driver/library trouble shouldn't cost the run: fall back to CPU
                logger.warning(f"Whisper on CUDA failed ({e}); falling back to CPU")
                device, compute_type = "cpu", "int8"
                self._model = WhisperModel(self.model_size, device=device, compute_type=compute_type)
            else:
                raise
        logger.info(f"Loaded Whisper {self.model_size} on {device} ({compute_type}) in {time.time() - start:.1f}s")
        return self._model

    def transcribe(self, audio: bytes) -> str:
        model = self._load()
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio)
            path = Path(f.name)
        try:
            segments, _info = model.transcribe(str(path), language="en", beam_size=5, vad_filter=False)
            text = " ".join(segment.text for segment in segments)
        finally:
            path.unlink(missing_ok=True)
        # The expected answer is lowercase words/digits without punctuation
        return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


class LocalSolver:
    """The configured local layers; `solve(page)` runs them in order."""

    def __init__(self, image: ImageChallengeSolver | None = None, audio: WhisperTranscriber | None = None):
        self.image = image
        self.audio = audio

    def describe(self) -> str:
        layers = ["checkbox"]
        if self.image:
            layers.append("image challenge (reCognizer)")
        if self.audio:
            layers.append(f"audio challenge (Whisper {self.audio.model_size})")
        return ", ".join(layers)

    def solve(self, page) -> str | None:
        """
        Try to pass the reCAPTCHA v2 on `page` without any paid service.

        Returns the g-recaptcha-response token on success, None if the caller
        should fall back. Never raises for ordinary failures.
        """
        try:
            return _solve(page, self)
        except AudioChallengeBlocked:
            logger.warning("Audio challenge blocked for this IP (\"Try again later\")")
            return None
        except Exception as e:
            logger.warning(f"Local captcha solve failed: {e}")
            return None


# ---------------------------------------------------------------------------
# DOM helpers

def _frame(page, selector):
    handle = page.query_selector(selector)
    return handle.content_frame() if handle else None


def _response_token(page) -> str:
    return page.evaluate(
        "() => { const t = document.querySelector('[name=\"g-recaptcha-response\"]'); return t ? t.value : ''; }"
    ) or ""


def _wait_for_token(page, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        token = _response_token(page)
        if token:
            return token
        time.sleep(0.5)
    return ""


def _visible(frame, selector) -> bool:
    try:
        el = frame.query_selector(selector)
        return bool(el and el.is_visible())
    except Exception:
        return False


def _text(frame, selector) -> str:
    try:
        el = frame.query_selector(selector)
        return (el.inner_text() if el else "").strip()
    except Exception:
        return ""


def _rate_limited(frame) -> bool:
    return _visible(frame, ".rc-doscaptcha-header") or _visible(frame, ".rc-doscaptcha-body")


# ---------------------------------------------------------------------------
# Solve flow

def _solve(page, solver: LocalSolver) -> str | None:
    anchor = _frame(page, ANCHOR_FRAME)
    if anchor is None:
        logger.info("No reCAPTCHA anchor frame on page")
        return None

    # Layer 1: the checkbox alone
    checkbox = anchor.wait_for_selector("#recaptcha-anchor", timeout=10000)
    checkbox.click()
    token = _wait_for_token(page, CHECKBOX_WAIT)
    if token:
        logger.info("reCAPTCHA passed on checkbox click (no challenge)")
        return token

    challenge = _frame(page, CHALLENGE_FRAME)
    if challenge is None:
        token = _wait_for_token(page, 4)
        if token:
            logger.info("reCAPTCHA passed on checkbox click (late)")
            return token
        logger.info("Challenge frame did not appear after checkbox click")
        return None

    # Layer 2: image challenge
    if solver.image is not None and not _visible(challenge, "#audio-response"):
        token = _solve_image_challenge(page, challenge, solver.image)
        if token:
            return token

    # Layer 3: audio challenge
    if solver.audio is not None:
        token = _solve_audio_challenge(page, challenge, solver.audio)
        if token:
            return token

    return None


def _grid(challenge):
    """(label, tiles, dynamic) for the currently shown image challenge."""
    label = _text(challenge, ".rc-imageselect-desc-wrapper strong") or _text(challenge, "strong")
    desc = _text(challenge, ".rc-imageselect-desc-wrapper")
    tiles = challenge.query_selector_all("td.rc-imageselect-tile")
    dynamic = "none left" in desc.lower()
    return label, tiles, dynamic


def _wait_for_grid(challenge, timeout: float = 10) -> bool:
    """Wait until a 3x3 or 4x4 grid with its label is shown."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        label, tiles, _ = _grid(challenge)
        if label and len(tiles) in (9, 16) and all(t.is_visible() for t in tiles):
            return True
        time.sleep(0.5)
    return False


def _tile_srcs(tiles) -> list[str]:
    srcs = []
    for tile in tiles:
        img = tile.query_selector("img")
        srcs.append((img.get_attribute("src") or "") if img else "")
    return srcs


def _wait_for_tile_replacements(challenge, clicked: list[int], before: list[str], timeout: float = 12) -> None:
    """
    In a dynamic grid each clicked tile fades out and is replaced by a new
    image (a new img src). Wait until every clicked tile has been swapped and
    nothing is still marked as loading, so the next round sees the new images.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, tiles, _ = _grid(challenge)
        if len(tiles) == len(before):
            now = _tile_srcs(tiles)
            swapped = all(now[i] and now[i] != before[i] for i in clicked)
            loading = challenge.query_selector(".rc-imageselect-dynamic-selected") is not None
            if swapped and not loading:
                time.sleep(0.8)  # let the new images finish rendering
                return
        time.sleep(0.4)
    logger.info("  replacement tiles did not all load in time")


def _solve_image_challenge(page, challenge, image_solver: ImageChallengeSolver) -> str | None:
    for attempt in range(1, IMAGE_ATTEMPTS + 1):
        if _rate_limited(challenge):
            logger.warning("reCAPTCHA refuses to serve a challenge to this IP (\"Try again later\")")
            return None
        if not _wait_for_grid(challenge):
            if _rate_limited(challenge):
                logger.warning("reCAPTCHA refuses to serve a challenge to this IP (\"Try again later\")")
            else:
                logger.info("Image challenge grid did not load")
            return None

        label, tiles, dynamic = _grid(challenge)
        kind = f"{'4x4' if len(tiles) == 16 else '3x3'}{' dynamic' if dynamic else ''}"
        logger.info(f"Image challenge {attempt}/{IMAGE_ATTEMPTS}: \"{label}\" ({kind})")

        clicked_any = False
        rounds = DYNAMIC_ROUNDS if dynamic else 1
        for round_no in range(rounds):
            label, tiles, _ = _grid(challenge)
            if len(tiles) not in (9, 16):
                break
            start = time.time()
            shots = [tile.screenshot() for tile in tiles]
            matches = image_solver.classify(label, shots)
            hits = [i for i, m in enumerate(matches) if m]
            logger.info(f"  tiles matching \"{label}\": {hits} ({time.time() - start:.1f}s)")
            if not matches or not hits:
                break
            before = _tile_srcs(tiles)
            for i in hits:
                tiles[i].click()
                time.sleep(0.25)
            clicked_any = True
            if not dynamic:
                break
            _wait_for_tile_replacements(challenge, hits, before)

        if not clicked_any:
            # Nothing we recognise in this grid: ask for another one
            logger.info("  no matching tiles found, reloading challenge")
            challenge.click("#recaptcha-reload-button")
            time.sleep(1.5)
            continue

        challenge.click("#recaptcha-verify-button")
        token = _wait_for_token(page, VERIFY_WAIT)
        if token:
            logger.info("reCAPTCHA passed via image challenge")
            return token

        # Still here: find out what Google objected to
        for selector in (".rc-imageselect-incorrect-response", ".rc-imageselect-error-select-more",
                         ".rc-imageselect-error-dynamic-more", ".rc-imageselect-error-select-something"):
            if _visible(challenge, selector):
                logger.info(f"  rejected: {_text(challenge, selector) or selector}")
                if selector != ".rc-imageselect-incorrect-response":
                    # Same grid is still up and we have nothing more to click: get a new one
                    challenge.click("#recaptcha-reload-button")
                    time.sleep(1.5)
                break
        else:
            logger.info("  no token and no error shown after verify")
        time.sleep(1)

    logger.info("Image challenge not solved within attempt limit")
    return None


def _solve_audio_challenge(page, challenge, transcriber: WhisperTranscriber, max_attempts: int = 3) -> str | None:
    if not _visible(challenge, "#audio-response"):
        if not _visible(challenge, "#recaptcha-audio-button"):
            return None
        challenge.click("#recaptcha-audio-button")
        time.sleep(1.5)

    for attempt in range(1, max_attempts + 1):
        if _rate_limited(challenge):
            raise AudioChallengeBlocked()

        link = challenge.wait_for_selector(".rc-audiochallenge-tdownload-link", timeout=10000)
        audio_url = link.get_attribute("href")
        if not audio_url:
            logger.info("Audio challenge has no download link")
            return None

        # Fetch through the browser context so the request carries its cookies
        # and goes out via the same proxy as the page
        response = page.request.get(audio_url)
        if not response.ok:
            logger.info(f"Audio fetch failed: HTTP {response.status}")
            return None
        audio = response.body()

        start = time.time()
        answer = transcriber.transcribe(audio)
        logger.info(f"Audio challenge attempt {attempt}/{max_attempts}: heard \"{answer}\" ({time.time() - start:.1f}s, {len(audio)} bytes)")
        if not answer:
            challenge.click("#recaptcha-reload-button")
            time.sleep(1.5)
            continue

        challenge.fill("#audio-response", answer)
        time.sleep(0.5)
        challenge.click("#recaptcha-verify-button")

        token = _wait_for_token(page, VERIFY_WAIT)
        if token:
            logger.info("reCAPTCHA passed via audio challenge")
            return token

        error = _text(challenge, ".rc-audiochallenge-error-message")
        if error:
            logger.info(f"reCAPTCHA rejected the answer: {error}")
        if _rate_limited(challenge):
            raise AudioChallengeBlocked()
        # A fresh clip is loaded automatically after a wrong answer; loop

    logger.info("Audio challenge not solved within attempt limit")
    return None
