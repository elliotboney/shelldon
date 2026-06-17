"""display — long-lived E-Ink region compositor (the pet face)."""

from shelldon.display.renderer import Renderer, StubRenderer
from shelldon.display.service import run_display

__all__ = ["Renderer", "StubRenderer", "run_display"]
