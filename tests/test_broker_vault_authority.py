"""Story 4.3 / Task 4 — the broker-only `surface_vault` authority seam.

Asserts the read+authorize GATE (AD-6) is the SOLE authorized vault-read path
(AD-2): the broker reads, traversal is rejected by construction, and the worker
(untrusted brain) exposes no `surface_vault` API at all.
"""

import importlib

import pytest

from shelldon.broker import vault


def test_surface_returns_seeded_content(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(mode=0o700)
    (vault_dir / "secret.md").write_text("the launch codes\n")

    assert vault.surface_vault(tmp_path, "secret") == "the launch codes\n"


def test_missing_key_returns_none(tmp_path):
    (tmp_path / "vault").mkdir(mode=0o700)
    assert vault.surface_vault(tmp_path, "nope") is None


@pytest.mark.parametrize("key", ["../secret", "a/b", "..", "", "with space"])
def test_unsafe_keys_rejected(key):
    assert vault.authorize_surface(key) is False


@pytest.mark.parametrize("key", ["../secret", "a/b", "..", "", "with space"])
def test_unsafe_keys_surface_none(tmp_path, key):
    (tmp_path / "vault").mkdir(mode=0o700)
    assert vault.surface_vault(tmp_path, key) is None


def test_safe_keys_authorized():
    for key in ["secret", "alex", "key-1", "a_b"]:
        assert vault.authorize_surface(key) is True


@pytest.mark.parametrize("key", ["café", "用户", "naïve"])
def test_unicode_keys_rejected(key):
    """`re.ASCII` keeps `\\w` ASCII-only — vault keys are internal IDs, not names."""
    assert vault.authorize_surface(key) is False


def test_non_utf8_content_surfaces_none(tmp_path):
    """A corrupt (non-UTF-8) vault file must not raise into the egress path."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(mode=0o700)
    (vault_dir / "secret.md").write_bytes(b"\xff\xfe not utf-8")

    assert vault.surface_vault(tmp_path, "secret") is None


def test_no_escape_outside_vault(tmp_path):
    """A crafted key cannot reach a file that lives OUTSIDE vault/."""
    (tmp_path / "vault").mkdir(mode=0o700)
    (tmp_path / "outside.md").write_text("escaped!\n")

    # The slug carries no separators, so `<root>/vault/../outside` is unreachable.
    assert vault.surface_vault(tmp_path, "../outside") is None


def test_worker_has_no_vault_read_path():
    """Structural (AD-2): the untrusted brain exposes no vault-read API."""
    worker = importlib.import_module("shelldon.worker.worker")
    forkserver = importlib.import_module("shelldon.worker.forkserver")

    assert not hasattr(worker, "surface_vault")
    assert not hasattr(forkserver, "surface_vault")
    # The seam is exported by exactly one module: the broker.
    assert hasattr(vault, "surface_vault")
    assert vault.surface_vault.__module__ == "shelldon.broker.vault"
