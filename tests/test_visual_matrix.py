"""Visual matrix diagnostics preserve exact cell and source-color associations."""
import copy
import json

import pytest

from scripts.eval_visual_matrix import evaluate_matrix_row, parse_matrix_row


def row():
    return {'row': 'Example', 'arms': '12', 'cells': [
        {'column': 1, 'text': '-2 (-4 to 0)', 'color': 'medium green'},
        {'column': 2, 'text': '0.9 (0.8 to 1.1)', 'color': 'white'}],
        'uncertainties': []}


def test_order_is_explicit_and_a_perfect_row_has_no_index_authority():
    candidate = row()
    candidate['cells'].reverse()
    result = evaluate_matrix_row(row(), candidate)
    assert result['matches_reference'] and not result['indexable']
    assert parse_matrix_row(json.dumps(candidate)) == candidate


@pytest.mark.parametrize('fault', ['sign', 'interval', 'color', 'column', 'missing',
                                   'extra', 'arms', 'row', 'uncertainty'])
def test_fidelity_and_coverage_failures_are_not_silently_repaired(fault):
    candidate = row()
    if fault == 'sign':
        candidate['cells'][0]['text'] = '2 (-4 to 0)'
    elif fault == 'interval':
        candidate['cells'][0]['text'] = '-2 (-3 to 0)'
    elif fault == 'color':
        candidate['cells'][0]['color'] = 'light green'
    elif fault == 'column':
        candidate['cells'][0]['column'], candidate['cells'][1]['column'] = 2, 1
    elif fault == 'missing':
        candidate['cells'].pop()
    elif fault == 'extra':
        candidate['cells'].append({'column': 3, 'text': '1', 'color': 'white'})
    elif fault in {'arms', 'row'}:
        candidate[fault] = 'Different'
    else:
        candidate['uncertainties'] = ['Unreadable cell']
    result = evaluate_matrix_row(row(), candidate)
    assert not result['matches_reference'] and not result['indexable']


def test_duplicate_cells_and_json_fields_are_refused():
    candidate = row()
    candidate['cells'].append(copy.deepcopy(candidate['cells'][0]))
    with pytest.raises(ValueError):
        evaluate_matrix_row(row(), candidate)
    with pytest.raises(ValueError):
        parse_matrix_row(json.dumps(row()).replace('"arms": "12"',
                                                  '"arms": "99", "arms": "12"'))
