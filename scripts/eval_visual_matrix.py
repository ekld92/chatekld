"""Exact row/column/value/color development diagnostics, not qualification."""
from __future__ import annotations

from scripts.eval_visual_graph import parse_visual_json

COLORS = {'white', 'light green', 'medium green', 'dark green',
          'light red', 'medium red', 'dark red'}


def _cells(row: dict) -> dict:
    if (not isinstance(row, dict)
            or set(row) != {'row', 'arms', 'cells', 'uncertainties'}
            or any(not isinstance(row[k], str) or not row[k].strip()
                   for k in ('row', 'arms'))
            or not isinstance(row['cells'], list) or len(row['cells']) > 1000
            or not isinstance(row['uncertainties'], list)
            or any(not isinstance(u, str) or not u.strip() for u in row['uncertainties'])):
        raise ValueError('Invalid matrix row')
    cells = {}
    for cell in row['cells']:
        if (not isinstance(cell, dict) or set(cell) != {'column', 'text', 'color'}
                or type(cell['column']) is not int or cell['column'] < 1
                or cell['column'] in cells
                or not isinstance(cell['text'], str) or not cell['text'].strip()
                or not isinstance(cell['color'], str) or cell['color'] not in COLORS):
            raise ValueError('Invalid or duplicate matrix cell')
        cells[cell['column']] = cell
    return cells


def parse_matrix_row(text: str) -> dict:
    row = parse_visual_json(text)
    _cells(row)
    return row


def evaluate_matrix_row(reference: dict, candidate: dict) -> dict:
    """Require exact row, arms, column, printed value and observed color.

    A caller separately binds the images, headers, source and completed response.
    No sign, interval, whitespace, color or digit correction is permitted here.
    Counts below are supplemental diagnostics against a reviewed development row.
    """
    expected, actual = _cells(reference), _cells(candidate)
    if (not expected or reference['uncertainties']
            or set(expected) != set(range(1, len(expected) + 1))):
        raise ValueError('Incomplete reference row')
    row_match = reference['row'] == candidate['row']
    checks = []
    for column, cell in expected.items():
        value = actual.get(column)
        checks.append({'column': column, 'expected': cell, 'observed': value,
                       'text_match': row_match and value is not None
                       and value['text'] == cell['text'],
                       'color_match': row_match and value is not None
                       and value['color'] == cell['color']})
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    arms_match = reference['arms'] == candidate['arms']
    return {
        'scope': 'Supplemental exact development matrix row comparison only',
        'row_match': row_match, 'arms_match': arms_match,
        'total': len(expected),
        'text_matched': sum(c['text_match'] for c in checks),
        'color_matched': sum(c['color_match'] for c in checks),
        'missing_columns': missing, 'unexpected_columns': extra, 'checks': checks,
        'uncertainties': list(candidate['uncertainties']),
        'matches_reference': row_match and arms_match and not missing and not extra
        and not candidate['uncertainties']
        and all(c['text_match'] and c['color_match'] for c in checks),
        'indexable': False,
    }
