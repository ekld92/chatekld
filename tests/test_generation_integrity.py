"""Synthetic regressions for the 2026-10-02 generation integrity audit."""
import io
import logging
from types import SimpleNamespace

import pytest

from core.llm.retry import retry_with_backoff
from core.llm.types import ErrorCategory, LLMError


def test_create_note_collision_at_publication_preserves_external(tmp_path, monkeypatch):
    from vault_history import create_note as module
    writer_name = "create_text_atomic" if hasattr(module, "create_text_atomic") else "write_text_atomic"
    publish = getattr(module, writer_name)
    monkeypatch.setattr(module.journal, 'save', lambda *a: None)
    def racing_write(path, text):
        from pathlib import Path
        Path(path).write_text('external')
        publish(path, text)
    monkeypatch.setattr(module, writer_name, racing_write)
    manifest = {'ops': []}
    _, status, error = module.create_note(tmp_path, {}, 'note.md', 'generated', manifest, prune=False)
    assert status == 'failed' and 'FileExistsError' in error
    assert (tmp_path / 'note.md').read_text() == 'external'
    assert manifest['ops'][0]['state'] == 'failed'


def test_retry_does_not_dispatch_at_deadline(monkeypatch):
    from core.llm import retry
    now = [0.0]
    calls = []
    error = LLMError(ErrorCategory.NETWORK, 'synthetic')
    monkeypatch.setattr(retry.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(retry.time, 'sleep', lambda n: now.__setitem__(0, now[0] + n))
    monkeypatch.setattr(retry.random, 'random', lambda: 0.5)
    def request():
        calls.append(now[0])
        raise error
    with pytest.raises(LLMError) as caught:
        retry_with_backoff(request, deadline_monotonic_s=1)
    assert caught.value is error
    assert calls == [0]


def test_retry_cancel_before_first_dispatch():
    calls = []
    with pytest.raises(LLMError):
        retry_with_backoff(lambda: calls.append(True), should_cancel=lambda: True)
    assert calls == []


def test_redacts_chained_exceptions_and_cached_exception():
    from core.llm.redact import RedactingFilter
    fake = 'sk-' + 'FAKESECRET' * 5
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    logger = logging.Logger('synthetic')
    logger.addHandler(handler)
    try:
        try:
            raise ValueError(fake)
        except ValueError as error:
            raise RuntimeError(fake) from error
    except RuntimeError:
        logger.exception('synthetic failure')
    assert fake not in stream.getvalue()
    assert 'ValueError' in stream.getvalue() and 'RuntimeError' in stream.getvalue()


def test_driver_retains_partial_on_eof_and_read_error(monkeypatch):
    from core import driver
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def __iter__(self):
            yield b'data: {"retrieval": [{"source": "fixture"}]}\n'
            yield b'data: {"token": "partial"}\n'
            raise OSError('synthetic interruption')
    monkeypatch.setattr(driver.urllib.request, 'urlopen', lambda *a, **kw: Response())
    monkeypatch.setattr(driver, 'resolve_base_url', lambda: 'http://127.0.0.1:9999')
    result = driver.vault_ask('question')
    assert result['answer'] == 'partial'
    assert result['sources'] == [{'source': 'fixture'}]
    assert result['status'] == 'interrupted'


def test_openai_requires_finish_reason(monkeypatch):
    from core.llm.adapters.openai import OpenAIProvider
    from core.llm.types import LLMRequest
    provider = OpenAIProvider()
    monkeypatch.setattr(provider, '_client', lambda: object())
    chunk = SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content='partial'), finish_reason=None)])
    monkeypatch.setattr(provider, '_create_with_heal', lambda *a, **k: iter([chunk]))
    stream = provider.stream(LLMRequest(model='fixture', messages=[]))
    with pytest.raises(LLMError):
        list(stream.response_gen)
    assert stream.final.text == 'partial'
    assert stream.final.error is not None


def test_checkpoint_vault_binding_and_legacy_refusal(tmp_path):
    import json
    from deckgen import checkpoint
    context_a = checkpoint.generation_context(str(tmp_path / 'a'), embed='existing')
    context_b = checkpoint.generation_context(str(tmp_path / 'b'), embed='existing')
    manifest = checkpoint.new_manifest(job_key='fixture', topic='x', slug='x', out_dir='x', sections=[], context=context_a)
    checkpoint.save(str(tmp_path), manifest)
    assert checkpoint.load(str(tmp_path), 'fixture', context=context_a) == manifest
    with pytest.raises(ValueError, match='context changed'):
        checkpoint.load(str(tmp_path), 'fixture', context=context_b)
    manifest.pop('context')
    (tmp_path / 'fixture.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='legacy'):
        checkpoint.load(str(tmp_path), 'fixture', context=context_a)


@pytest.mark.parametrize('name', ['anthropic', 'google', 'ollama', 'lm_studio'])
@pytest.mark.parametrize('complete', [False, True])
def test_protocol_terminal_and_partial(name, complete, monkeypatch):
    import contextlib
    import json
    from core.llm.adapters.anthropic import AnthropicProvider
    from core.llm.adapters.google import GoogleProvider
    from core.llm.adapters.local import LocalLLMProvider
    from core.llm.types import LLMRequest
    if name in ('ollama', 'lm_studio'):
        provider = LocalLLMProvider(name)
        if name == 'ollama':
            chunks = [SimpleNamespace(message=SimpleNamespace(content='partial'), done=complete)]
        else:
            chunks = [SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content='partial'), finish_reason='stop' if complete else None)])]
        monkeypatch.setattr(provider, '_provider', lambda: SimpleNamespace(stream_chat=lambda *a, **kw: iter(chunks)))
    else:
        provider = AnthropicProvider() if name == 'anthropic' else GoogleProvider()
        if name == 'anthropic':
            events = [{'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'partial'}}]
            if complete:
                events += [{'type': 'message_stop'}]
        else:
            events = [{'candidates': [{'content': {'parts': [{'text': 'partial'}]},
                                       **({'finishReason': 'STOP'} if complete else {})}]}]
        response = SimpleNamespace(status_code=200, iter_lines=lambda: iter('data: ' + json.dumps(e) for e in events))
        client = SimpleNamespace(stream=lambda *a, **kw: contextlib.nullcontext(response))
        monkeypatch.setattr(provider, '_client', lambda: client)
        if name == 'anthropic':
            monkeypatch.setattr(provider, '_headers', lambda: {})
        else:
            monkeypatch.setattr(provider, '_stream_with_heal', lambda *a: contextlib.nullcontext(response))
    stream = provider.stream(LLMRequest(model='fixture', messages=[]))
    if complete:
        assert list(stream.response_gen) == ['partial']
        assert stream.final.error is None
    else:
        with pytest.raises(LLMError):
            list(stream.response_gen)
        assert stream.final.error is not None
    assert stream.final.text == 'partial'


def test_checkpoint_context_observes_existing_files_without_modification(tmp_path):
    from deckgen import checkpoint
    index = tmp_path / 'index'
    index.mkdir()
    fixture = b'{"docstore/data": {"existing-id": {"text": "kept", "metadata": {"source": "a.md"}}}}'
    path = index / 'docstore.json'
    path.write_bytes(fixture)
    before = path.stat()
    context = checkpoint.generation_context(str(tmp_path / 'vault'), embed='existing', index_dir=str(index))
    assert context['index_files']['docstore.json'] == [before.st_ino, before.st_size, before.st_mtime_ns]
    assert path.read_bytes() == fixture
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert set(index.iterdir()) == {path}


def test_docgen_route_does_not_resume_another_vault(tmp_path, monkeypatch):
    from app import app
    import api.routes.docgen as route
    from deckgen.assemble import SectionOutput
    from deckgen.outline import Section
    vault_a, vault_b = tmp_path / 'a', tmp_path / 'b'
    vault_a.mkdir()
    vault_b.mkdir()
    current = [str(vault_a)]
    calls = []
    monkeypatch.setattr(route.obsidian_manager, 'get_vault_path', lambda: current[0])
    monkeypatch.setattr(route.obsidian_manager, 'try_acquire_lock', lambda *a, **k: 1)
    monkeypatch.setattr(route.obsidian_manager, 'release_lock', lambda *a, **k: None)
    monkeypatch.setattr(route, '_checkpoints_dir', lambda: str(tmp_path / 'checkpoints'))
    monkeypatch.setattr(route.md, 'request_outline', lambda *a, **k: ([Section(title='One', points=[])], ''))
    def section(*args, **kwargs):
        calls.append(current[0])
        return SectionOutput(title='One', body='## One\n' + current[0])
    monkeypatch.setattr(route.md, 'generate_section', section)
    # Keep A's completed checkpoint by refusing publication, using the real
    # route and checkpoint store. B must generate rather than reuse its text.
    monkeypatch.setattr(route, 'create_note', lambda *a, **k: ('', 'failed', 'synthetic refused publication'))
    client = app.test_client()
    payload = {'topic': 'Fixture', 'doc_name': 'fixture', 'confirm': True}
    for vault in (vault_a, vault_b):
        current[0] = str(vault)
        result = client.post('/api/docgen/generate-md', json=payload,
                             headers={'X-Requested-With': 'ChatEKLD'})
        result.get_data()
    assert calls == [str(vault_a), str(vault_b)]
    assert len(list((tmp_path / 'checkpoints').glob('*.json'))) == 2


@pytest.mark.parametrize('terminal', [False, True])
def test_driver_requires_done_marker(monkeypatch, terminal):
    from core import driver
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def __iter__(self):
            yield b'data: {"token": "partial"}\n'
            if terminal:
                yield b'data: [DONE]\n'
    monkeypatch.setattr(driver.urllib.request, 'urlopen', lambda *a, **kw: Response())
    result = driver._http_sse('obsidian_chat', {}, base_url='http://127.0.0.1:9999')
    assert result['tokens'] == 'partial'
    assert ('error' in result) is not terminal


def test_retry_cancel_after_sleep_prevents_dispatch(monkeypatch):
    from core.llm import retry
    calls = []
    cancelled = [False]
    error = LLMError(ErrorCategory.NETWORK, 'synthetic')
    def sleep(_duration):
        cancelled[0] = True
    def request():
        calls.append(True)
        raise error
    monkeypatch.setattr(retry.time, 'sleep', sleep)
    with pytest.raises(LLMError) as caught:
        retry.retry_with_backoff(request, should_cancel=lambda: cancelled[0])
    assert caught.value is error and len(calls) == 1


def test_create_only_two_writers_one_complete_result(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from core.utils import create_text_atomic
    gate = Barrier(2)
    path = tmp_path / 'note.md'
    def publish(text):
        gate.wait()
        try:
            create_text_atomic(str(path), text)
            return text
        except FileExistsError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, ['A' * 10000, 'B' * 10000]))
    winners = [text for text in results if text is not None]
    assert len(winners) == 1
    assert path.read_text() == winners[0]
    assert list(tmp_path.iterdir()) == [path]


def test_create_note_directory_fsync_uncertain_is_restorable(tmp_path, monkeypatch):
    from core import utils
    from vault_history import create_note as module
    monkeypatch.setattr(module.journal, 'save', lambda *a: None)
    original = utils.os.fsync
    calls = [0]
    def fail_directory(fd):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError('synthetic directory fsync failure')
        original(fd)
    monkeypatch.setattr(utils.os, 'fsync', fail_directory)
    manifest = {'ops': []}
    _, status, error = module.create_note(tmp_path, {}, 'note.md', 'generated', manifest, prune=False)
    assert status == 'uncertain' and 'published' in error
    assert (tmp_path / 'note.md').read_text() == 'generated'
    op = manifest['ops'][0]
    assert op['state'] == 'applied' and op['publication_uncertain']
    assert module.revert_create_note(tmp_path, {}, op)['ok']
    assert not (tmp_path / 'note.md').exists()


@pytest.mark.parametrize('legacy_kind', ['v1', 'v2'])
def test_docgen_route_refuses_legacy_resume_until_explicit_fresh(tmp_path, monkeypatch, legacy_kind):
    import json
    from app import app
    import api.routes.docgen as route
    from deckgen.assemble import SectionOutput
    from deckgen.outline import Section
    vault = tmp_path / 'vault'
    vault.mkdir()
    ckpts = tmp_path / 'checkpoints'
    ckpts.mkdir()
    monkeypatch.setattr(route, '_checkpoints_dir', lambda: str(ckpts))
    monkeypatch.setattr(route.obsidian_manager, 'get_vault_path', lambda: str(vault))
    monkeypatch.setattr(route.obsidian_manager, 'try_acquire_lock', lambda *a, **k: 1)
    monkeypatch.setattr(route.obsidian_manager, 'release_lock', lambda *a, **k: None)
    original_key = route.checkpoint.compute_job_key
    legacy_files = []
    def key(**kwargs):
        result = original_key(**kwargs)
        if ((legacy_kind == 'v1' and kwargs.get('_legacy'))
                or (legacy_kind == 'v2' and 'context' in kwargs
                    and 'embed_provider' not in kwargs['context'])):
            path = ckpts / (result + '.json')
            path.write_text(json.dumps({'version': 1 if legacy_kind == 'v1' else 2,
                                        'context': kwargs.get('context', {}),
                                        'job_key': result, 'outline': [], 'sections': {}}))
            legacy_files.append(path)
        return result
    monkeypatch.setattr(route.checkpoint, 'compute_job_key', key)
    calls = []
    def outline(*a, **k):
        calls.append(True)
        return [Section(title='One', points=[])], ''
    monkeypatch.setattr(route.md, 'request_outline', outline)
    monkeypatch.setattr(route.md, 'generate_section', lambda *a, **k: SectionOutput(title='One', body='fixture'))
    monkeypatch.setattr(route, 'create_note', lambda *a, **k: ('', 'failed', 'synthetic refusal'))
    client = app.test_client()
    payload = {'topic': 'Fixture', 'doc_name': 'fixture', 'confirm': True}
    response = client.post('/api/docgen/generate-md', json=payload, headers={'X-Requested-With': 'ChatEKLD'})
    assert 'legacy manifest' in response.get_data(as_text=True)
    assert calls == []
    payload['force_fresh'] = True
    response = client.post('/api/docgen/generate-md', json=payload, headers={'X-Requested-With': 'ChatEKLD'})
    response.get_data()
    assert len(calls) == 1
    assert all(path.exists() for path in legacy_files)


def test_vault_ask_sources_survive_later_terminal(monkeypatch):
    from core import driver
    monkeypatch.setattr(driver, 'resolve_base_url', lambda: 'http://127.0.0.1:9999')
    monkeypatch.setattr(driver, '_http_sse', lambda *a, **k: {
        'tokens': 'partial', 'events': [{'retrieval': [{'source': 'fixture'}]}],
        'terminal': {'note': 'later'}, 'error': 'interrupted'})
    result = driver.vault_ask('fixture')
    assert result['sources'] == [{'source': 'fixture'}]
    assert result['answer'] == 'partial' and result['status'] == 'interrupted'


@pytest.mark.parametrize('cancelled', [True, False])
def test_markdown_publication_cancellation_and_uncertain_receipt(tmp_path, monkeypatch, cancelled):
    from threading import Event
    from flask import jsonify
    from app import app
    import api.routes.docgen as route
    from deckgen.assemble import SectionOutput
    vault = tmp_path / 'vault'
    vault.mkdir()
    cancellation = Event()
    events = []
    writes = []
    monkeypatch.setattr(route.obsidian_manager, 'get_vault_path', lambda: str(vault))
    monkeypatch.setattr(route.obsidian_manager, 'try_acquire_lock', lambda **kw: 1)
    monkeypatch.setattr(route.obsidian_manager, 'release_lock', lambda *a: None)
    monkeypatch.setattr(route, 'index_preflight_msgs', lambda *a: [])
    monkeypatch.setattr(route, '_checkpoints_dir', lambda: str(tmp_path / 'checkpoints'))
    monkeypatch.setattr(route, '_run_sections_with_resume', lambda *a, **kw: (
        [], [SectionOutput(title='One', body='fixture')], 0, None))
    original_assemble = route.md.assemble_markdown
    def assemble(*a, **kw):
        if cancelled:
            cancellation.set()
        return original_assemble(*a, **kw)
    monkeypatch.setattr(route.md, 'assemble_markdown', assemble)
    monkeypatch.setattr(route.journal, 'load', lambda *a: {'ops': []})
    def publish(*a):
        writes.append(True)
        return 'receipt-1', 'uncertain', 'directory fsync failed'
    monkeypatch.setattr(route, 'create_note', publish)
    def run(worker, **kw):
        try:
            worker(events.append, cancellation)
        finally:
            kw['release']()
        return jsonify(events)
    monkeypatch.setattr(route, 'run_sse_worker', run)
    response = app.test_client().post('/api/docgen/generate-md',
        json={'topic': 'Fixture', 'doc_name': 'fixture', 'confirm': True},
        headers={'X-Requested-With': 'ChatEKLD'})
    assert response.status_code == 200
    if cancelled:
        assert writes == []
    else:
        assert writes == [True]
        assert events[-2]['doc_md']['op_id'] == 'receipt-1'
        assert events[-2]['doc_md']['rel'] == 'fixture.md'
        assert events[-2]['doc_md']['publication_uncertain'] is True
        assert 'was published' in events[-1]['error']


@pytest.mark.parametrize('kind', ['docgen', 'deck'])
@pytest.mark.parametrize('change_context', [True, False])
def test_latex_context_change_during_assembly_refuses_publication(tmp_path, monkeypatch, kind, change_context):
    import importlib
    from app import app
    from deckgen.assemble import SectionOutput
    from deckgen.outline import Section
    route = importlib.import_module('api.routes.' + kind)
    vault = [str(tmp_path / 'vault-a')]
    monkeypatch.setattr(route.obsidian_manager, 'get_vault_path', lambda: vault[0])
    monkeypatch.setattr(route, '_checkpoints_dir', lambda: str(tmp_path / 'checkpoints'))
    monkeypatch.setattr(route, 'request_outline', lambda *a, **kw: ([Section(title='One', points=[])], ''))
    monkeypatch.setattr(route, 'generate_section', lambda *a, **kw: SectionOutput(title='One', body='fixture'))
    original_assemble = route.assemble_with_template
    def assemble(*a, **kw):
        result = original_assemble(*a, **kw)
        if change_context:
            vault[0] = str(tmp_path / 'vault-b')
        return result
    monkeypatch.setattr(route, 'assemble_with_template', assemble)
    writes = []
    def scaffold(*a, **kw):
        # The same vault gate used by config/indexing is held through publication.
        assert not route.obsidian_manager.try_acquire_lock(ttl=3600)
        writes.append(True)
        return {}
    monkeypatch.setattr(route, 'scaffold_deck', scaffold)
    response = app.test_client().post('/api/' + kind + '/generate', json={
        'topic': 'Fixture', 'out_dir': str(tmp_path), 'citations_enabled': False,
        'template_tex': r'\documentclass{' + ('beamer' if kind == 'deck' else 'article') + r'}\begin{document}\end{document}',
    }, headers={'X-Requested-With': 'ChatEKLD'})
    text = response.get_data(as_text=True)
    assert response.status_code == 200, text
    if change_context:
        assert 'context changed' in text
        assert writes == []
    else:
        assert writes == [True]
    epoch = route.obsidian_manager.try_acquire_lock(ttl=3600)
    assert epoch
    route.obsidian_manager.release_lock(epoch)


@pytest.mark.parametrize('name', ['openai', 'anthropic', 'google'])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('stop', ['deadline', 'cancel'])
def test_parameter_heal_does_not_redispatch_after_stop(monkeypatch, name, streaming, stop):
    import contextlib
    import importlib
    import httpx
    from core.llm.types import LLMRequest
    module = importlib.import_module('core.llm.adapters.' + name)
    provider_class = getattr(module, {'openai': 'OpenAIProvider', 'anthropic': 'AnthropicProvider', 'google': 'GoogleProvider'}[name])
    provider = provider_class(max_retries=1)
    now = [0.0]
    cancelled = [False]
    calls = []
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    def reject(*a, **kw):
        calls.append(True)
        if stop == 'deadline':
            now[0] = 2.0
        else:
            cancelled[0] = True
        if name == 'openai':
            class ParameterError(Exception):
                status_code = 400
            raise ParameterError("Unsupported parameter: 'temperature'")
        return httpx.Response(400, json={'error': {
            'message': 'temperature invalid', 'type': 'invalid_request_error',
            'status': 'INVALID_ARGUMENT'}}, request=httpx.Request('POST', 'https://fixture.invalid'))
    if name == 'openai':
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=reject)))
    else:
        client = SimpleNamespace(post=reject, stream=lambda *a, **kw: contextlib.nullcontext(reject(*a, **kw)))
        if name == 'anthropic':
            monkeypatch.setattr(provider, '_headers', lambda: {})
        else:
            monkeypatch.setattr(provider, '_params', lambda **kw: {})
    monkeypatch.setattr(provider, '_client', lambda: client)
    request = LLMRequest(model='fixture-heal-guard', messages=[], temperature=0.5,
                         timeout_s=1.0, metadata={'should_cancel': lambda: cancelled[0]})
    # Each case must exercise an actual param rejection, independent of the
    # process-wide parameter compatibility cache populated by earlier cases.
    monkeypatch.setattr(module, 'is_rejected', lambda *a: False)
    with pytest.raises(LLMError, match='before dispatch'):
        if streaming:
            list(provider.stream(request).response_gen)
        else:
            provider.generate(request)
    assert calls == [True]


@pytest.mark.parametrize('directory_uncertain', [False, True])
def test_published_note_keeps_receipt_when_journal_save_fails(tmp_path, monkeypatch, directory_uncertain):
    from core.utils import CreatePublicationUncertainError
    from vault_history import create_note as module
    def fail_save(*a):
        raise OSError('synthetic journal failure')
    monkeypatch.setattr(module.journal, 'save', fail_save)
    if directory_uncertain:
        original = module.create_text_atomic
        def publish(path, text):
            original(path, text)
            raise CreatePublicationUncertainError('directory durability uncertain')
        monkeypatch.setattr(module, 'create_text_atomic', publish)
    manifest = {'ops': []}
    op_id, status, error = module.create_note(tmp_path, {}, 'note.md', 'generated', manifest, prune=False)
    assert status == 'uncertain'
    assert op_id == manifest['ops'][0]['id']
    assert (tmp_path / 'note.md').read_text() == 'generated'
    assert 'journal could not be confirmed' in error
    assert 'Do not assume Restore is available' in error
    assert manifest['ops'][0]['state'] == 'applied'


@pytest.mark.parametrize('kind', ['markdown', 'docgen', 'deck'])
@pytest.mark.parametrize('change_during_section', [False, True])
def test_generation_binds_effective_embedding_provider(tmp_path, monkeypatch, kind, change_during_section):
    import importlib
    from app import app
    from deckgen.assemble import SectionOutput
    from deckgen.outline import Section
    from core.config import load_config
    route = importlib.import_module('api.routes.' + ('docgen' if kind == 'markdown' else kind))
    cfg = dict(load_config())
    cfg.update(provider='openai', embed='same-model', embed_provider='ollama')
    monkeypatch.setattr(route, 'load_config', lambda: dict(cfg))
    vault = tmp_path / 'vault'
    vault.mkdir()
    monkeypatch.setattr(route.obsidian_manager, 'get_vault_path', lambda: str(vault))
    ckpts = tmp_path / 'checkpoints'
    monkeypatch.setattr(route, '_checkpoints_dir', lambda: str(ckpts))
    hooks = route.md if kind == 'markdown' else route
    monkeypatch.setattr(hooks, 'request_outline', lambda *a, **kw: (
        [Section(title='One', points=[]), Section(title='Two', points=[])], ''))
    calls = []
    def section(*a, **kw):
        calls.append(cfg['embed_provider'])
        result = SectionOutput(title='Section', body=cfg['embed_provider'])
        if change_during_section:
            cfg['embed_provider'] = 'lm_studio'
        return result
    monkeypatch.setattr(hooks, 'generate_section', section)
    publications = []
    if kind == 'markdown':
        monkeypatch.setattr(route.journal, 'load', lambda *a: {'ops': []})
        def create(*a):
            publications.append(True)
            return '', 'failed', 'fixture refusal preserves checkpoint'
        monkeypatch.setattr(route, 'create_note', create)
    else:
        def scaffold(*a, **kw):
            publications.append(True)
            raise route.ScaffoldError('fixture refusal preserves checkpoint')
        monkeypatch.setattr(route, 'scaffold_deck', scaffold)
    payload = {'topic': 'Fixture', 'doc_name': 'fixture', 'confirm': True,
               'provider': 'openai', 'model': 'fixture', 'out_dir': str(tmp_path),
               'citations_enabled': False,
               'template_tex': r'\documentclass{' + ('beamer' if kind == 'deck' else 'article') + r'}\begin{document}\end{document}'}
    endpoint = '/api/docgen/generate-md' if kind == 'markdown' else '/api/' + kind + '/generate'
    client = app.test_client()
    response = client.post(endpoint, json=payload, headers={'X-Requested-With': 'ChatEKLD'})
    text = response.get_data(as_text=True)
    assert response.status_code == 200, text
    if change_during_section:
        assert 'context changed' in text
        assert calls == ['ollama']
        assert publications == []
    else:
        assert calls == ['ollama', 'ollama']
        cfg['embed_provider'] = 'lm_studio'
        response = client.post(endpoint, json=payload, headers={'X-Requested-With': 'ChatEKLD'})
        text = response.get_data(as_text=True)
        assert response.status_code == 200, text
        assert calls == ['ollama', 'ollama', 'lm_studio', 'lm_studio']
        assert len(list(ckpts.glob('*.json'))) == 2
