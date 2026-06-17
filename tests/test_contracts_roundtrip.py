"""M0 required test (AD-10): every envelope type round-trips encode→decode without loss.

Also guards the closed header (AD-11) and the no-creds-on-the-bus invariant (AD-2/NFR9).
"""

import re

import msgspec
import pytest

from shelldon.contracts import (
    SCHEMA_VERSION,
    Actor,
    Envelope,
    Job,
    MsgKind,
    Result,
    decode,
    encode,
)


def _envelopes():
    """One Envelope per body type, covering the header edge cases."""
    return [
        # Job body, with a turn_id (a turn-bound request)
        Envelope(
            id="env-1",
            kind=MsgKind.JOB,
            src=Actor.CORE,
            dst=Actor.BROKER,
            body=Job(payload="hello"),
            turn_id="turn-1",
        ),
        # Result body, no turn_id, broadcast-style dst=None
        Envelope(
            id="env-2",
            kind=MsgKind.RESULT,
            src=Actor.BROKER,
            dst=None,
            body=Result(ok=True, payload="hi"),
        ),
        # Result error variant (errors travel as Result, never an exception over the bus)
        Envelope(
            id="env-3",
            kind=MsgKind.RESULT,
            src=Actor.WORKER,
            dst=Actor.CORE,
            body=Result(ok=False, error="boom"),
            turn_id="turn-3",
        ),
    ]


@pytest.mark.parametrize("env", _envelopes())
def test_envelope_roundtrip(env):
    """encode→decode yields a value-equal Envelope with the correct concrete body type."""
    decoded = decode(encode(env))
    assert decoded == env
    assert type(decoded.body) is type(env.body)


def test_default_schema_version():
    env = Envelope(
        id="env-v",
        kind=MsgKind.JOB,
        src=Actor.CORE,
        dst=Actor.WORKER,
        body=Job(payload="x"),
    )
    assert env.v == SCHEMA_VERSION
    assert decode(encode(env)).v == SCHEMA_VERSION


def test_closed_header_fields():
    """AD-11: the header is exactly id/v/kind/src/dst/turn_id (plus the typed body)."""
    names = {f.name for f in msgspec.structs.fields(Envelope)}
    assert names == {"id", "v", "kind", "src", "dst", "turn_id", "body"}


_CRED_PATTERN = re.compile(
    r"token|key|secret|password|api_?key|authorization|credential", re.IGNORECASE
)


def test_job_carries_no_credentials():
    """AD-2 / NFR9: a Job envelope contains no credential fields — creds never travel on the bus."""
    for field in msgspec.structs.fields(Job):
        assert not _CRED_PATTERN.search(field.name), (
            f"Job field {field.name!r} looks like a credential — creds must not ride the bus"
        )


# --- Review hardening (AD-11 closed header, AD-10 versioning) ---------------


def test_unknown_wire_fields_rejected():
    """AD-11: the header is CLOSED — an envelope carrying an unknown field is rejected,
    not silently dropped, so a typo can't smuggle a field past the contract."""
    raw = msgspec.msgpack.encode(
        {
            "id": "x",
            "kind": "job",
            "src": "core",
            "dst": "broker",
            "body": {"type": "job", "payload": "hi"},
            "v": SCHEMA_VERSION,
            "turn_id": None,
            "rogue_field": "sneaky",
        }
    )
    with pytest.raises(msgspec.ValidationError):
        decode(raw)


def test_kind_must_match_body():
    """A header `kind` that contradicts the body tag is an invalid envelope —
    rejected at construction so the two can never drift."""
    with pytest.raises(ValueError):
        Envelope(
            id="x",
            kind=MsgKind.RESULT,  # contradicts the Job body
            src=Actor.CORE,
            dst=Actor.BROKER,
            body=Job(payload="hi"),
        )


def test_consistent_kind_accepted():
    """The happy path still constructs: kind agrees with body."""
    env = Envelope(
        id="x",
        kind=MsgKind.JOB,
        src=Actor.CORE,
        dst=Actor.BROKER,
        body=Job(payload="hi"),
    )
    assert decode(encode(env)) == env


def test_unsupported_schema_version_rejected():
    """AD-10: `v` is the schema version — an envelope from an unsupported version
    is rejected at decode, not accepted as valid."""
    raw = msgspec.msgpack.encode(
        {
            "id": "x",
            "kind": "job",
            "src": "core",
            "dst": "broker",
            "body": {"type": "job", "payload": "hi"},
            "v": SCHEMA_VERSION + 998,
            "turn_id": None,
        }
    )
    with pytest.raises(msgspec.ValidationError):
        decode(raw)
