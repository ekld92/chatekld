"""Cached recovery must not require transmitting/decoding oversized originals."""
import hashlib
from unittest.mock import Mock

from rag.vault import ObsidianVaultManager, vision_manager


def manager_for(tmp_path):
    manager = object.__new__(ObsidianVaultManager)
    manager._IMAGE_MAX_BYTES = 8
    manager._emit = Mock()
    manager._image_cache_file = lambda *_: tmp_path / 'cached.txt'
    manager._legacy_image_cache_file = lambda *_: tmp_path / 'legacy.txt'
    return manager


def test_oversized_original_uses_existing_text_without_vision(tmp_path, monkeypatch):
    manager = manager_for(tmp_path)
    image = tmp_path / 'large.png'
    image.write_bytes(b'x' * 64)
    (tmp_path / 'cached.txt').write_text('Recovered text from bounded DeepSeek tiles')
    monkeypatch.setattr(vision_manager, 'describe_image', lambda *_: (_ for _ in ()).throw(AssertionError('Unexpected vision')))
    result = manager._extract_image_description(image, tmp_path, 'large.png', cache_only=True)
    assert result == 'Recovered text from bounded DeepSeek tiles'
    assert hashlib.sha256(image.read_bytes()).hexdigest() == hashlib.sha256(b'x' * 64).hexdigest()


def test_oversized_uncached_original_still_cannot_reach_vision(tmp_path, monkeypatch):
    manager = manager_for(tmp_path)
    image = tmp_path / 'large.png'
    image.write_bytes(b'x' * 64)
    calls = Mock()
    monkeypatch.setattr(vision_manager, 'describe_image', calls)
    assert manager._extract_image_description(image, tmp_path, 'large.png') == ''
    calls.assert_not_called()
    assert any('vault image cap' in str(c) for c in manager._emit.call_args_list)
