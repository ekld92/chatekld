"""Disposable PDF/checkpoint tests; no actual OCR, model, vault or paid calls."""
import hashlib
import json
from pathlib import Path
import threading
from unittest.mock import Mock

import fitz
import pytest

from core.page_ocr import TesseractOCR, as_sections, export_pages, extract_pages


class Engine:
    def __init__(self, results=None):
        self.calls = 0
        self.results = results or []

    def identity(self):
        return {'fixture': 'v1'}

    def recognize(self, png, cancelled, heartbeat):
        self.calls += 1
        assert png.startswith(b'\x89PNG')
        if self.results:
            result = self.results.pop(0)
            if callable(result):
                return result()
            return result
        return {'status': 'ocr', 'text': f'page text {self.calls}', 'calls': 1, 'call_s': .01}


def pdf(tmp_path, kinds=('scan', 'scan', 'scan')):
    inputs = tmp_path / 'inputs'
    inputs.mkdir(exist_ok=True)
    source = inputs / 'test.pdf'
    image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    image.clear_with(255)
    with fitz.open() as doc:
        for kind in kinds:
            page = doc.new_page(width=150, height=150)
            if kind in {'scan', 'mixed'}:
                page.insert_image(page.rect, stream=image.tobytes('png'))
            if kind in {'native', 'mixed'}:
                page.insert_text((10, 20), 'Native evidence with 123 patients.\nSecond line: 42 controls.', fontsize=6)
        doc.save(source)
    return source


def run(tmp_path, source, engine, **kwargs):
    return extract_pages(source, source.parent, tmp_path / 'checkpoints', engine,
                         resource_guard=lambda: '', **kwargs)


def test_cancel_resume_reuses_completed_pages_and_exports_partial(tmp_path):
    source = pdf(tmp_path)
    stop = threading.Event()
    def first():
        stop.set()
        return {'status': 'ocr', 'text': 'First page, 12 participants.', 'calls': 1}
    engine = Engine([first])
    partial = run(tmp_path, source, engine, cancelled=stop.is_set)
    assert [r['status'] for r in partial['pages']] == ['ocr', 'not_attempted', 'not_attempted']
    output = export_pages(partial, tmp_path / 'exports', source.parent)
    assert 'partial' in output.read_text() and 'Page 2 — not_attempted' in output.read_text()
    assert 'No usable text' in output.read_text()
    stop.clear()
    complete = run(tmp_path, source, engine)
    assert engine.calls == 3
    assert complete['pages'][0] == partial['pages'][0]
    assert complete['coverage'] == 'processed_unverified'
    assert as_sections(complete).full_text == complete['payload']['text']
    full_export = export_pages(complete, tmp_path / 'exports', source.parent)
    assert full_export.with_suffix('.txt').read_text() == as_sections(complete).full_text
    assert output.exists()  # original partial snapshot retained
    engine.recognize = Mock(side_effect=AssertionError('cached pages must not invoke OCR'))
    cached = run(tmp_path, source, engine)
    assert cached['payload']['text'] == complete['payload']['text']


def test_failures_need_explicit_retry_and_persist_allowance(tmp_path):
    source = pdf(tmp_path, ('scan',))
    engine = Engine([{'status': 'timeout', 'text': '', 'calls': 1},
                     {'status': 'empty_unverified', 'text': '', 'calls': 1}])
    first = run(tmp_path, source, engine)
    assert as_sections(first).extraction_status.failed_pages == (1,)
    run(tmp_path, source, engine)
    assert engine.calls == 1
    retry = run(tmp_path, source, engine, retry_pages=frozenset({1}))
    assert retry['pages'][0]['attempt'] == 2
    run(tmp_path, source, engine, retry_pages=frozenset({1}))
    assert engine.calls == 2
    assert len(list((tmp_path / 'checkpoints').rglob('p*-a*.json'))) == 4


def test_service_failure_defers_pages_without_consuming_attempts(tmp_path):
    source = pdf(tmp_path)
    engine = Engine([{'status': 'unavailable', 'text': '', 'calls': 0}])
    result = run(tmp_path, source, engine)
    assert engine.calls == 1
    assert [r['attempt'] for r in result['pages']] == [1, 0, 0]
    assert result['stop_reason'] == 'engine_unavailable'
    assert as_sections(result).extraction_status.skipped_pages == (2, 3)


def test_two_timeouts_trip_circuit_breaker(tmp_path):
    source = pdf(tmp_path)
    engine = Engine([{'status': 'timeout', 'text': '', 'calls': 1}] * 2)
    result = run(tmp_path, source, engine)
    assert engine.calls == 2 and result['pages'][2]['status'] == 'not_attempted'


def test_mixed_pdf_routes_per_page_and_does_not_duplicate_native(tmp_path):
    source = pdf(tmp_path, ('native', 'scan', 'mixed', 'blank'))
    engine = Engine()
    result = run(tmp_path, source, engine)
    assert engine.calls == 1
    assert [r['status'] for r in result['pages']] == ['native', 'ocr', 'mixed_unverified', 'empty_unverified']
    assert '123' in result['pages'][2]['text']
    assert result['pages'][2]['native_text_sha256']
    assert result['coverage'] == 'partial'


def test_index_policy_retains_native_warning_and_transcribes_vector_only_page(tmp_path, monkeypatch):
    from core import page_layout

    source = pdf(tmp_path, ('native', 'blank', 'blank'))
    with fitz.open(source) as document:
        page = document[1]
        page.draw_rect(fitz.Rect(15, 15, 120, 120), color=(0, 0, 0))
        document.saveIncr()

    original = page_layout.native_page
    def flagged(page):
        result = original(page)
        if result['text']:
            result['warnings'] = ['rotated_text_reading_order_unverified']
        return result
    monkeypatch.setattr(page_layout, 'native_page', flagged)
    engine = Engine()
    result = run(tmp_path, source, engine, accept_mixed_native=True)
    assert engine.calls == 1
    assert result['coverage'] == 'processed_unverified'
    assert [row['status'] for row in result['pages']] == ['native', 'ocr', 'native']
    assert result['pages'][0]['original_status'] == 'native_partial'
    assert 'rotated_text_reading_order_unverified' in result['pages'][0]['quality_flags']
    assert result['pages'][2]['quality_flags'] == ['blank_page_no_extractable_content']


def test_corrupt_checkpoint_refused_without_inference(tmp_path):
    source = pdf(tmp_path, ('scan',))
    engine = Engine()
    run(tmp_path, source, engine)
    receipt = next((tmp_path / 'checkpoints').rglob('p00001-a1.json'))
    data = json.loads(receipt.read_text())
    data['record']['text'] = 'invented'
    receipt.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Altered'):
        run(tmp_path, source, engine)
    assert engine.calls == 1


def test_interrupted_reservation_is_not_success_and_retry_is_explicit(tmp_path):
    source = pdf(tmp_path, ('scan',))
    engine = Engine()
    run(tmp_path, source, engine)
    receipt = next((tmp_path / 'checkpoints').rglob('p00001-a1.json'))
    receipt.unlink()  # simulate process loss after reservation, disposable data only
    result = run(tmp_path, source, engine)
    assert result['pages'][0]['status'] == 'interrupted' and engine.calls == 1
    result = run(tmp_path, source, engine, retry_pages=frozenset({1}))
    assert result['pages'][0]['status'] == 'ocr' and engine.calls == 2


def test_changed_source_and_recipe_never_reuse_pages(tmp_path):
    source = pdf(tmp_path, ('scan',))
    engine = Engine()
    one = run(tmp_path, source, engine)
    with fitz.open(source) as doc:
        doc[0].insert_text((10, 20), 'new', fontsize=6)
        doc.saveIncr()
    two = run(tmp_path, source, engine)
    assert one['payload']['source_sha256'] != two['payload']['source_sha256']
    engine.identity = lambda: {'fixture': 'v2'}
    three = run(tmp_path, source, engine)
    assert two['payload']['recipe'] != three['payload']['recipe']
    assert engine.calls == 3


def test_changed_source_during_run_refuses_publication(tmp_path):
    source = pdf(tmp_path, ('scan',))
    def mutate():
        source.write_bytes(source.read_bytes() + b'\n% concurrent change')
        return {'status': 'ocr', 'text': 'retained snapshot evidence', 'calls': 1}
    with pytest.raises(ValueError, match='Source changed'):
        run(tmp_path, source, Engine([mutate]))
    assert list((tmp_path / 'checkpoints').rglob('p00001-a1.json'))
    assert not list((tmp_path / 'checkpoints').rglob('snapshot-*.json'))


def test_external_destinations_lock_and_no_clobber(tmp_path):
    from core.cache_exports import _locked
    source = pdf(tmp_path, ('scan',))
    with pytest.raises(ValueError, match='Unsafe'):
        extract_pages(source, source.parent, source.parent / 'bad', Engine())
    storage = tmp_path / 'checkpoints'
    storage.mkdir()
    with _locked(storage / 'worker.lock'), pytest.raises(BlockingIOError):
        run(tmp_path, source, Engine())
    result = run(tmp_path, source, Engine())
    with pytest.raises(ValueError, match='outside'):
        export_pages(result, source.parent / 'bad', source.parent)
    output = export_pages(result, tmp_path / 'exports', source.parent)
    output.write_text('user edit')
    with pytest.raises(ValueError, match='user-edited'):
        export_pages(result, tmp_path / 'exports', source.parent)
    assert output.read_text() == 'user edit'


def test_resource_stop_creates_no_attempts(tmp_path):
    source = pdf(tmp_path)
    engine = Engine()
    result = extract_pages(source, source.parent, tmp_path / 'checkpoints', engine,
                           resource_guard=lambda: 'initial_memory_pressure')
    assert engine.calls == 0
    assert all(r['status'] == 'not_attempted' for r in result['pages'])
    assert not list((tmp_path / 'checkpoints').rglob('*started.json'))


def test_tesseract_requires_verified_artifacts(tmp_path):
    executable = tmp_path / 'tesseract'
    executable.write_text('fixture')
    executable.chmod(0o700)
    (tmp_path / 'eng.traineddata').write_text('not the pinned language')
    with pytest.raises(ValueError, match='Unverified'):
        TesseractOCR(executable, tmp_path).identity()


def test_tesseract_cancellation_terminates_owned_child(tmp_path, monkeypatch):
    import core.page_ocr as module
    monkeypatch.setattr('core.ocr_resources.process_rss_bytes', lambda _: 1000)
    child = Mock()
    child.poll.return_value = None
    monkeypatch.setattr(module.subprocess, 'Popen', Mock(return_value=child))
    checks = iter([False, True])
    result = TesseractOCR(Path('/fake'), tmp_path).recognize(
        b'fake png', lambda: next(checks), lambda: None)
    assert result['status'] == 'cancelled'
    child.terminate.assert_called_once()
    child.wait.assert_called_once()
    command = module.subprocess.Popen.call_args
    assert command.kwargs['env']['OMP_THREAD_LIMIT'] == '1'
    assert 'shell' not in command.kwargs


def test_native_and_ocr_use_actual_pdf_chunker_unchanged(tmp_path, monkeypatch):
    from rag import vault
    from llama_index.core import Document
    source = pdf(tmp_path, ('native', 'scan'))
    result = run(tmp_path, source, Engine())
    sections = as_sections(result)
    # No manager initialization/provider; exercise the actual PDF branch directly.
    manager = vault.ObsidianVaultManager()
    chunks = list(manager._chunk_raw_documents([
        Document(text=sections.full_text, metadata={'source': 'test.pdf', 'extension': '.pdf',
                                                   'file_path': str(source)})], str(source.parent)))
    assert chunks
    exported = export_pages(result, tmp_path / 'exports', source.parent)
    text = exported.with_suffix('.txt').read_text()
    assert text == sections.full_text
    assert all(chunk.text in text for chunk in chunks)
    assert all('Coverage:' not in chunk.text for chunk in chunks)
    assert hashlib.sha256(text.encode()).hexdigest() == result['payload']['text_sha256']


def test_memory_guard_baseline_growth_and_missing_metrics(monkeypatch):
    from core import ocr_resources as module
    from types import SimpleNamespace
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    clock = [0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    outputs = iter(['vm.swapusage: used = 1024.00M\nkern.memorystatus_vm_pressure_level: 1',
                    'vm.swapusage: used = 1537.00M\nkern.memorystatus_vm_pressure_level: 1'])
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=next(outputs)))
    guard = module.MemoryGuard()
    assert guard() == ''
    clock[0] = 3
    assert guard() == 'swap_growth_over_512_mib'
    assert len(guard.samples) == 2
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='unavailable'))
    assert module.MemoryGuard()() == 'memory_monitor_unavailable'


def test_memory_guard_admits_warning_but_refuses_critical_baseline(monkeypatch):
    from core import ocr_resources as module
    from types import SimpleNamespace
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout='vm.swapusage: used = 1024.00M\nkern.memorystatus_vm_pressure_level: 2'))
    assert module.MemoryGuard()() == ''
    assert module.MemoryGuard(allow_initial_pressure=True)() == ''
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout='vm.swapusage: used = 1024.00M\nkern.memorystatus_vm_pressure_level: 4'))
    assert module.MemoryGuard(allow_initial_pressure=True)() == 'initial_memory_pressure'


@pytest.mark.parametrize('kind', ['native', 'scan'])
def test_warning_pressure_allows_bounded_extraction(tmp_path, monkeypatch, kind):
    from core import ocr_resources as module
    from types import SimpleNamespace
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout='vm.swapusage: used = 6400.00M\nkern.memorystatus_vm_pressure_level: 2'))
    source = pdf(tmp_path, (kind,))
    engine = Engine()
    result = extract_pages(source, source.parent, tmp_path / 'checkpoints', engine,
                           accept_mixed_native=True)
    assert result['coverage'] == 'processed_unverified'
    assert result['stop_reason'] == ''
    assert result['resources'][0]['pressure'] == 2
    assert engine.calls == (kind == 'scan')


def test_ranges_never_replay_outside_request(tmp_path):
    source = pdf(tmp_path)
    engine = Engine()
    part = run(tmp_path, source, engine, start_page=1, end_page=2)
    assert len(part['pages']) == 1 and part['pages'][0]['binding']['page'] == 2
    assert part['page_count'] == 3
    run(tmp_path, source, engine)
    assert engine.calls == 3
    with pytest.raises(ValueError, match='Retry pages'):
        run(tmp_path, source, engine, start_page=1, end_page=2, retry_pages=frozenset({1}))


def test_tesseract_timeout_retains_partial_and_has_no_internal_retry(tmp_path, monkeypatch):
    import core.page_ocr as module
    monkeypatch.setattr('core.ocr_resources.process_rss_bytes', lambda _: 1000)
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    child = Mock()
    child.poll.return_value = None
    def launch(command, **kwargs):
        Path(command[2] + '.txt').write_text('partial evidence')
        return child
    popen = Mock(side_effect=launch)
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    def heartbeat():
        clock[0] += 2
    result = TesseractOCR(Path('/fake'), tmp_path, timeout_s=1).recognize(
        b'fake', lambda: False, heartbeat)
    assert result['status'] == 'timeout' and result['text'] == 'partial evidence'
    assert result['calls'] == 1 and result['peak_sampled_rss_bytes'] == 1000
    assert popen.call_count == 1
    child.terminate.assert_called_once()


def test_text_memory_bound_retains_checkpoint_and_smaller_range_recovers(tmp_path, monkeypatch):
    import core.page_ocr as module
    source = pdf(tmp_path, ('scan', 'scan'))
    monkeypatch.setattr(module, '_MAX_RANGE_TEXT', 15)
    engine = Engine()
    result = run(tmp_path, source, engine)
    assert [r['status'] for r in result['pages']] == ['ocr', 'not_materialized']
    assert result['coverage'] == 'partial'
    assert len(result['payload']['text']) < 20
    recovered = run(tmp_path, source, engine, start_page=1, end_page=2)
    assert recovered['pages'][0]['status'] == 'ocr'
    assert engine.calls == 2


def test_tesseract_success_command_and_output_projection(tmp_path, monkeypatch):
    import core.page_ocr as module
    child = Mock()
    child.poll.return_value = 0
    child.returncode = 0
    def launch(command, **kwargs):
        assert command[command.index('-l') + 1] == 'eng+fra'
        assert command[command.index('--psm') + 1] == '3'
        assert command[command.index('--oem') + 1] == '1'
        assert 'tessedit_create_tsv=1' in command
        assert set(kwargs['env']) == {'PATH', 'LC_ALL', 'OMP_THREAD_LIMIT'}
        Path(command[2] + '.txt').write_text('12 participants\n')
        Path(command[2] + '.tsv').write_text('level\ttext\n5\t12\n')
        return child
    monkeypatch.setattr(module.subprocess, 'Popen', launch)
    result = TesseractOCR(Path('/fake'), tmp_path).recognize(b'png', lambda: False, lambda: None)
    assert result['status'] == 'ocr'
    assert result['text'] == '12 participants\n'
    assert result['tsv'].startswith('level')
    assert result['calls'] == 1
    assert result['peak_sampled_rss_bytes'] is None  # too short to sample
    child.terminate.assert_not_called()


def test_symlink_storage_refused(tmp_path):
    source = pdf(tmp_path, ('scan',))
    actual = tmp_path / 'actual'
    actual.mkdir()
    link = tmp_path / 'checkpoints'
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match='Symlink'):
        run(tmp_path, source, Engine())


def test_runtime_change_during_page_is_partial_and_stops(tmp_path):
    source = pdf(tmp_path)
    engine = Engine()
    def changed():
        engine.identity = lambda: {'fixture': 'changed'}
        return {'status': 'ocr', 'text': 'unqualified evidence', 'calls': 1}
    engine.results = [changed]
    result = run(tmp_path, source, engine)
    assert result['pages'][0]['status'] == 'runtime_changed'
    assert result['pages'][1]['status'] == 'not_attempted'
    assert result['coverage'] == 'partial'
    assert engine.calls == 1


def test_memory_guard_sustained_critical_stop(monkeypatch):
    from core import ocr_resources as module
    from types import SimpleNamespace
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    clock = [0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout=f'vm.swapusage: used = 1024.00M\nkern.memorystatus_vm_pressure_level: {1 if clock[0] == 0 else 4}'))
    guard = module.MemoryGuard()
    assert guard() == ''
    clock[0] = 2
    assert guard() == ''
    clock[0] = 12
    assert guard() == 'critical_memory_pressure'


def test_language_installer_refuses_bad_download_and_preserves_existing(tmp_path, monkeypatch):
    import io
    import sys
    from scripts import install_ocr_languages as module
    monkeypatch.setattr(module, 'LANGUAGE_HASHES', {'eng.traineddata': hashlib.sha256(b'valid').hexdigest()})
    monkeypatch.setattr(sys, 'argv', ['installer', '--destination', str(tmp_path)])
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'wrong'))
    with pytest.raises(ValueError, match='hash mismatch'):
        module.main()
    assert not (tmp_path / 'eng.traineddata').exists()
    (tmp_path / 'eng.traineddata').write_bytes(b'user file')
    with pytest.raises(ValueError, match='hash mismatch'):
        module.main()
    assert (tmp_path / 'eng.traineddata').read_bytes() == b'user file'


def test_language_installer_replays_verified_files_without_network(tmp_path, monkeypatch):
    import sys
    from scripts import install_ocr_languages as module
    raw = b'verified fixture'
    monkeypatch.setattr(module, 'LANGUAGE_HASHES', {'eng.traineddata': hashlib.sha256(raw).hexdigest()})
    monkeypatch.setattr(sys, 'argv', ['installer', '--destination', str(tmp_path)])
    (tmp_path / 'eng.traineddata').write_bytes(raw)
    network = Mock(side_effect=AssertionError('existing artifact must be reused'))
    monkeypatch.setattr(module.urllib.request, 'urlopen', network)
    module.main()
    assert not network.called
    assert json.loads((tmp_path / 'receipt.json').read_text())['artifacts'][0]['bytes'] == len(raw)


def test_large_page_raster_fits_budget_before_allocation():
    from core.page_ocr import _bounded_render_scale
    # A scan whose point geometry would exceed the old 300-DPI limit.
    document = fitz.open()
    page = document.new_page(width=2400, height=3200)
    scale = _bounded_render_scale(page.rect, 300)
    assert scale < 300 / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    assert pix.width * pix.height <= 20_000_000
    document.close()


def test_oversized_scanned_pdf_reaches_engine_with_bounded_raster(tmp_path):
    from PIL import Image
    import io
    source = pdf(tmp_path, ('scan',))
    with fitz.open(source) as document:
        page = document[0]
        image = document.extract_image(page.get_images()[0][0])['image']
        page.set_mediabox(fitz.Rect(0, 0, 2400, 3200))
        page.insert_image(page.rect, stream=image)
        document.saveIncr()

    class BoundedEngine(Engine):
        def recognize(self, png, cancelled, heartbeat):
            with Image.open(io.BytesIO(png)) as image:
                assert image.width * image.height <= 20_000_000
            return super().recognize(png, cancelled, heartbeat)

    engine = BoundedEngine()
    result = run(tmp_path, source, engine)
    assert engine.calls == 1
    row = result['pages'][0]
    assert row['status'] == 'ocr'
    assert row['render_dpi'] < 300
    assert 'Resolution reduced' in row['render_warning']


def test_online_render_byte_budget_converts_only_transport():
    from core.page_ocr import _render_transport
    pix = Mock()
    oversized = b'P' * (20 * 1024**2 + 1)
    pix.tobytes.side_effect = lambda kind, **kwargs: oversized if kind == 'png' else b'bounded-jpeg'
    data, encoding = _render_transport(pix, online=True)
    assert data == b'bounded-jpeg' and encoding == 'jpeg95'
    pix.tobytes.assert_any_call('jpeg', jpg_quality=95)
    # The online-specific encoding policy must not alter local OCR inputs.
    assert _render_transport(pix, online=False) == (oversized, 'png')


def test_shared_guard_cannot_mutate_completed_snapshot(tmp_path, monkeypatch):
    from core import ocr_resources as module
    from types import SimpleNamespace
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout='vm.swapusage: used = 100.00M\nkern.memorystatus_vm_pressure_level: 2'))
    guard = module.MemoryGuard()
    source = pdf(tmp_path, ('native',))
    result = extract_pages(source, source.parent, tmp_path / 'checkpoints', Engine(),
                           resource_guard=guard)
    frozen = json.loads(json.dumps(result))
    clock[0] = 3
    guard()
    assert result == frozen
    # Later exports must contain exactly the evidence hashed in the snapshot.
    export_pages(result, tmp_path / 'exports', source.parent)
    assert result['resource_sample_count'] == 1
    assert len(guard.samples) == 2


def test_memory_evidence_is_bounded_without_resetting_safeguards(monkeypatch):
    from core import ocr_resources as module
    from types import SimpleNamespace
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    clock = [0.0]
    used = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout=f'vm.swapusage: used = {used[0]}M\nkern.memorystatus_vm_pressure_level: 2'))
    guard = module.MemoryGuard()
    for i in range(1000):
        clock[0] = i * 3
        assert guard() == ''
    assert guard.sample_count == 1000
    assert len(guard.samples) == guard.MAX_SAMPLES
    assert guard.samples[0]['monotonic_s'] == 0
    assert guard.samples[-1]['monotonic_s'] == clock[0]
    used[0] = 613
    clock[0] += 3
    assert guard() == 'swap_growth_over_512_mib'
    assert guard.samples[-1]['swap_used_mib'] == 613
