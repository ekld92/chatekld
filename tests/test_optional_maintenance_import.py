"""Boot-boundary regressions for the private Maintenance capability."""

from __future__ import annotations

import builtins
from unittest import mock

import pytest

import app


def _import_failure(exc: BaseException):
    real_import = builtins.__import__

    def controlled(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "api.routes.maintenance":
            raise exc
        return real_import(name, globals, locals, fromlist, level)

    return mock.patch("builtins.__import__", side_effect=controlled)


@pytest.mark.parametrize("missing_name", [
    "api.routes.maintenance",
    "maintenance",
])
def test_expected_public_strip_absence_disables_capability(missing_name):
    exc = ModuleNotFoundError(f"No module named {missing_name!r}")
    exc.name = missing_name
    with _import_failure(exc):
        assert app._load_optional_maintenance_blueprint() is None


def test_missing_transitive_dependency_fails_boot_boundary():
    exc = ModuleNotFoundError("No module named 'private_dependency'")
    exc.name = "private_dependency"
    with _import_failure(exc), pytest.raises(ModuleNotFoundError) as raised:
        app._load_optional_maintenance_blueprint()
    assert raised.value.name == "private_dependency"


def test_symbol_import_failure_fails_boot_boundary():
    with _import_failure(ImportError("cannot import name 'maintenance_bp'")), \
            pytest.raises(ImportError, match="maintenance_bp"):
        app._load_optional_maintenance_blueprint()


def test_import_time_module_defect_fails_boot_boundary():
    with _import_failure(RuntimeError("module initialization exploded")), \
            pytest.raises(RuntimeError, match="initialization exploded"):
        app._load_optional_maintenance_blueprint()
