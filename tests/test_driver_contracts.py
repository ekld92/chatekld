"""Contract ratchet for core/driver.py — the check that would have caught the
first cut's phantom APIs.

The reverted-in-spirit first driver called functions, methods, kwargs and config
keys that did not exist, and dispatched to HTTP endpoints that had to be right.
None of that was caught because the tests only exercised error paths. This test
asserts, cheaply and without a live server:

1. Every ``/api/...`` endpoint the driver's source targets is REGISTERED in the
   Flask app's URL map with a compatible method.
2. Every in-process symbol the driver imports at call time exists with a
   call-compatible signature.

If a future edit points the driver at a non-existent endpoint or a renamed
function, this fails immediately instead of at runtime in the CLI/MCP.
"""
import inspect
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class TestDriverEndpointContracts(unittest.TestCase):
    def test_every_registry_endpoint_is_registered_with_its_method(self):
        """Ratchet v2 (2026-07-22 audit T1): the driver's declarative ENDPOINTS
        table must match the Flask url_map on PATH **and METHOD** — the v1
        check asserted rule membership only, so a driver POST to a GET-only
        route passed. Body-key drift is covered by test_driver_replay.py."""
        from app import create_app
        from core.driver import ENDPOINTS, _OPTIONAL_ENDPOINT_KEYS

        app = create_app()
        # One path can be served by SEVERAL rules (GET and POST /api/config are
        # separate @route registrations) — merge methods per path.
        methods_by_path: dict = {}
        for r in app.url_map.iter_rules():
            methods_by_path.setdefault(r.rule, set()).update(r.methods)
        problems = []
        for key, (path, method) in ENDPOINTS.items():
            allowed = methods_by_path.get(path)
            if allowed is None:
                if key in _OPTIONAL_ENDPOINT_KEYS:
                    # Private-only subsystem (Maintenance, STRIP STEP 4): the
                    # route is legitimately absent on the public mirror. When
                    # PRESENT, the method check below still applies.
                    continue
                problems.append(f"{key}: {path} not registered")
            elif method not in allowed:
                problems.append(f"{key}: {method} not allowed on {path} "
                                f"(allows {sorted(allowed)})")
        self.assertEqual(problems, [], problems)

    def test_every_api_literal_in_driver_source_is_in_the_registry(self):
        """Completeness: any '/api/...' literal in driver source outside
        ENDPOINTS is a call the ratchet can't see (the v1 blindspot was the
        f-string-built /api/upload)."""
        import core.driver as driver_mod

        src = inspect.getsource(driver_mod)
        targeted = set(re.findall(r'["\'](/api/[a-zA-Z0-9_\-/]+)["\']', src))
        registry_paths = {p for p, _m in driver_mod.ENDPOINTS.values()}
        stray = sorted(targeted - registry_paths)
        self.assertEqual(stray, [],
                         f"/api literals outside ENDPOINTS (unratcheted): {stray}")

    def test_in_process_symbols_exist_and_are_callable(self):
        # The read-only in-process fallbacks and validators the driver imports.
        from core.config import load_config, resolve_chat_model
        from core.paths import resolve_under_root
        from rag.vault import obsidian_manager

        # Signatures the driver relies on.
        self.assertTrue(callable(load_config))
        # resolve_chat_model(cfg, provider_name)
        inspect.signature(resolve_chat_model).bind({}, "ollama")
        # resolve_under_root(raw, root, must_be_dir=...)
        inspect.signature(resolve_under_root).bind("sub", "/root", must_be_dir=True)
        # obsidian_manager.get_status() -> str|dict
        self.assertTrue(callable(obsidian_manager.get_status))
        inspect.signature(obsidian_manager.get_status).bind()
        # obsidian_manager.get_status_payload() -> dict (the rich shape the
        # driver's in-process status fallback returns since 2026-07-22 C3).
        self.assertTrue(callable(obsidian_manager.get_status_payload))
        inspect.signature(obsidian_manager.get_status_payload).bind()

    def test_config_reads_the_real_vault_key(self):
        # The first cut READ a non-existent 'vault_path' config key
        # (cfg.get("vault_path")); the real one is 'obsidian_vault_path'. Assert
        # the driver never reads the phantom key. (A response FIELD named
        # vault_path is fine — this checks the .get() read pattern only.)
        import core.driver as driver
        src = inspect.getsource(driver)
        self.assertIn('get("obsidian_vault_path")', src)
        self.assertNotIn('.get("vault_path")', src)
        self.assertNotIn(".get('vault_path')", src)


if __name__ == "__main__":
    unittest.main()
