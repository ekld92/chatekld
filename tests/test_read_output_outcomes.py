"""Read workflow outcomes remain separate from provider billing success."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest


def test_empty_capped_deepseek_replay_keeps_usage_and_does_not_retry(monkeypatch):
    from core.llm.adapters.openai_compat import DeepSeekProvider
    from core.llm.usage import UsageTracker
    from rag import summarizer

    tracker = UsageTracker()
    provider = DeepSeekProvider(api_key='fixture')
    chunks = [NS(usage=NS(prompt_tokens=27118, completion_tokens=1800),
                 choices=[NS(delta=NS(content=None), finish_reason='length')])]
    client = NS(chat=NS(completions=NS(create=Mock(return_value=iter(chunks)))))
    monkeypatch.setattr(provider, '_client', lambda: client)
    monkeypatch.setattr('core.llm.adapters.openai.usage_tracker', tracker)
    factory = Mock(return_value=provider)
    monkeypatch.setattr('core.llm.factory.get_llm_provider', factory)
    monkeypatch.setattr(summarizer, 'load_config', lambda: {})
    infos = []
    with pytest.raises(ValueError, match='no usable summary text'):
        list(summarizer._stream_online(prompt='fixture', system_prompt='fixture',
             provider_name='deepseek', model='deepseek-flash', temperature=.3,
             top_p=.9, max_tokens=1800, info_cb=infos.append))
    assert client.chat.completions.create.call_count == 1
    assert factory.call_count == 1
    assert any('max_tokens=1800' in msg for msg in infos)
    records = tracker.records_since('', workflow='paper_summary')
    assert len(records) == 1
    assert records[0].success is True  # Transport, not usable workflow completion.
    assert records[0].output_tokens == 1800
    assert records[0].cost_usd > 0


def test_empty_map_part_stops_before_reduce(monkeypatch):
    from rag import summarizer
    calls = []
    def empty_part(**kwargs):
        calls.append(kwargs)
        yield '   '
    monkeypatch.setattr(summarizer, '_single_pass_stream', empty_part)
    monkeypatch.setattr(summarizer, 'load_config', lambda: {'context_window': 4096})
    with pytest.raises(ValueError, match='part 1/'):
        list(summarizer.summarise_stream('word ' * 12000, 'fixture',
                                        provider_name='lm_studio'))
    assert len(calls) == 1


def test_map_reduce_preserves_focus_and_generation_controls(monkeypatch):
    from rag import summarizer
    calls = []
    def part(**kwargs):
        calls.append(kwargs)
        yield 'limitations and outcomes'
    monkeypatch.setattr(summarizer, '_single_pass_stream', part)
    monkeypatch.setattr(summarizer, 'load_config', lambda: {'context_window': 4096})
    list(summarizer.summarise_stream('word ' * 12000, 'fixture',
         provider_name='lm_studio', focus_question='Describe limitations',
         system_prompt='fixture system', audience_modifier=' researcher',
         language='French', temperature=.45, top_p=.8, max_tokens=1800))
    assert len(calls) > 2
    for call in calls:
        assert call['focus_question'] == 'Describe limitations'
        assert call['sys_p'] == 'fixture system researcher Write in French.'
        assert call['temperature'] == .45
        assert call['top_p'] == .8
    assert all(call['max_tokens'] == 1024 for call in calls[:-1])
    assert calls[-1]['max_tokens'] == 1800


@pytest.mark.parametrize('raises', [False, True])
def test_route_empty_or_failed_summary_reports_error_and_preserves_usage(monkeypatch, raises):
    import json
    from app import app
    from api.routes import paper
    from core.database import init_db, get_db_connection
    from core.llm.types import LLMUsage
    from core.llm.usage import UsageTracker

    tracker = UsageTracker()
    monkeypatch.setattr('core.llm.usage.usage_tracker', tracker)
    def summary(*args, **kwargs):
        tracker.record(provider='deepseek', model='deepseek-flash',
                       workflow='paper_summary', usage=LLMUsage(input_tokens=27118, output_tokens=1800),
                       latency_ms=10, stream=True, success=True)
        if raises:
            raise ValueError('No usable summary text')
        yield '  '
    monkeypatch.setattr(paper, 'summarise_stream', summary)
    init_db()
    with get_db_connection() as conn:
        conn.execute('INSERT OR REPLACE INTO uploads(upload_id,filename,extracted_text) VALUES(?,?,?)',
                     ('empty-output-fixture', 'fixture.pdf', 'Disposable text'))
        conn.commit()
    response = app.test_client().post('/api/summarise',
        headers={'X-Requested-With': 'ChatEKLD'},
        json={'upload_id': 'empty-output-fixture', 'model': 'fixture'}, buffered=True)
    lines = response.get_data(as_text=True).splitlines()
    frames = [json.loads(line[6:]) for line in lines if line.startswith('data: ') and line != 'data: [DONE]']
    assert any('error' in frame for frame in frames)
    usage = next(frame['usage'] for frame in frames if 'usage' in frame)
    assert usage['output_tokens'] == 1800
    assert usage['cost_usd'] > 0
    assert lines.index('data: [DONE]') > next(i for i, line in enumerate(lines) if '"error"' in line)
