"""Real generation/config routes cannot admit an embedding-backend ABA switch."""
import importlib
import threading
import time

import pytest


@pytest.mark.parametrize('kind', ['markdown', 'docgen', 'deck'])
@pytest.mark.parametrize('disconnect', [False, True])
def test_generation_lease_blocks_backend_switch_until_worker_finishes(
        tmp_path, monkeypatch, kind, disconnect):
    from app import app
    from core.config import load_config
    from deckgen.outline import Section

    route = importlib.import_module('api.routes.' + ('docgen' if kind == 'markdown' else kind))
    vault = tmp_path / 'vault'
    vault.mkdir()
    monkeypatch.setattr(route.obsidian_manager, 'get_vault_path', lambda: str(vault))
    monkeypatch.setattr(route, '_checkpoints_dir', lambda: str(tmp_path / 'checkpoints'))
    hooks = route.md if kind == 'markdown' else route
    monkeypatch.setattr(hooks, 'request_outline', lambda *a, **kw: ([Section(title='One', points=[])], ''))
    entered, finish, exited = threading.Event(), threading.Event(), threading.Event()
    observed = []

    def section(*args, **kwargs):
        observed.append(load_config()['embed_provider'])
        entered.set()
        try:
            assert finish.wait(5)
            raise ValueError('synthetic stop before any publication')
        finally:
            exited.set()

    monkeypatch.setattr(hooks, 'generate_section', section)
    headers = {'X-Requested-With': 'ChatEKLD'}
    client = app.test_client()
    # A change completed before admission becomes the generation's context.
    assert client.post('/api/config', json={'embed_provider': 'lm_studio'}, headers=headers).status_code == 200
    payload = {'topic': 'Fixture', 'doc_name': 'fixture', 'confirm': True,
               'provider': 'openai', 'model': 'fixture', 'embed': 'same-model',
               'out_dir': str(tmp_path), 'citations_enabled': False,
               'template_tex': r'\documentclass{' + ('beamer' if kind == 'deck' else 'article') + r'}\begin{document}\end{document}'}
    endpoint = '/api/docgen/generate-md' if kind == 'markdown' else '/api/' + kind + '/generate'
    response = client.post(endpoint, json=payload, headers=headers, buffered=False)
    try:
        # Initial informational frames precede worker.start(); advance the
        # real stream until the worker reaches the blocked section.
        frames = iter(response.response)
        for _ in range(20):
            if entered.wait(.05):
                break
            next(frames)
        assert entered.wait(3)
        assert observed == ['lm_studio']
        if disconnect:
            response.close()
        for backend in ('ollama', 'lm_studio'):
            refusal = client.post('/api/config', json={'embed_provider': backend}, headers=headers)
            assert refusal.status_code == 409
            assert refusal.json['reason'] == 'audit_context_busy'
        assert load_config()['embed_provider'] == 'lm_studio'
        assert client.post('/api/config', json={'temperature': .3}, headers=headers).status_code == 200
    finally:
        finish.set()
        try:
            if entered.is_set():
                assert exited.wait(3)
            if not disconnect:
                response.get_data()
        finally:
            response.close()
    for _ in range(100):
        accepted = client.post('/api/config', json={'embed_provider': 'ollama'}, headers=headers)
        if accepted.status_code == 200:
            break
        time.sleep(.01)
    assert accepted.status_code == 200
    assert load_config()['embed_provider'] == 'ollama'
