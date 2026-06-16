"""M0 harness: every package in the source tree imports cleanly (AC1, AC3)."""
import importlib
import pytest

PACKAGES = [
    "shelldon",
    "shelldon.core",
    "shelldon.broker",
    "shelldon.worker",
    "shelldon.transport",
    "shelldon.display",
    "shelldon.plugins",
    "shelldon.contracts",
]


@pytest.mark.parametrize("pkg", PACKAGES)
def test_package_imports(pkg):
    assert importlib.import_module(pkg) is not None
