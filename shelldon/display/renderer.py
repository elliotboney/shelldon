"""The renderer seam (AD-5): the display service draws snapshots through a
`Renderer` interface so the actual panel is injectable.

The real Waveshare V4 E-Ink driver is the production `Renderer`, living
component-locally (`spidev` + the vendored Waveshare module / `omni-epd`) and added
when the hardware is in hand — it is NOT a dependency of this story. Story 1.7
ships the interface + a recording stub so the whole display behaves and is fully
tested on a laptop, dependency-free (same "ship the mechanism + seam, gate the
hardware" discipline as Story 1.5's Linux-gated fork test). Partial-refresh /
layered-sprite techniques and the real expression bitmaps are a Story 3.3 concern.

`render` is async because the real E-Ink draw is a slow (seconds-scale) I/O
operation the event loop must await, not block on (NFR3).
"""

from typing import Protocol, runtime_checkable

from shelldon.contracts import StateSnapshot


@runtime_checkable
class Renderer(Protocol):
    """Draws a single face/state snapshot to a surface. The display service owns
    latest-wins + coalescing; a Renderer just draws what it is handed."""

    async def render(self, snapshot: StateSnapshot) -> None: ...


class StubRenderer:
    """A recording `Renderer` for tests and headless laptop runs: it remembers
    every snapshot it was asked to draw, in order, instead of touching hardware."""

    def __init__(self) -> None:
        self.rendered: list[StateSnapshot] = []

    async def render(self, snapshot: StateSnapshot) -> None:
        self.rendered.append(snapshot)
