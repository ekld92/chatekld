"""Cancelled app-state writes and durable partial mirror outcomes."""
from unittest import mock

import test_notesync_route as _base
import test_notesync_mirror as _mirror


def own_tests(cls):
    for base in cls.__mro__[1:]:
        for name in vars(base):
            if name.startswith('test_') and name not in vars(cls):
                setattr(cls, name, None)
    return cls


@own_tests
class TestCancelBases(_base.TestSyncState):
    def test_cancel_stops_unengaged_baselines(self):
        from api.routes import notesync as ns
        from notesync import base_store
        entry = {'key': 'a2020', 'row': {'item_key': 'ABCD1234', 'zotero_version': 1,
            'state': 'unbaselined', 'ctx': {'note_key': 'WXYZ5678', 'html': '<p>alpha</p>',
                                         'parts': ('', 'alpha', '')}}}
        plan = {'planned': [], 'keep_base': [entry], 'stale_bases': [], 'notes_dir': self.notes}
        result = ns._refresh_write(self.vault, plan, should_cancel=lambda: True)
        assert result['cancelled'] is True
        assert result['bases_written'] == 0
        assert base_store.load(self.vault, 'a2020') is None

    def test_cancel_stops_unengaged_stale_deletions(self):
        from api.routes import notesync as ns
        plan = {'planned': [], 'keep_base': [], 'stale_bases': ['old'], 'notes_dir': self.notes}
        with mock.patch.object(ns.base_store, 'delete_if_unchanged') as delete:
            result = ns._refresh_write(self.vault, plan, should_cancel=lambda: True)
        assert result['cancelled'] is True
        delete.assert_not_called()


@own_tests
class TestPartialCreate(_mirror.TestMirror):
    def test_mirror_reports_create_that_landed_before_journal_failure(self):
        from vault_history import journal
        recs = [self._rec_keyed('fresh2026', has_note=False)]
        real_save = journal.save
        calls = []

        def fail_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError('fixture disk failure')
            return real_save(*args, **kwargs)

        _, frames = self._mirror(recs, {'confirm': True}, extra=[
            mock.patch.object(journal, 'save', side_effect=fail_once)])
        assert (self.notes / 'fresh2026.md').exists()
        result = self._terminal(frames)
        assert result['stopped'] == 'create_failed'
        assert result['created'] == 1, result
