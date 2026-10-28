"""Bounded geometry-only native reading; never repairs words or numbers.

Whitespace cuts use the occupied text area, not the physical page midpoint.
All text fragments survive exactly once, including captions and marginal text.
Tables and non-text marks remain unqualified, with explicit evidence warnings.
"""
from __future__ import annotations

import re
from statistics import median


def _gap(items: list[dict], axis: int, minimum: float):
    ordered = sorted(items, key=lambda item: item['bbox'][axis])
    edge = ordered[0]['bbox'][axis + 2]
    candidates = []
    for i, item in enumerate(ordered[1:], 1):
        start = item['bbox'][axis]
        if start - edge >= minimum:
            candidates.append((start - edge, i))
        edge = max(edge, item['bbox'][axis + 2])
    if not candidates:
        return None
    _, split = max(candidates)
    return ordered[:split], ordered[split:]


def reading_order(items: list[dict], height: float, depth: int = 0) -> list[dict]:
    """Recursive whitespace cuts. Never cut through a text bounding box."""
    if len(items) < 2 or depth >= 32:
        return sorted(items, key=lambda item: (item['bbox'][1], item['bbox'][0]))
    # A sustained empty vertical gutter takes precedence over line spacing.
    split = _gap(items, 0, max(12, height * 1.5))
    if split is None:
        split = _gap(items, 1, max(2, height * .6))
    if split is None:
        return sorted(items, key=lambda item: (item['bbox'][1], item['bbox'][0]))
    return [item for group in split for item in reading_order(group, height, depth + 1)]


def _ruled_regions(page, lines: list[dict], height: float) -> tuple[list[dict], bool]:
    """Keep repeated horizontal-rule bands together instead of splitting columns.

    No inferred cell values: each positioned text line belongs to one band once.
    This is still an unqualified table hypothesis, explicitly disclosed.
    """
    groups: dict[tuple, list] = {}
    for drawing in page.get_drawings():
        for item in drawing['items']:
            if item[0] != 'l' or abs(item[1].y - item[2].y) >= 1:
                continue
            left, right = sorted((item[1].x, item[2].x))
            if right - left > page.rect.width * .25:
                key = (round(left / 2) * 2, round(right / 2) * 2)
                groups.setdefault(key, []).append(item[1].y)
    remaining = list(lines)
    regions = []
    for (left, right), ys in groups.items():
        boundaries = sorted(set(ys))
        if len(boundaries) < 4:
            continue
        top, bottom = boundaries[0], boundaries[-1]
        selected = [line for line in remaining if
                    left - 2 <= line['bbox'][0] and line['bbox'][2] <= right + 2
                    and top <= (line['bbox'][1] + line['bbox'][3]) / 2 <= bottom]
        if not selected:
            continue
        selected_ids = {id(line) for line in selected}
        remaining = [line for line in remaining if id(line) not in selected_ids]
        bands: list[list[dict]] = [[] for _ in boundaries[1:]]
        for line in selected:
            middle = (line['bbox'][1] + line['bbox'][3]) / 2
            for index, end in enumerate(boundaries[1:]):
                if middle <= end:
                    bands[index].append(line)
                    break
        texts = []
        for band in bands:
            if not band:
                continue
            columns = _gap(band, 0, max(12, height * 1.5)) or (band,)
            texts.append(' | '.join('\n'.join(line['text'].strip() for line in
                                              reading_order(column, height)) for column in columns))
        regions.append({'text': '\n\n'.join(texts), 'bbox': [left, top, right, bottom]})
    return remaining + regions, bool(regions)


def native_page(page) -> dict:
    """Join adjacent same-baseline fragments, then order their occupied regions.

    Does not infer spaces inside a fused native span, remove hyphens, suppress
    headers or transcribe images. Original span text and coordinates are retained.
    """
    import fitz

    fragments = []
    warnings = []
    for block in page.get_text('dict', flags=fitz.TEXT_PRESERVE_WHITESPACE)['blocks']:
        for line in block.get('lines', []):
            if tuple(line['dir']) != (1.0, 0.0):
                warnings.append('rotated_text_reading_order_unverified')
            for span in line['spans']:
                if span['text'].strip():
                    fragments.append({'text': span['text'], 'bbox': list(span['bbox']),
                                      'baseline': span['origin'][1], 'size': span['size']})
    if not fragments:
        return {'text': '', 'warnings': [], 'fragments': []}
    height = median(f['size'] for f in fragments)
    # Group baseline fragments before sorting: some PDFs encode every word as
    # a separate line/block. The x gap prevents joining across a column gutter.
    rows: list[list[dict]] = []
    for fragment in sorted(fragments, key=lambda f: (f['baseline'], f['bbox'][0])):
        if rows and abs(fragment['baseline'] - rows[-1][0]['baseline']) <= height * .6:
            rows[-1].append(fragment)
        else:
            rows.append([fragment])
    lines = []
    for row in rows:
        current = None
        for fragment in sorted(row, key=lambda f: f['bbox'][0]):
            box = fragment['bbox']
            if current is not None and -height * .15 <= box[0] - current['bbox'][2] < height * 1.5:
                # Preserve explicit spaces; only separate distinct positioned
                # fragments when the PDF supplies a visible inter-word gap.
                gap = box[0] - current['bbox'][2]
                separator = ' ' if gap > height * .15 and not (
                    current['text'].endswith(' ') or fragment['text'].startswith(' ')) else ''
                current['text'] += separator + fragment['text']
                current['bbox'] = [current['bbox'][0], min(current['bbox'][1], box[1]),
                                   box[2], max(current['bbox'][3], box[3])]
            else:
                current = {'text': fragment['text'], 'bbox': list(box)}
                lines.append(current)
    lines, ruled = _ruled_regions(page, lines, height)
    ordered = reading_order(lines, height)
    text = '\n'.join(line['text'].strip() for line in ordered)
    if re.search(r'[^\W\d_]{35,}', text):
        warnings.append('long_native_token_possible_missing_spaces')
    images = page.get_image_info()
    if images:
        warnings.append('image_regions_not_transcribed')
    if page.first_annot is not None:
        warnings.append('annotations_not_transcribed')
    # Vector rules are evidence of layout requiring review, not certified tables.
    if ruled:
        warnings.append('ruled_layout_cell_associations_unverified')
    return {'text': text, 'warnings': sorted(set(warnings)), 'fragments': fragments}
