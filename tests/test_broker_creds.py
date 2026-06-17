"""AC3: the credential lives only inside the broker and never on the bus."""

import re

import msgspec
import pytest

from shelldon.broker.glm import GLMProvider
from shelldon.contracts import Job, Result

_CRED = re.compile(r"token|key|secret|password|api_?key|authorization|credential", re.IGNORECASE)


def test_glm_requires_a_credential(monkeypatch):
    """The provider can't be built without the key — no silent keyless calls."""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        GLMProvider()


def test_wire_types_carry_no_credential_fields():
    """Creds never travel on the bus: neither Job nor Result has a cred-shaped field."""
    for struct in (Job, Result):
        for field in msgspec.structs.fields(struct):
            assert not _CRED.search(field.name), f"{struct.__name__}.{field.name} is cred-shaped"


def test_credential_not_exposed_on_provider_public_api(monkeypatch):
    """A constructed provider keeps the key private (not a public attribute)."""
    monkeypatch.setenv("GLM_API_KEY", "sk-fake-not-real")
    p = GLMProvider()
    public = {name: getattr(p, name) for name in vars(p) if not name.startswith("_")}
    assert all("sk-fake-not-real" not in str(v) for v in public.values())
