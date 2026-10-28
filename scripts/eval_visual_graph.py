"""Exact development-only graph diagnostics; never grants indexing authority."""
from __future__ import annotations

from collections import Counter
import json


def parse_visual_json(text: str):
    """Accept JSON, optionally in one outer JSON fence; never repair its contents."""
    if not isinstance(text, str) or len(text) > 1_000_000:
        raise ValueError('Invalid visual response')
    value = text.strip()
    if value.startswith('```json\n') and value.endswith('\n```'):
        value = value[8:-4]
    def unique_fields(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = item
        return result

    return json.loads(value, object_pairs_hook=unique_fields)


def parse_graph(text: str) -> dict:
    """Read a graph without numerical or label normalization."""
    graph = parse_visual_json(text)
    _edges(graph)
    return graph


def _edges(graph: dict) -> Counter:
    if not isinstance(graph, dict) or set(graph) != {'nodes', 'edges', 'uncertainties'}:
        raise ValueError('Invalid graph schema')
    nodes, edges, uncertainties = (graph[k] for k in ('nodes', 'edges', 'uncertainties'))
    if (not isinstance(nodes, list) or not nodes or len(nodes) > 1000
            or any(not isinstance(n, str) or not n.strip() for n in nodes)
            or len(set(nodes)) != len(nodes)
            or not isinstance(edges, list) or len(edges) > 10000
            or not isinstance(uncertainties, list)
            or any(not isinstance(u, str) or not u.strip() for u in uncertainties)):
        raise ValueError('Invalid graph content')
    result: Counter = Counter()
    for edge in edges:
        if (not isinstance(edge, dict) or set(edge) != {'source', 'target', 'count'}
                or not isinstance(edge['source'], str)
                or not isinstance(edge['target'], str)
                or edge['source'] not in nodes or edge['target'] not in nodes
                or edge['source'] == edge['target']
                or type(edge['count']) is not int or edge['count'] <= 0):
            raise ValueError('Invalid graph edge')
        a, b = sorted((edge['source'], edge['target']))
        result[(a, b, edge['count'])] += 1
    return result


def evaluate_graph(reference: dict, candidate: dict) -> dict:
    """Compare undirected endpoints AND counts; duplicate or guessed edges fail.

    References are separately reviewed development evidence, not new frozen gold.
    Source/render identity must be checked by the caller before this pure scorer.
    Node spelling and counts remain exact. An empty uncertainty list is a model
    claim, not proof of completeness. Even perfect matching is not qualification.
    """
    expected, actual = _edges(reference), _edges(candidate)
    if reference['uncertainties'] or any(n != 1 for n in expected.values()):
        raise ValueError('Ambiguous reference')
    missing = expected - actual
    unexpected = actual - expected
    absent_nodes = sorted(set(reference['nodes']) - set(candidate['nodes']))
    extra_nodes = sorted(set(candidate['nodes']) - set(reference['nodes']))
    matched = sum((expected & actual).values())
    return {
        'scope': 'Supplemental exact development graph comparison only',
        'matched': matched, 'total': sum(expected.values()),
        'missing': [list(edge) for edge in missing.elements()],
        'unexpected': [list(edge) for edge in unexpected.elements()],
        'missing_nodes': absent_nodes, 'unexpected_nodes': extra_nodes,
        'uncertainties': list(candidate['uncertainties']),
        'matches_reference': not any((missing, unexpected, absent_nodes, extra_nodes,
                                      candidate['uncertainties'])),
        'indexable': False,
    }
