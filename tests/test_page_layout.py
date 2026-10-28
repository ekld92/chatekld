"""Local fixtures: off-centre columns, table bands and bounded OCR regions."""
from collections import Counter

import fitz

from core.page_layout import native_page
from core.page_ocr import TesseractOCR, _ocr_uncertainties, _summary
from core.raster_layout import regions


def test_off_centre_columns_keep_paragraphs_and_every_number():
    with fitz.open() as doc:
        page = doc.new_page(width=800, height=800)
        for x, y, text in [(50, 80, 'Left opening 2010'), (50, 100, 'Left continuation 7.7%'),
                           (340, 80, 'Right opening 1980'), (340, 100, 'Right continuation 4.8%')]:
            # Separate word spans reproduce the problematic PDF encoding.
            cursor = x
            for word in text.split(' '):
                page.insert_text((cursor, y), word + ' ', fontsize=10)
                cursor += fitz.get_text_length(word + ' ', fontsize=10)
        text = native_page(page)['text']
        assert text.index('Left continuation') < text.index('Right opening')
        assert Counter(text.split()) == Counter('Left opening 2010 Left continuation 7.7% '
                                               'Right opening 1980 Right continuation 4.8%'.split())


def test_ruled_rows_keep_labels_with_multiline_values():
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=800)
        for y in (70, 130, 190, 250):
            page.draw_line((50, y), (500, y))
        for x, y, text in [(60, 90, 'Treatment A'), (250, 90, '12 participants'),
                           (250, 110, '30 minutes'), (60, 150, 'Treatment B'),
                           (250, 150, '24 participants'), (60, 210, 'Treatment C'),
                           (250, 210, '36 participants')]:
            page.insert_text((x, y), text, fontsize=10)
        result = native_page(page)
        assert 'Treatment A | 12 participants\n30 minutes' in result['text']
        assert result['text'].index('30 minutes') < result['text'].index('Treatment B')
        assert result['text'].count('24 participants') == 1
        assert 'ruled_layout_cell_associations_unverified' in result['warnings']


def test_small_logo_is_disclosed_without_full_page_ocr():
    with fitz.open() as doc:
        page = doc.new_page()
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
        pix.clear_with(0)
        page.insert_image(fitz.Rect(10, 10, 30, 30), stream=pix.tobytes('png'))
        page.insert_text((50, 100), 'Useful native paragraph 12.4')
        result = native_page(page)
        assert '12.4' in result['text']
        assert 'image_regions_not_transcribed' in result['warnings']


def test_raster_partition_covers_every_pixel_once():
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=800)
        for y in (100, 250, 400, 550):
            page.draw_line((60, y), (540, y))
        for x in (60, 300, 540):
            page.draw_line((x, 100), (x, 550))
        pix = page.get_pixmap()
        boxes = regions(pix.tobytes('png'))
        assert 4 < len(boxes) <= 48
        coverage = bytearray(pix.width * pix.height)
        for region in boxes:
            x0, y0, x1, y1 = region['bbox_pixels']
            for y in range(y0, y1):
                for x in range(x0, x1):
                    coverage[y * pix.width + x] += 1
        assert min(coverage) == max(coverage) == 1


def test_region_service_failure_defers_remaining_calls(tmp_path, monkeypatch):
    monkeypatch.setattr('core.raster_layout.regions', lambda _: [
        {'bbox_pixels': [0, 0, 10, 10], 'band': 0, 'psm': 6},
        {'bbox_pixels': [10, 0, 20, 10], 'band': 0, 'psm': 6}])
    monkeypatch.setattr('core.raster_layout.crop', lambda *_: b'crop')
    calls = []
    def unavailable(*_):
        calls.append(1)
        return {'status': 'unavailable', 'text': '', 'calls': 0, 'call_s': 0}
    monkeypatch.setattr(TesseractOCR, '_recognize_single', unavailable)
    result = TesseractOCR(tmp_path, tmp_path).recognize(b'\x89PNG', lambda: False, lambda: None)
    assert len(calls) == 1
    assert result['status'] == 'unavailable'
    assert result['regions'][1]['status'] == 'not_attempted'


def test_uncertain_identifier_is_retained_not_repaired():
    tsv = 'level\tconf\tleft\ttop\twidth\theight\ttext\n5\t42\t10\t20\t30\t40\tJa\n'
    assert _ocr_uncertainties(tsv) == [{'text': 'Ja', 'confidence': 42,
                                      'bbox_pixels': [10, 20, 30, 40]}]


def test_region_summary_replay_preserves_evidence_hashes():
    record = {'text': '12 | 34', 'regions': [
        {'text': '12', 'tsv': 'raw', 'status': 'ocr', 'bbox_pixels': [0, 0, 10, 10]}]}
    summary = _summary(record)
    assert summary == _summary(summary)
    assert record['regions'][0]['tsv'] == 'raw'
    assert 'tsv' not in summary['regions'][0]
