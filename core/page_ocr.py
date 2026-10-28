"""Opt-in, local page extraction. Never activates a profile or edits legacy caches.

One worker, immutable attempt receipts, explicit retry allowance, and one canonical
payload for PDF chunking and display-only Markdown. Processing is not accuracy.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable

from core.cache_exports import _directory, _locked, _publish_new, _safe_directory
from core.practical_payload import canonical, validate, _source_hash

LANGUAGE_COMMIT = 'e12c65a915945e4c28e237a9b52bc4a8f39a0cec'
LANGUAGE_HASHES = {
    'eng.traineddata': '8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba',
    'fra.traineddata': '907743d98915c91a3906dfbf6e48b97598346698fe53aaa797e1a064ffcac913',
    'LICENSE': 'a6cba85bc92e0cff7a450b1d873c0eaa2e9fc96bf472df0247a26bec77bf3ff9',
}
_SUCCESS = {'native', 'ocr'}
_EXTRACTED = _SUCCESS | {'native_partial', 'ocr_partial', 'mixed_unverified'}
_MAX_ATTEMPTS = 2
_MAX_OUTPUT = 8 * 1024**2
_MAX_RANGE_TEXT = 32 * 1024**2


def _bounded_render_scale(rect, dpi: int, max_pixels: int = 20_000_000) -> float:
    """Bound the raster before allocation, including integer pixel rounding.

    Resolution reduction is recorded as unverified evidence, never a claim of
    transcription quality. Invalid geometry remains an explicit render limit.
    """
    width, height = float(rect.width), float(rect.height)
    if not all(math.isfinite(x) and x > 0 for x in (width, height)):
        raise ValueError('Invalid page geometry')
    scale = min(dpi / 72, math.sqrt((max_pixels * 0.99) / (width * height)))
    while (math.ceil(width * scale) + 2) * (math.ceil(height * scale) + 2) > max_pixels:
        scale *= 0.99
    return scale


def _render_transport(pix, *, online: bool) -> tuple[bytes, str]:
    """Keep lossless PNG unless its bytes exceed the online transport limit.

    Pixel allocation remains bounded by the renderer. JPEG conversion changes
    only the request raster, never the source PDF; callers record the encoding.
    The provider adapter still enforces both byte and pixel limits.
    """
    data = pix.tobytes('png')
    if online and len(data) > 20 * 1024**2:
        return pix.tobytes('jpeg', jpg_quality=95), 'jpeg95'
    return data, 'png'


def _bytes(record: dict | list) -> bytes:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _new(directory: Path, name: str, record: dict) -> None:
    with _directory(directory) as fd:
        _publish_new(fd, name, _bytes(record))


def _read(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > 2 * _MAX_OUTPUT:
        raise ValueError('Unsafe or oversized page receipt')
    return json.loads(path.read_text(encoding='utf-8'))


@dataclass(frozen=True)
class TesseractOCR:
    executable: Path
    tessdata: Path
    timeout_s: float = 120
    psm: int = 3

    def identity(self) -> dict:
        import numpy
        import PIL

        if not 0 < self.timeout_s <= 120 or self.psm not in (3, 6):
            raise ValueError('OCR timeout must be in (0, 120] seconds')
        executable = self.executable.resolve(strict=True)
        if not os.access(executable, os.X_OK):
            raise ValueError('Tesseract is not executable')
        for name, expected in LANGUAGE_HASHES.items():
            if _source_hash(self.tessdata / name) != expected:
                raise ValueError(f'Unverified OCR artifact: {name}')
        return {'engine': 'tesseract', 'binary_sha256': _source_hash(executable),
                'languages': LANGUAGE_HASHES, 'language_order': 'eng+fra',
                'oem': 1, 'psm': self.psm, 'threads': 1, 'timeout_s': self.timeout_s,
                'ruled_regions': 'max48-psm6-total120s-v1',
                'numpy': numpy.__version__, 'pillow': PIL.__version__,
                'max_sampled_rss_bytes': 1024**3}

    def recognize(self, png: bytes, cancelled: Callable[[], bool],
                  heartbeat: Callable[[], None]) -> dict:
        from core.raster_layout import crop, regions
        started = time.monotonic()
        boxes = regions(png) if png.startswith(b'\x89PNG') else []
        if not boxes:
            return self._recognize_single(png, cancelled, heartbeat)
        records = []
        stopped = False
        budget_expired = False
        output_bytes = 0
        for box in boxes:
            remaining = self.timeout_s - (time.monotonic() - started)
            if stopped or cancelled() or remaining <= 0:
                budget_expired = budget_expired or remaining <= 0
                records.append({**box, 'status': 'not_attempted', 'text': '', 'calls': 0,
                                'call_s': 0.0})
                stopped = True
                continue
            engine = replace(self, psm=box['psm'], timeout_s=min(self.timeout_s, remaining))
            result = engine._recognize_single(
                crop(png, box['bbox_pixels']),
                lambda: cancelled() or time.monotonic() - started >= self.timeout_s, heartbeat)
            if result['status'] == 'cancelled' and time.monotonic() - started >= self.timeout_s:
                result['status'] = 'timeout'
            output_bytes += len(result.get('text', '').encode()) + len(result.get('tsv', '').encode())
            if output_bytes > _MAX_OUTPUT:
                result = {**result, 'status': 'output_limit', 'text': '', 'tsv': '',
                          'warning': 'Region output exceeds aggregate page bound.'}
            records.append({**box, **result})
            if result['status'] not in {'ocr', 'empty_unverified'}:
                stopped = True
        bands: dict[int, list[str]] = {}
        for row in records:
            bands.setdefault(row['band'], []).append(row['text'].strip())
        text = '\n\n'.join(' | '.join(cells) for cells in bands.values())
        failures = [row['status'] for row in records
                    if row['status'] not in {'ocr', 'empty_unverified', 'not_attempted'}]
        status = (failures[0] if failures else 'timeout' if budget_expired else
                  'cancelled' if stopped else 'ocr_partial')
        if not stopped and not failures and not any(row['text'].strip() for row in records):
            status, text = 'empty_unverified', ''
        return {'status': status, 'text': text, 'regions': records,
                'calls': sum(row['calls'] for row in records),
                'call_s': sum(row['call_s'] for row in records),
                'peak_sampled_rss_bytes': max((row.get('peak_sampled_rss_bytes') or 0
                                              for row in records), default=0),
                'quality_flags': ['ruled_raster_cell_associations_unverified'],
                'warning': 'Partial ruled-page OCR: cells are geometry hypotheses. '
                           'Arrows, signs, empty regions and numeric/label recognition require review.'}

    def _recognize_single(self, png: bytes, cancelled: Callable[[], bool],
                          heartbeat: Callable[[], None]) -> dict:
        """A single subprocess; cancellation terminates only this owned child."""
        started = time.monotonic()
        if cancelled():
            return {'status': 'cancelled', 'text': '', 'calls': 0, 'call_s': 0.0}
        with tempfile.TemporaryDirectory(prefix='papermind-tesseract-') as temporary:
            root = Path(temporary)
            image, output = root / 'page.png', root / 'result'
            image.write_bytes(png)
            command = [str(self.executable.resolve()), str(image), str(output),
                       '--tessdata-dir', str(self.tessdata.resolve()), '-l', 'eng+fra',
                       '--oem', '1', '--psm', str(self.psm), '-c', 'tessedit_create_txt=1',
                       '-c', 'tessedit_create_tsv=1']
            # No inherited provider credentials, no shell, no service/model startup.
            environment = {'PATH': '/usr/bin:/bin:/opt/homebrew/bin',
                           'LC_ALL': 'C', 'OMP_THREAD_LIMIT': '1'}
            status = 'failed'
            peak_rss = None
            last_sample = float('-inf')
            from core.ocr_resources import process_rss_bytes
            try:
                with (root / 'stderr').open('wb') as errors:
                    started = time.monotonic()
                    child = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                             stderr=errors, env=environment)
                    try:
                        while child.poll() is None:
                            if time.monotonic() - last_sample >= 1:
                                heartbeat()
                                sample = process_rss_bytes(child.pid)
                                if sample is not None:
                                    peak_rss = max(peak_rss or 0, sample)
                                    if sample > 1024**3:
                                        status = 'memory_limit'
                                        break
                                last_sample = time.monotonic()
                            if cancelled():
                                status = 'cancelled'
                                break
                            if time.monotonic() - started >= self.timeout_s:
                                status = 'timeout'
                                break
                            time.sleep(.1)
                        else:
                            status = 'ocr' if child.returncode == 0 else 'engine_error'
                    finally:
                        if child.poll() is None:
                            child.terminate()
                            try:
                                child.wait(timeout=1)
                            except subprocess.TimeoutExpired:
                                child.kill()
                                child.wait(timeout=2)
            except OSError:
                return {'status': 'unavailable', 'text': '', 'calls': 0,
                        'call_s': time.monotonic() - started}
            result: dict = {'status': status, 'text': '', 'tsv': '', 'calls': 1,
                      'call_s': time.monotonic() - started,
                      'peak_sampled_rss_bytes': peak_rss}
            for suffix, key in [('.txt', 'text'), ('.tsv', 'tsv')]:
                path = output.with_suffix(suffix)
                if path.exists():
                    if path.stat().st_size > _MAX_OUTPUT:
                        result['status'] = 'output_limit'
                    else:
                        result[key] = path.read_text(encoding='utf-8', errors='replace')
            if result['status'] == 'ocr' and not str(result['text']).strip():
                result['status'] = 'empty_unverified'
            return result


def _has_substantial_image(page) -> bool:
    """Whether an image covers enough of this physical page to need OCR."""
    # get_image_info renders the display list, which can be very slow on dense
    # vector-only pages. Check image resources and inline-image operators first.
    if not page.get_images(full=True) and not re.search(
        rb"(?<!\S)BI(?=\s)", page.read_contents()
    ):
        return False
    return any(
        (rect & page.rect).get_area() >= page.rect.get_area() * .2
        for image in page.get_image_info() for rect in [page.rect.__class__(image['bbox'])]
    )


def _has_visual_figure(page) -> bool:
    """Raster images or nontrivial vector regions; eligibility is not accuracy."""
    if _has_substantial_image(page):
        return True
    area = max(1, page.rect.width * page.rect.height)
    return any(drawing.get('rect') is not None and
               drawing['rect'].width * drawing['rect'].height >= area * 0.02
               for drawing in page.get_drawings())


def _page_content(page) -> tuple[str, bool]:
    """Use existing native parser, bounded to this page; never whole-file fallback."""
    # Caller supplies the native text separately through the existing extractor.
    raw = page.get_text('text')
    return raw, _has_substantial_image(page)


def _summary(record: dict) -> dict:
    result = dict(record)
    for key in ('tsv', 'native_text'):
        if key in result:
            result[key + '_sha256'] = _sha(result.pop(key).encode())
    if 'native_fragments' in result:
        result['native_fragments_sha256'] = _sha(_bytes(result.pop('native_fragments')))
    if 'regions' in result and any('text' in region or 'tsv' in region for region in result['regions']):
        result['regions_sha256'] = _sha(_bytes(result['regions']))
        result['regions'] = [{k: v for k, v in region.items() if k not in {'text', 'tsv'}}
                             for region in result['regions']]
    return result


def _ocr_uncertainties(tsv: str) -> list[dict]:
    """Confidence is a review locator, never a correctness/completeness score."""
    uncertain = []
    try:
        reader = csv.DictReader(io.StringIO(tsv), delimiter='\t', quoting=csv.QUOTE_NONE)
        if not {'level', 'conf', 'left', 'top', 'width', 'height', 'text'} <= set(reader.fieldnames or []):
            return [{'reason': 'tsv_confidence_unavailable'}]
        for word in reader:
            if word.get('level') != '5' or not word.get('text', '').strip():
                continue
            confidence = float(word['conf'])
            if confidence < 80:
                uncertain.append({'text': word['text'], 'confidence': confidence,
                                  'bbox_pixels': [int(word[k]) for k in ('left', 'top', 'width', 'height')]})
    except (KeyError, ValueError, TypeError, csv.Error):
        return [{'reason': 'tsv_confidence_unavailable'}]
    return uncertain


def _latest(directory: Path, page: int, binding: dict) -> tuple[dict | None, int]:
    last = None
    used = 0
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        stem = f'p{page:05d}-a{attempt}'
        reservation = directory / (stem + '-started.json')
        result = directory / (stem + '.json')
        if not reservation.exists():
            if result.exists():
                raise ValueError('Page result has no attempt reservation')
            continue
        if attempt != used + 1 or _read(reservation) != {**binding, 'attempt': attempt}:
            raise ValueError('Page reservation mismatch')
        used = attempt
        if result.exists():
            envelope = _read(result)
            record = envelope['record']
            if (envelope['sha256'] != _sha(_bytes(record))
                    or record['binding'] != binding or record['attempt'] != attempt):
                raise ValueError('Altered or mismatched page checkpoint')
            last = _summary(record)
        else:
            last = {'binding': binding, 'attempt': attempt, 'status': 'interrupted',
                    'text': '', 'warning': 'Attempt reserved without a completed receipt.'}
    return last, used


def extract_pages(source: Path, input_root: Path, storage: Path, engine: TesseractOCR, *,
                  start_page: int = 0, end_page: int | None = None,
                  cancelled: Callable[[], bool] = lambda: False,
                  heartbeat: Callable[[], None] = lambda: None,
                  retry_pages: frozenset[int] = frozenset(),
                  budget_s: float = 1200,
                  accept_mixed_native: bool = False,
                  visual_engine=None,
                  attempt_generation: str | None = None,
                  resource_guard: Callable[[], str] | None = None) -> dict:
    """Explicit execution only. Pages are physical, retries are one-based.

    Failed/interrupted pages are retained until explicitly selected for retry;
    unattempted pages resume normally. At most two attempts per recipe/page.
    Only the explicitly supplied engines may call providers. No legacy cache,
    embedding or activation calls.
    """
    import fitz
    from core.page_layout import native_page

    source = source.absolute()
    inputs, target = input_root.resolve(), storage.resolve()
    if (source.is_symlink() or not source.resolve().is_relative_to(inputs)
            or source.suffix.lower() != '.pdf' or target.is_relative_to(inputs)
            or inputs.is_relative_to(target) or not 0 < budget_s <= 1200):
        raise ValueError('Unsafe source/storage boundary or time budget')
    identity = engine.identity()  # missing service/artifacts refuse before all pages
    if attempt_generation is not None and (len(attempt_generation) != 32 or
            any(char not in '0123456789abcdef' for char in attempt_generation)):
        raise ValueError('Invalid attempt generation')
    recipe_data = {'schema': 1, 'engine': identity, 'pymupdf': fitz.VersionBind,
                   'dpi': 300, 'max_pixels': 20_000_000, 'range_text_bytes': _MAX_RANGE_TEXT,
                   'visual_engine': visual_engine.identity() if visual_engine else None,
                   'native_policy': ('index-native-text-with-visual-warning-v2'
                                     if accept_mixed_native else 'geometry-preserving-partial-v2'),
                   **({'attempt_generation': attempt_generation} if attempt_generation else {}),
                   'implementation': _source_hash(Path(__file__)),
                   'layout_implementation': _source_hash(Path(__file__).with_name('page_layout.py')),
                   'raster_implementation': _source_hash(Path(__file__).with_name('raster_layout.py')),
                   'native_implementation': _source_hash(Path(__file__).resolve().parents[1] / 'pdf_extractor.py')}
    recipe = _sha(_bytes(recipe_data))
    digest = _source_hash(source)
    root = _safe_directory(storage, ())
    # Global across documents sharing this storage: only one OCR worker at a time.
    with _locked(root / 'worker.lock'):
        directory = _safe_directory(root, (recipe, digest))
        _new(directory, 'recipe.json', recipe_data)
        started = time.monotonic()
        records = []
        consecutive_failures = 0
        retained_bytes = 0
        stop_reason = ''
        if resource_guard is None:
            from core.ocr_resources import MemoryGuard
            resource_guard = MemoryGuard()

        def should_stop() -> bool:
            nonlocal stop_reason
            if cancelled():
                stop_reason = 'cancelled'
            elif time.monotonic() - started >= budget_s:
                stop_reason = 'budget'
            else:
                assert resource_guard is not None
                stop_reason = stop_reason or resource_guard()
            return bool(stop_reason)

        def retain(record: dict) -> None:
            nonlocal retained_bytes, stop_reason
            summary = _summary(record)
            size = len(summary['text'].encode())
            if retained_bytes + size > _MAX_RANGE_TEXT:
                stop_reason = 'text_memory_budget'
                summary['text'] = ''
                summary['status'] = 'not_materialized'
                summary['warning'] = 'Text retained in page checkpoint; use a smaller export range.'
            else:
                retained_bytes += size
            records.append(summary)

        should_stop()
        with tempfile.TemporaryDirectory(prefix='page-source-', dir=directory) as temporary:
            snapshot = Path(temporary) / 'source.pdf'
            shutil.copyfile(source, snapshot)
            if _source_hash(snapshot) != digest:
                raise ValueError('Source changed while taking extraction snapshot')
            with fitz.open(snapshot) as document:
                end = len(document) if end_page is None else min(end_page, len(document))
                if not 0 <= start_page < end or end - start_page > 1000:
                    raise ValueError('Expected a range of 1–1000 pages')
                if not retry_pages <= set(range(start_page + 1, end + 1)):
                    raise ValueError('Retry pages must belong to requested range')
                for index in range(start_page, end):
                    number = index + 1
                    binding = {'source_sha256': digest, 'recipe': recipe, 'page': number}
                    previous, attempts = _latest(directory, number, binding)
                    if previous and (previous['status'] in _SUCCESS
                                     or number not in retry_pages or attempts >= _MAX_ATTEMPTS):
                        retain(previous)
                        continue
                    heartbeat()
                    if should_stop():
                        records.append({'binding': binding, 'attempt': attempts, 'text': '',
                                        'status': 'not_attempted', 'warning': stop_reason})
                        continue
                    attempt = attempts + 1
                    stem = f'p{number:05d}-a{attempt}'
                    _new(directory, stem + '-started.json', {**binding, 'attempt': attempt})
                    page_started = time.monotonic()
                    row: dict = {'binding': binding, 'attempt': attempt, 'text': '', 'calls': 0,
                           'call_s': 0.0, 'render_s': 0.0, 'status': 'failed',
                           'started_at': time.time()}
                    try:
                        page = document[index]
                        native, image = _page_content(page)
                        if visual_engine is not None:
                            image = _has_visual_figure(page)
                        if accept_mixed_native and not native.strip() and not image:
                            # A content stream without extractable text can be a
                            # vector figure or outlined text. Avoid walking every
                            # drawing on dense pages just to detect this case.
                            image = bool(page.read_contents().strip())
                        # Native is never concatenated with full-page OCR. Keep the
                        # original layer as evidence when choosing OCR instead.
                        row['native_text'] = native
                        if native.strip() and (len(native.strip()) >= 40 or not image):
                            layout = native_page(page)
                            row['text'] = layout['text'] or native
                            row['native_fragments'] = layout['fragments']
                            row['quality_flags'] = layout['warnings']
                            row['status'] = ('mixed_unverified' if image else
                                             'native_partial' if layout['warnings'] else 'native')
                            if accept_mixed_native and row['status'] != 'native':
                                row['original_status'] = row['status']
                                row['status'] = 'native'
                                if image:
                                    row['quality_flags'] = (layout['warnings'] +
                                                            ['embedded_image_content_not_described'])
                                row['warning'] = ('Native text retained for indexing. Page layout '
                                                  'and flagged visual content remain unverified.')
                            if visual_engine is not None and image:
                                scale = _bounded_render_scale(page.rect, 150)
                                row['render_dpi'] = scale * 72
                                if scale < 150 / 72:
                                    row['render_warning'] = 'Resolution reduced to fit pixel budget; inspect small text.'
                                if page.rect.width * page.rect.height * scale**2 > 20_000_000:
                                    row['status'] = 'render_limit'
                                else:
                                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                                                         colorspace=fitz.csRGB, alpha=False)
                                    png, encoding = _render_transport(pix, online=recipe_data['visual_engine'].get('engine') in
                                        ('online_image_transcription', 'online_image_description'))
                                    del pix
                                    row['render_encoding'] = encoding
                                    if encoding != 'png':
                                        row['render_warning'] = 'JPEG95 request raster used to fit byte budget; inspect fine detail.'
                                    visual = visual_engine.recognize(png, should_stop, heartbeat)
                                    from core.online_ocr import ERROR_FIELDS
                                    row.update({key: visual[key] for key in ERROR_FIELDS if key in visual})
                                    row['visual_status'] = visual['status']
                                    row['visual_usage'] = visual.get('usage', {})
                                    row['calls'] = row.get('calls', 0) + visual.get('calls', 0)
                                    row['call_s'] = row.get('call_s', 0) + visual.get('call_s', 0)
                                    if visual['status'] == 'ocr':
                                        row['text'] += '\n\n[Visual description — unverified]\n' + visual['text']
                                        row['quality_flags'] = [f for f in row['quality_flags']
                                                               if f != 'embedded_image_content_not_described']
                                        row['quality_flags'].append('online_visual_description_unverified')
                                    else:
                                        row['status'] = visual['status']
                                        row['warning'] = 'Visual enrichment incomplete; native text retained in receipt.'
                            if layout['warnings'] and row['status'] != 'native':
                                row['warning'] = 'Partial/unqualified regions: ' + '; '.join(layout['warnings'])
                        elif not image and not native.strip():
                            if accept_mixed_native:
                                row['status'] = 'native'
                                row['quality_flags'] = ['blank_page_no_extractable_content']
                                row['warning'] = 'No native text, raster image or page content on this page.'
                            else:
                                row['status'] = 'empty_unverified'
                                row['warning'] = 'No native text or raster image; blank/vector page requires review.'
                        else:
                            scale = _bounded_render_scale(page.rect, 300)
                            row['render_dpi'] = scale * 72
                            if scale < 300 / 72:
                                row['render_warning'] = 'Resolution reduced to fit pixel budget; inspect small text.'
                            if page.rect.width * page.rect.height * scale**2 > 20_000_000:
                                row['status'] = 'render_limit'
                            else:
                                render_start = time.monotonic()
                                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
                                png, encoding = _render_transport(pix, online=identity.get('engine') in
                                    ('online_image_transcription', 'online_image_description'))
                                del pix
                                row['render_encoding'] = encoding
                                if encoding != 'png':
                                    row['render_warning'] = 'JPEG95 request raster used to fit byte budget; inspect fine detail.'
                                row['render_s'] = time.monotonic() - render_start
                                row['render_sha256'] = _sha(png)
                                row.update(engine.recognize(png, should_stop, heartbeat))
                                if 'regions' in row:
                                    row['uncertain_words'] = [
                                        {**word, 'region_bbox_pixels': region['bbox_pixels'],
                                         'region_padding_pixels': 12}
                                        for region in row['regions']
                                        for word in _ocr_uncertainties(region.get('tsv', ''))]
                                if row['status'] == 'ocr' and 'tsv' in row:
                                    row['uncertain_words'] = _ocr_uncertainties(row['tsv'])
                                    # Raster structure has not been qualified. Even high
                                    # confidence words cannot establish missing cells,
                                    # marks, arrows or labels. Do not claim page coverage.
                                    row['status'] = 'ocr_partial'
                                    row['quality_flags'] = ['raster_layout_and_omissions_unverified']
                                    if row['uncertain_words']:
                                        row['quality_flags'].append('low_confidence_words')
                                    row['warning'] = ('Partial OCR: labels, symbols, arrows and table cell '
                                                      'associations are unqualified and may be missing. '
                                                      'See uncertain words and original page; numbers are not corrected.')
                                del png
                    except (OSError, RuntimeError, ValueError) as exc:
                        row['status'] = 'failed'
                        row['warning'] = type(exc).__name__
                    try:
                        runtime_matches = (engine.identity() == identity and
                                           (visual_engine is None or visual_engine.identity() == recipe_data['visual_engine']))
                    except (OSError, ValueError):
                        runtime_matches = False
                    if not runtime_matches:
                        row['status'] = 'runtime_changed'
                        row['warning'] = 'OCR runtime changed during work; result is unqualified.'
                        stop_reason = 'runtime_changed'
                    row['page_s'] = time.monotonic() - page_started
                    row['fidelity'] = 'unverified'
                    _new(directory, stem + '.json', {'record': row, 'sha256': _sha(_bytes(row))})
                    retain(row)
                    consecutive_failures = (0 if row['status'] in _EXTRACTED
                                            else consecutive_failures + 1)
                    if row['status'] in {'unavailable', 'engine_error', 'memory_limit'}:
                        stop_reason = 'engine_unavailable'  # don't consume remaining page attempts
                    elif consecutive_failures >= 2 and row['calls']:
                        stop_reason = 'two_consecutive_ocr_failures'
                page_count = len(document)
        if _source_hash(source) != digest:
            raise ValueError('Source changed; snapshot checkpoints retained, publication refused')
        complete = all(row['status'] in _SUCCESS for row in records)
        payload = canonical(source, digest, recipe,
                            [(row['binding']['page'], row['text']) for row in records],
                            outcome='processed' if complete else 'partial')
        result: dict = {'schema': 1, 'payload': payload, 'pages': records, 'page_count': page_count,
                  'range': [start_page + 1, end], 'elapsed_s': time.monotonic() - started,
                  'coverage': 'processed_unverified' if complete else 'partial',
                  'warning': 'Automatic text; numbers, tables, reading order and figure semantics are unverified.',
                  'stop_reason': stop_reason,
                  # A shared batch guard keeps sampling after this snapshot.
                  # Freeze evidence so its hash and later exports stay valid.
                  'resources': [dict(sample) for sample in getattr(resource_guard, 'samples', [])],
                  'resource_sample_count': getattr(resource_guard, 'sample_count', 0)}
        result['snapshot_id'] = _sha(_bytes(result))
        _new(directory, 'snapshot-' + result['snapshot_id'] + '.json', result)
        return result


def export_pages(bundle: dict, destination: Path, input_root: Path) -> Path:
    """Immutable Markdown plus exact canonical text and source-bound JSON."""
    payload = bundle['payload']
    source = Path(payload['source'])
    text = validate(payload, source, payload['recipe'])
    inputs, target = input_root.resolve(), destination.resolve()
    if (not source.resolve().is_relative_to(inputs) or target.is_relative_to(inputs)
            or inputs.is_relative_to(target)):
        raise ValueError('Exports must be outside indexed inputs')
    identity = bundle['snapshot_id']
    original = {k: v for k, v in bundle.items() if k != 'snapshot_id'}
    if _sha(_bytes(original)) != identity:
        raise ValueError('Altered extraction snapshot')
    lines = [f'# {source.name}', '', f'[Source PDF](<{source.as_uri()}>)', '',
             f'Coverage: **{bundle["coverage"]}**. {bundle["warning"]}', '',
             f'PDF SHA-256: `{payload["source_sha256"]}`', '']
    for row in bundle['pages']:
        lines += [f'## Page {row["binding"]["page"]} — {row["status"]}', '',
                  '> ' + row.get('warning', 'Automatic transcription; fidelity unverified.'), '',
                  row['text'] or '*No usable text retained for this page.*', '']
        if row.get('uncertain_words'):
            lines += ['### OCR uncertainty locations (display only)', '',
                      'Word boxes are x/y/width/height at 300 dpi. For region OCR they are relative '
                      'to that region plus 12-pixel padding; region boxes are page x0/y0/x1/y1. '
                      'Confidence is not accuracy.', '']
            for word in row['uncertain_words']:
                lines.append('- ' + json.dumps(word, ensure_ascii=False))
            lines.append('')
        if row.get('regions'):
            lines += ['### Region coverage (display only)', '']
            for region in row['regions']:
                lines.append(f"- {region['bbox_pixels']}: {region['status']}")
            lines.append('')
    with _directory(destination.absolute(), create=True) as fd:
        _publish_new(fd, identity + '.txt', text.encode())
        _publish_new(fd, identity + '.json', _bytes(bundle))
        _publish_new(fd, identity + '.md', '\n'.join(lines).encode())
    return destination / (identity + '.md')


def as_sections(bundle: dict):
    """Pass the SAME canonical content to the unchanged production PDF chunker."""
    from pdf_extractor import ArticleSections
    from core.extraction_status import ExtractionStatus
    payload = bundle['payload']
    rows = bundle['pages']
    ok = tuple(r['binding']['page'] for r in rows if r['status'] in _SUCCESS)
    skipped = tuple(r['binding']['page'] for r in rows if r['status'] == 'not_attempted')
    failed = tuple(r['binding']['page'] for r in rows if r['status'] not in _SUCCESS
                   and r['status'] != 'not_attempted')
    sections = {f'Page {r["binding"]["page"]}': r['text'] for r in rows}
    return ArticleSections(full_text=payload['text'], sections=sections,
                           section_order=list(sections), page_count=bundle['page_count'],
                           truncated=bundle['coverage'] == 'partial',
                           extraction_status=ExtractionStatus(
                               completeness='partial' if failed or skipped else 'unknown',
                               origin='page_native_tesseract', profile=payload['recipe'],
                               source_sha256=payload['source_sha256'],
                               requested_range=tuple(bundle['range']),
                               warnings=(bundle['warning'],), completed_pages=ok,
                               attempted_pages=tuple(r['binding']['page'] for r in rows if r['attempt']),
                               failed_pages=failed, skipped_pages=skipped))
