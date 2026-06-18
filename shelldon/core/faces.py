"""core/faces — the self-modifiable faces registry + mood→face mapping (AD-5/AD-7).

The pet's expressions are **data, not a hardcoded enum**: an editable `faces.toml`
the owner can tune today and the bot will extend itself (Story 3.4). Core owns it
(sole writer of soul data, AD-5), maps the drifting mood to a face token, and pushes
that token; the display stays a dumb renderer. A face entry is validated against a
closed schema (name + mood ranges + token) — so a typo is rejected even though the
*set* is open.

The file is corruption-tolerant (absent → seed defaults; corrupt → built-in
defaults + warn, never crash) and written atomically with comment preservation —
the same AD-7/AD-10 discipline `core/state.py` introduced for the checkpoint.
"""

import logging
import os
import tempfile
import tomllib
from pathlib import Path

import msgspec
import tomlkit

log = logging.getLogger("shelldon.core.faces")

#: Default registry location — one editable file beside the state checkpoint.
#: Always injectable; tests pass a `tmp_path` file and never touch real `$HOME`.
DEFAULT_FACES_PATH = Path.home() / ".shelldon" / "faces.toml"

#: Field bounds a face's selection ranges must stay within.
_VALENCE_RANGE = (-1.0, 1.0)
_AROUSAL_RANGE = (-1.0, 1.0)
_ENERGY_RANGE = (0.0, 1.0)

#: Returned when an (impossible, given the catch-all) empty/degenerate registry
#: matches nothing — the pet still has a face.
DEFAULT_FACE_TOKEN = "content"

_SEED_HEADER = (
    "shelldon faces — edit to add or tune expressions.\n"
    "Each face: a name, the mood region that selects it ([lo, hi] for\n"
    "valence/arousal/energy), and an optional render token (defaults to name).\n"
    "Faces are tried top-to-bottom; the first whose ranges all contain the\n"
    "current mood wins, so keep the broadest 'catch-all' face last."
)


class Face(msgspec.Struct, frozen=True):
    """One expression: the mood region that selects it and the token to render.

    Mutable RAM/data, but the struct itself is frozen — the registry replaces the
    list, it never edits a Face in place. `token` defaults to `name`."""

    name: str
    valence: tuple[float, float]
    arousal: tuple[float, float]
    energy: tuple[float, float]
    token: str = ""


#: The starter emotion set (AC3). Order = selection priority (first match wins);
#: `content` is the broad catch-all and MUST stay last so selection always resolves.
DEFAULT_FACES: list[Face] = [
    Face("low-battery", (-1.0, 1.0), (-1.0, 1.0), (0.0, 0.15)),
    Face("sleepy", (-1.0, 1.0), (-1.0, -0.3), (0.15, 0.45)),
    Face("grumpy", (-1.0, -0.2), (-1.0, 1.0), (0.15, 1.0)),
    Face("excited", (0.4, 1.0), (0.4, 1.0), (0.5, 1.0)),
    Face("curious", (0.0, 1.0), (0.1, 1.0), (0.15, 1.0)),
    Face("content", (-1.0, 1.0), (-1.0, 1.0), (0.15, 1.0)),
]

STARTER_NAMES = frozenset(f.name for f in DEFAULT_FACES)

_decoder = msgspec.convert  # dict -> Face, with type validation


def _validate_range(rng: tuple[float, float], bounds: tuple[float, float], label: str) -> None:
    lo, hi = rng
    if lo > hi:
        raise ValueError(f"{label} range {rng} is inverted (lo > hi)")
    if lo < bounds[0] or hi > bounds[1]:
        raise ValueError(f"{label} range {rng} is outside {bounds}")


def _validate_face(face: Face) -> None:
    """The closed schema, shared by load and add_face: non-empty name + in-range,
    well-ordered selection ranges. Raises ValueError on any violation."""
    if not face.name:
        raise ValueError("face name must be a non-empty string")
    _validate_range(face.valence, _VALENCE_RANGE, "valence")
    _validate_range(face.arousal, _AROUSAL_RANGE, "arousal")
    _validate_range(face.energy, _ENERGY_RANGE, "energy")


def _in(value: float, rng: tuple[float, float]) -> bool:
    lo, hi = rng
    return lo <= value <= hi


def select_face(faces: list[Face], valence: float, arousal: float, energy: float) -> str:
    """Pure mood→token: the first face whose ranges all contain the mood, else the
    default token. No I/O, no mutation — the unit Story 3.4 / Epic 5 reuse."""
    for f in faces:
        if _in(valence, f.valence) and _in(arousal, f.arousal) and _in(energy, f.energy):
            return f.token or f.name
    return DEFAULT_FACE_TOKEN


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (temp in same dir → fsync → os.replace) —
    the same crash-safety recipe as core/state.py (AD-10). A failure before the
    replace leaves the prior file intact and no stray temp behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _face_table(face: Face) -> tomlkit.items.Table:
    t = tomlkit.table()
    t["name"] = face.name
    t["valence"] = list(face.valence)
    t["arousal"] = list(face.arousal)
    t["energy"] = list(face.energy)
    if face.token:
        t["token"] = face.token
    return t


def _seed_document(faces: list[Face]) -> tomlkit.TOMLDocument:
    doc = tomlkit.document()
    for line in _SEED_HEADER.split("\n"):
        doc.add(tomlkit.comment(line))
    doc.add(tomlkit.nl())
    aot = tomlkit.aot()
    for face in faces:
        aot.append(_face_table(face))
    doc["face"] = aot
    return doc


class FaceRegistry:
    """Owns the in-RAM face list and the editable `faces.toml` behind it. Core is the
    sole writer (AD-5); `add_face` is the validated, atomic apply path Story 3.4 calls."""

    def __init__(self, faces: list[Face], path) -> None:
        self._faces = faces
        self._path = Path(path) if path is not None else None

    @property
    def faces(self) -> list[Face]:
        return self._faces

    @property
    def path(self) -> Path | None:
        return self._path

    @classmethod
    def load(cls, path) -> "FaceRegistry":
        """Restore from `path`, defaulting cleanly. Absent → seed the starter set to
        disk and use it. Corrupt/invalid → built-in defaults + a warning, never raise
        (AD-7 corruption tolerance)."""
        path = Path(path)
        if not path.exists():
            _atomic_write_text(path, tomlkit.dumps(_seed_document(DEFAULT_FACES)))
            return cls(list(DEFAULT_FACES), path)
        try:
            raw = tomllib.loads(path.read_text())
            entries = raw.get("face", [])
            faces = [msgspec.convert(e, Face) for e in entries]
            if not faces:
                raise ValueError("no faces defined")
            for f in faces:
                _validate_face(f)
        except (tomllib.TOMLDecodeError, msgspec.ValidationError, ValueError, OSError) as exc:
            log.warning("unusable faces file at %s (%s); falling back to defaults", path, exc)
            return cls(list(DEFAULT_FACES), path)
        return cls(faces, path)

    def select(self, valence: float, arousal: float, energy: float) -> str:
        return select_face(self._faces, valence, arousal, energy)

    def add_face(
        self,
        name: str,
        *,
        valence: tuple[float, float],
        arousal: tuple[float, float],
        energy: tuple[float, float],
        token: str = "",
        replace: bool = False,
    ) -> Face:
        """Validate and apply a face addition: reject a malformed or duplicate (unless
        `replace`) face WITHOUT mutating RAM or disk; on success update the in-RAM list
        AND atomically rewrite `faces.toml`, preserving its comments (tomlkit)."""
        face = Face(name=name, valence=tuple(valence), arousal=tuple(arousal), energy=tuple(energy), token=token)
        _validate_face(face)

        existing = next((i for i, f in enumerate(self._faces) if f.name == name), None)
        if existing is not None and not replace:
            raise ValueError(f"face {name!r} already exists (pass replace=True to overwrite)")

        # Build the next list first (so a write failure below leaves RAM untouched too).
        new_faces = list(self._faces)
        if existing is not None:
            new_faces[existing] = face
        else:
            # Keep the catch-all last: insert before a trailing DEFAULT_FACE_TOKEN face.
            if new_faces and (new_faces[-1].token or new_faces[-1].name) == DEFAULT_FACE_TOKEN:
                new_faces.insert(len(new_faces) - 1, face)
            else:
                new_faces.append(face)

        if self._path is not None:
            self._write(new_faces)
        self._faces = new_faces
        return face

    def _write(self, faces: list[Face]) -> None:
        """Rewrite faces.toml preserving the existing comments/formatting. Reads the
        current document via tomlkit, rebuilds the `face` array-of-tables from `faces`
        (so order/replacements track RAM), and atomically replaces the file."""
        try:
            doc = tomlkit.parse(self._path.read_text())
        except (OSError, tomlkit.exceptions.ParseError):
            doc = _seed_document(faces)
            _atomic_write_text(self._path, tomlkit.dumps(doc))
            return
        aot = tomlkit.aot()
        for f in faces:
            aot.append(_face_table(f))
        doc["face"] = aot
        _atomic_write_text(self._path, tomlkit.dumps(doc))
