import json
from unittest import mock

import pytest

from scripts import check_dependency_audits as audits


@pytest.mark.parametrize('code, report, expected', [
    (1, {'error': {'code': 'ENOTFOUND'}}, 1),
    (1, {'vulnerabilities': {}}, 1),
    (0, {}, 1),
    (0, [], 1),
    (0, {'vulnerabilities': {'x': {}}}, 1),
    (2, {'vulnerabilities': {}}, 1),
    (0, {'vulnerabilities': {}}, 0),
    (1, {'vulnerabilities': {'x': {'severity': 'moderate'}}}, 0),
    (1, {'vulnerabilities': {'x': {'severity': 'high'}}}, 1),
])
def test_npm_operational_errors_never_pass(tmp_path, code, report, expected):
    lock = tmp_path / 'lock.txt'
    lock.write_text('')
    with mock.patch.object(audits, '_load', return_value=([], [], [])), \
            mock.patch.object(audits, '_lockfile', return_value=lock), \
            mock.patch.object(audits, '_run', side_effect=[
                (0, ''), (code, json.dumps(report)),
            ]):
        assert audits.main() == expected


def test_npm_waiver_filters_findings_without_hiding_errors(tmp_path):
    lock = tmp_path / 'lock.txt'
    lock.write_text('')
    with mock.patch.object(audits, '_load', return_value=([], [{'id': 'x'}], [])), \
            mock.patch.object(audits, '_lockfile', return_value=lock), \
            mock.patch.object(audits, '_run', side_effect=[
                (0, ''), (1, '{"vulnerabilities":{"x":{"severity":"high"}}}'),
            ]):
        assert audits.main() == 0


def test_no_resolve_preserves_hash_requirement_and_waivers(tmp_path):
    lock = tmp_path / 'lock.txt'
    lock.write_text('')
    with mock.patch.object(audits, '_load', return_value=([{'id': 'known'}], [], [])), \
            mock.patch.object(audits, '_lockfile', return_value=lock), \
            mock.patch.object(audits, '_run', side_effect=[
                (0, ''), (0, '{"vulnerabilities":{}}'),
            ]) as run:
        assert audits.main(no_resolve=True) == 0
    command = run.call_args_list[0].args[0]
    assert '--disable-pip' in command and '--require-hashes' in command
    assert command[-2:] == ['--ignore-vuln', 'known']
