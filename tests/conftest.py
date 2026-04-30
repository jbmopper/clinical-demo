"""Session-wide test setup.

The test suite must be hermetic from the developer's local `.env`
file: a flag toggled there (e.g. `BINDING_STRATEGY=two_pass`) must
never silently change which code path the unit tests exercise. We
hit this in practice when the bindings registry expansion went in
and a developer flipped `BINDING_STRATEGY=two_pass` to smoke-test
the live two_pass path -- legacy alias tests started consulting the
resolver, hitting the live VSAC API, and producing ConceptSets
that no longer matched the alias-table singletons by identity.

The autouse fixture below pins `Settings.model_config["env_file"]`
to `None` for every test, so `Settings()` constructs from defaults
(currently `binding_strategy="alias"`) plus whatever the test
explicitly monkeypatches. Tests that need `two_pass` continue to
opt in via the existing per-test `two_pass_settings` fixture in
`tests/matcher/test_concept_lookup.py`, which still works because
it monkeypatches `get_settings` directly inside the matcher module
and clears the singleton.

Also clears the `get_settings` lru_cache before each test so a
prior test's settings construction doesn't bleed into the next.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from clinical_demo.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _hermetic_settings() -> Iterator[None]:
    """Default every test to .env-independent Settings construction.

    Overridable per-test: tests that need a specific binding strategy
    or other env-driven value should monkeypatch `get_settings` (or
    construct a Settings instance directly), not write to .env.

    Manages the env_file override directly (not via monkeypatch) so
    we don't request the `monkeypatch` fixture here -- requesting it
    would push monkeypatch's setup to before any other autouse
    fixture in nested files (e.g. the langfuse shim's `_reset_caches`),
    which inverts the teardown order and breaks teardowns that depend
    on monkeypatch having already unwound (e.g. an lru_cache attr
    swapped to a lambda).
    """
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    try:
        yield
    finally:
        Settings.model_config["env_file"] = original
        get_settings.cache_clear()
