"""Per-answer Ask usage excludes concurrent Read and Create calls."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('provider', ['lm_studio', 'deepseek'])
def test_ask_export_usage_excludes_other_workflows(monkeypatch, provider):
    from app import app
    from api.routes import vault
    from core.llm.types import LLMUsage
    from core.llm.usage import UsageTracker

    tracker = UsageTracker()
    monkeypatch.setattr('core.llm.usage.usage_tracker', tracker)
    monkeypatch.setattr(vault, 'load_config', lambda: {'vault_agent_enabled': False})
    def answer():
        # Exact totals from the mixed field-test footer, all on the same model.
        for workflow, tokens_in, tokens_out in [
            ('paper_summary', 13913, 372), ('paper_summary', 1120, 270),
            ('deck_generate', 3153, 814), ('vault_rag', 3693, 264),
        ]:
            tracker.record(provider=provider, model='fixture-model', workflow=workflow,
                           usage=LLMUsage(input_tokens=tokens_in, output_tokens=tokens_out),
                           latency_ms=10, stream=True)
        yield 'Fixture answer'
    monkeypatch.setattr(vault.obsidian_manager, 'stream_chat',
                        lambda *a, **kw: SimpleNamespace(response_gen=answer(), source_nodes=[]))
    response = app.test_client().post('/api/obsidian/chat',
        headers={'X-Requested-With': 'ChatEKLD'},
        json={'message': 'Fixture question', 'provider': provider,
              'llm': 'fixture-model', 'agent_enabled': False}, buffered=True)
    assert response.status_code == 200
    frames = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
              if line.startswith('data: ') and line != 'data: [DONE]']
    usage = next(frame['usage'] for frame in frames if 'usage' in frame)
    assert usage['input_tokens'] == 3693
    assert usage['output_tokens'] == 264
    assert usage['requests'] == 1


def test_online_ask_tags_usage_without_replacing_rich_prompt_capture(monkeypatch):
    from core.llm.factory import _capture_request
    from core.llm.types import RetrievedChunk
    from rag.engine import SimpleQueryEngine

    capture = Mock()
    monkeypatch.setattr('core.prompt_capture.record', capture)
    engine = SimpleQueryEngine(index=Mock(), llm_name='deepseek-flash',
                               embed_name='fixture', provider_name='deepseek')
    response = engine._query_online_from_chunks(
        message='Fixture question',
        chunks=[RetrievedChunk(source='fixture.md', text='Source evidence', score=.9)],
        qa_template='{context_str}\n{query_str}', cfg={})
    assert response.request.workflow == 'vault_rag'
    _capture_request(response.request, 'deepseek')
    assert capture.call_count == 1
    assert capture.call_args.kwargs['context_chunks'] == 1
    assert capture.call_args.kwargs['query'] == 'Fixture question'


def test_ordinary_tagged_requests_still_capture(monkeypatch):
    from core.llm.factory import _capture_request
    from core.llm.types import LLMRequest
    capture = Mock()
    monkeypatch.setattr('core.prompt_capture.record', capture)
    _capture_request(LLMRequest(model='fixture', workflow='paper_summary'), 'deepseek')
    assert capture.call_count == 1
