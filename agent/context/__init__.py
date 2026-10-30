"""Context assembly.

Two builders, and the distinction matters:

- `build_context` (render.py) renders whole file contents. Cheap, no tools, no
  checkout. This is what the pipeline used before Phase 4.
- `build_tool_context` (builder.py) asks the code-intelligence tools for exactly what
  the diff implies it needs. More expensive per review, far more precise.

Both return the same `ContextBundle`, so swapping one for the other is a one-line
change at the call site.
"""

from agent.context.builder import BuildReport, build_tool_context
from agent.context.render import (
    ContextBundle,
    build_context,
    neutralise,
    render_numbered,
)

__all__ = [
    "BuildReport",
    "ContextBundle",
    "build_context",
    "build_tool_context",
    "neutralise",
    "render_numbered",
]
