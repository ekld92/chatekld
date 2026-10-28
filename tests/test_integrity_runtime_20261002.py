"""Adversarial runtime regressions; synthetic files and transports only."""
import threading
from unittest.mock import patch

import pytest


def test_bounded_reservation_precedes_thread_start():
    from api.bounded import run_bounded_action
    original = threading.Thread.start
    registered, release = threading.Event(), threading.Event()
    results = []

    def start(thread):
        if thread.name == 'bounded-action-start-race' and not registered.is_set():
            registered.set()
            assert release.wait(5)
        return original(thread)

    with patch.object(threading.Thread, 'start', start):
        first = threading.Thread(target=lambda: results.append(
            run_bounded_action('start-race', lambda _: 'first', deadline_s=2)))
        first.start()
        try:
            assert registered.wait(2)
            second = run_bounded_action('start-race', lambda _: 'second', deadline_s=2)
        finally:
            release.set()
            first.join(5)
    assert second == (None, False, True)
    assert results == [('first', False, False)]


def test_bounded_failed_start_releases_reservation():
    from api.bounded import run_bounded_action
    with patch.object(threading.Thread, 'start', side_effect=RuntimeError('start failed')):
        with pytest.raises(RuntimeError, match='start failed'):
            run_bounded_action('failed-start', lambda _: None, deadline_s=1)
    assert run_bounded_action('failed-start', lambda _: 'ok', deadline_s=1) == ('ok', False, False)


@pytest.mark.parametrize('pages', [200, 1100])
def test_pdf_prefix_reaches_real_extractor(tmp_path, pages):
    import fitz
    import pdf_extractor
    from services.pdf_service import _extract_all_pages
    path = tmp_path / 'many.pdf'
    with fitz.open() as doc:
        for _ in range(pages):
            doc.new_page()
        doc.save(path)
    original = pdf_extractor.extract_structured_from_pdf
    seen = []

    def extract(*args, **kwargs):
        seen.append(kwargs.copy())
        return original(*args, **kwargs)

    with patch.object(pdf_extractor, 'extract_structured_from_pdf', extract), patch.object(
        pdf_extractor, '_try_markitdown_extraction', side_effect=AssertionError('whole-file fallback')
    ):
        _extract_all_pages(str(path), ocr_cb=None, max_pages=151)
    assert seen[0]['end_page'] == 151


@pytest.mark.parametrize('kind', ['ocr', 'vision'])
def test_rejected_config_does_not_change_runtime(kind):
    from app import app
    from services import vision
    manager = getattr(vision, 'glm_ocr_manager' if kind == 'ocr' else 'vision_manager')
    with patch.object(manager, 'set_provider') as provider, patch.object(manager, 'set_model') as model:
        response = app.test_client().post('/api/config', json={f'{kind}_provider': 'invalid',
            f'{kind}_model': 'x' * 121}, headers={'X-Requested-With': 'ChatEKLD'})
    assert response.status_code == 200
    assert set(response.json['dropped']) == {f'{kind}_provider', f'{kind}_model'}
    provider.assert_not_called()
    model.assert_not_called()


def test_failed_config_save_does_not_change_runtime():
    from app import app
    from services.vision import glm_ocr_manager
    with patch.dict(app.config, {'PROPAGATE_EXCEPTIONS': False}), patch(
        'api.routes.config.save_config', side_effect=OSError('disk failed')
    ), patch.object(
        glm_ocr_manager, 'set_model'
    ) as model:
        response = app.test_client().post('/api/config', json={'ocr_model': 'fixture'},
                                         headers={'X-Requested-With': 'ChatEKLD'})
    assert response.status_code == 500
    model.assert_not_called()


def test_server_lease_excludes_second_start_and_releases(tmp_path):
    from core import server_info
    with patch.object(server_info, 'SERVER_INFO_FILE', str(tmp_path / 'server.json')):
        lease = server_info.acquire_server_lease()
        try:
            with pytest.raises(server_info.ServerAlreadyRunning):
                server_info.acquire_server_lease()
        finally:
            lease.close()
        server_info.acquire_server_lease().close()


def test_server_lease_refuses_legacy_live_owner(tmp_path):
    from core import server_info
    with patch.object(server_info, 'SERVER_INFO_FILE', str(tmp_path / 'server.json')), patch.object(
        server_info, 'read_server_info', return_value={'pid': 98765}
    ), patch.object(server_info, 'pid_alive', return_value=True):
        with pytest.raises(server_info.ServerAlreadyRunning):
            server_info.acquire_server_lease()


def test_simultaneous_server_processes_have_one_owner(tmp_path):
    import os
    import subprocess
    import sys
    script = '''
import sys
from core import server_info
server_info.SERVER_INFO_FILE = sys.argv[1]
sys.stdin.readline()
try:
    lease = server_info.acquire_server_lease()
except server_info.ServerAlreadyRunning:
    print('busy', flush=True)
else:
    print('owner', flush=True)
    sys.stdin.readline()
    lease.close()
'''
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path),
           'CHATEKLD_BASE_DIR': str(tmp_path / 'base'), 'PYTHONPATH': os.getcwd()}
    children = [subprocess.Popen([sys.executable, '-c', script, str(tmp_path / 'server.json')],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env) for _ in range(2)]
    try:
        for child in children:
            child.stdin.write('start\n')
            child.stdin.flush()
        states = [child.stdout.readline().strip() for child in children]
        assert sorted(states) == ['busy', 'owner']
        owner = children[states.index('owner')]
        owner.stdin.write('stop\n')
        owner.stdin.flush()
        for child in children:
            assert child.wait(timeout=5) == 0
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def test_launchers_acquire_before_loading_services():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for file, function, service_call in [('launch.py', 'main', '_load_runtime'),
                                        ('cli/main.py', 'server_cmd', 'load_env_files')]:
        tree = ast.parse((root / file).read_text())
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == function)
        calls = {node.func.id: node.lineno for node in ast.walk(fn)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert calls['acquire_server_lease'] < calls[service_call]


@pytest.mark.parametrize('kind', ['ocr', 'vision'])
def test_empty_accepted_model_applies_after_save(kind):
    from app import app
    from services import vision
    manager = getattr(vision, 'glm_ocr_manager' if kind == 'ocr' else 'vision_manager')
    with patch.object(manager, 'set_model') as model:
        response = app.test_client().post('/api/config', json={f'{kind}_model': ''},
                                         headers={'X-Requested-With': 'ChatEKLD'})
    assert response.status_code == 200
    assert response.json['dropped'] == []
    model.assert_called_once_with('')


def test_concurrent_config_persist_and_apply_remain_in_order():
    from app import app
    from api.routes import config as route
    from core.config import load_config
    from services.vision import glm_ocr_manager
    saved_first, second_done, release_first = (threading.Event() for _ in range(3))
    original_save = route.save_config
    applied = []
    results = []

    def save(data):
        original_save(data)
        if data.get('ocr_model') == 'fixture-a':
            saved_first.set()
            assert release_first.wait(5)

    def request(model, done=None):
        try:
            results.append(app.test_client().post('/api/config', json={'ocr_model': model},
                           headers={'X-Requested-With': 'ChatEKLD'}).status_code)
        finally:
            if done:
                done.set()

    with patch.object(route, 'save_config', save), patch.object(glm_ocr_manager, 'set_model', applied.append):
        first = threading.Thread(target=request, args=('fixture-a',))
        second = threading.Thread(target=request, args=('fixture-b', second_done))
        first.start()
        try:
            assert saved_first.wait(2)
            second.start()
            assert not second_done.wait(.1), 'second save overtook first runtime application'
        finally:
            release_first.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
    assert results == [200, 200]
    assert applied == ['fixture-a', 'fixture-b']
    assert load_config()['ocr_model'] == applied[-1]
