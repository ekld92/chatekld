"""Generation publication boundaries through route workers, without model calls."""
import importlib
import shutil
import threading
from pathlib import Path

import pytest
from flask import Flask, jsonify

from core.utils import RagOperationLock
from deckgen.latex import SectionOutput
from deckgen.outline import Section


@pytest.mark.parametrize('kind', ['deck', 'docgen'])
@pytest.mark.parametrize('overwrite', [False, True])
@pytest.mark.parametrize('figure_count', [1, 2])
@pytest.mark.parametrize('stop', ['cancel', 'ownership', 'none'])
def test_generation_stops_between_copy_and_publication(tmp_path, monkeypatch, kind, overwrite, figure_count, stop):
    route = importlib.import_module(f'api.routes.{kind}')
    output = tmp_path / 'output'
    output.mkdir()
    project = output / 'fixture'
    if overwrite:
        project.mkdir()
        (project / 'fixture.tex').write_bytes(b'original tex\r\n')
        (project / 'Makefile').write_bytes(b'original make\n')
    before = {p.name: p.read_bytes() for p in project.glob('*') if p.is_file()}
    sources = []
    for index in range(figure_count):
        source = tmp_path / f'figure-{index}.png'
        source.write_bytes(b'fixture figure')
        sources.append((str(source), str(project / 'figures' / source.name)))
    cfg = {'provider': 'ollama', 'llm': 'fixture', 'embed': 'fixture',
           'deck_resume_enabled': False, 'docgen_resume_enabled': False}
    monkeypatch.setattr(route, 'load_config', lambda: cfg)
    monkeypatch.setattr(route.obsidian_manager, 'get_vault_path', lambda: '')
    monkeypatch.setattr(route.obsidian_manager, 'get_status', lambda: 'idle')
    lock = RagOperationLock()
    monkeypatch.setattr(route.obsidian_manager, 'try_acquire_lock', lambda ttl: lock.try_acquire_epoch(ttl))
    monkeypatch.setattr(route.obsidian_manager, 'heartbeat', lock.heartbeat)
    monkeypatch.setattr(route.obsidian_manager, 'release_lock', lock.release)
    monkeypatch.setattr(route, 'InProcessChatRunner', lambda **kw: object())
    monkeypatch.setattr(route, 'request_outline', lambda *a, **kw: ([Section('Intro', ['point'])], None))
    body = r'\section{Intro} Body' if kind == 'docgen' else r'\section{Intro}\begin{frame}{Intro}Body\end{frame}'
    monkeypatch.setattr(route, 'generate_section', lambda *a, **kw: SectionOutput('Intro', body, False))
    resolver = 'resolve_and_copy_figures' if kind == 'docgen' else '_resolve_and_copy_deck_figures'
    monkeypatch.setattr(route, resolver, lambda tex, *a, **kw: (tex, set(), [], sources))
    cancelled = threading.Event()
    copies, new_epochs = [], []
    real_copy = shutil.copy2

    def copy_then_stop(src, dst):
        real_copy(src, dst)
        copies.append(dst)
        if stop == 'cancel':
            cancelled.set()
        elif stop == 'ownership':
            lock.force_release()
            new_epochs.append(lock.try_acquire_epoch(3600))

    monkeypatch.setattr(route.shutil, 'copy2', copy_then_stop)

    def run_worker(worker, **kwargs):
        frames = []
        try:
            worker(frames.append, cancelled)
        finally:
            kwargs['release']()
        return jsonify(frames=frames)

    monkeypatch.setattr(route, 'run_sse_worker', run_worker)
    app = Flask(__name__)
    app.register_blueprint(route.docgen_bp if kind == 'docgen' else route.deck_bp)
    docclass = 'article' if kind == 'docgen' else 'beamer'
    response = app.test_client().post(f'/api/{kind}/generate', json={
        'topic': 'Fixture', 'template_tex': rf'\documentclass{{{docclass}}}\begin{{document}}\section{{Example}}\end{{document}}',
        'out_dir': str(output), 'doc_name': 'fixture', 'deck_name': 'fixture',
        'overwrite': overwrite, 'citations_enabled': False, 'review_enabled': False})
    assert response.status_code == 200, response.json
    terminal = 'doc' if kind == 'docgen' else 'deck'
    frames = response.json['frames']
    if stop == 'none':
        assert len(copies) == figure_count
        assert any(terminal in frame for frame in frames)
        assert (project / 'fixture.tex').read_bytes() != before.get('fixture.tex')
        assert (project / 'Makefile').exists()
    else:
        assert len(copies) == 1
        assert not any(terminal in frame for frame in frames)
        assert {p.name: p.read_bytes() for p in project.glob('*') if p.is_file()} == before
        assert Path(copies[0]).read_bytes() == b'fixture figure'
        assert any('Copied figures may remain' in str(frame) for frame in frames)
        if stop == 'ownership':
            assert lock.heartbeat(new_epochs[0])  # Old worker did not release the new owner.
