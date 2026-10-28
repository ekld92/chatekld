"""Graph scoring must not credit isolated counts or self-reported confidence."""
import copy
import json

import pytest

from scripts.eval_visual_graph import evaluate_graph, parse_graph


def graph():
    return {'nodes': ['A', 'B', 'C'],
            'edges': [{'source': 'A', 'target': 'B', 'count': 7},
                      {'source': 'B', 'target': 'C', 'count': 2}],
            'uncertainties': []}


def test_reverse_edge_is_same_relationship_but_never_indexable():
    candidate = graph()
    candidate['edges'][0].update(source='B', target='A')
    result = evaluate_graph(graph(), candidate)
    assert result['matches_reference'] and result['matched'] == 2
    assert not result['indexable']


@pytest.mark.parametrize('fault', ['endpoints', 'count', 'duplicate', 'missing',
                                   'uncertain', 'extra_node'])
def test_preserved_counts_do_not_hide_wrong_associations_or_omissions(fault):
    candidate = graph()
    if fault == 'endpoints':
        candidate['edges'][0]['target'] = 'C'
    elif fault == 'count':
        candidate['edges'][0]['count'] = 2
    elif fault == 'duplicate':
        candidate['edges'].append(copy.deepcopy(candidate['edges'][0]))
    elif fault == 'missing':
        candidate['edges'].pop()
    elif fault == 'uncertain':
        candidate['uncertainties'].append('One line cannot be traced')
    else:
        candidate['nodes'].append('D')
    assert not evaluate_graph(graph(), candidate)['matches_reference']


@pytest.mark.parametrize('value', [True, '7', 7.0, -1, None])
def test_noninteger_or_invalid_counts_are_not_coerced(value):
    candidate = graph()
    candidate['edges'][0]['count'] = value
    with pytest.raises(ValueError):
        evaluate_graph(graph(), candidate)


def test_json_fence_is_unwrapped_without_repairing_bad_json():
    assert parse_graph('```json\n' + json.dumps(graph()) + '\n```') == graph()
    with pytest.raises(ValueError):
        parse_graph('{"nodes": [')
    with pytest.raises(ValueError):
        parse_graph('Result: ' + json.dumps(graph()))


def test_reference_with_duplicate_edges_is_refused():
    reference = graph()
    reference['edges'].append(copy.deepcopy(reference['edges'][0]))
    with pytest.raises(ValueError):
        evaluate_graph(reference, graph())


def test_duplicate_json_fields_are_not_silently_overwritten():
    text = json.dumps(graph()).replace('"count": 7', '"count": 999, "count": 7')
    with pytest.raises(ValueError, match='Duplicate JSON field'):
        parse_graph(text)
