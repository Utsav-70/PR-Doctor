"""Tools 3 and 4 — get_symbol and find_references, over Tree-sitter.

Both answer questions a diff cannot. `process_payment(order)` changed: is that safe?
The answer is in the definition and in whoever calls it, and neither is in the patch.

The index is built lazily and cached by `(path, mtime)`. Parsing a whole repository up
front costs seconds a review does not have, and an agent only ever asks about a handful
of files.

Tree-sitter is error-tolerant by design: a file with a syntax error still yields a
usable tree for the parts that parsed. Returning that with `partial_parse=True` beats
failing the call — a broken file is often exactly the one under review.
"""

from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from agent.tools.results import Reference, Symbol, SymbolKind, ToolResult
from agent.tools.workspace import PathNotAllowed, Workspace

logger = logging.getLogger(__name__)

SYMBOL_TOOL = "get_symbol"
REFERENCE_TOOL = "find_references"

# Registry keyed by the `language` value Phase 3 assigns. Adding JS/TS later is a
# registration here, not a refactor of either tool.
_LANGUAGES: dict[str, Language] = {"python": Language(tree_sitter_python.language())}
_EXTENSION_LANGUAGE = {".py": "python", ".pyi": "python"}

MAX_BODY_LINES = 200

# (path, mtime_ns) -> parsed tree. Bounded so a huge repository cannot grow it without
# limit inside one review.
_CACHE: dict[tuple[str, int], tuple[Node, bool]] = {}
_CACHE_LIMIT = 256


def _language_for(path: Path) -> str | None:
    return _EXTENSION_LANGUAGE.get(path.suffix.lower())


def _parse(path: Path) -> tuple[Node, bool] | None:
    """Parsed root node plus whether the parse hit errors. Cached by path and mtime."""
    language = _language_for(path)
    if language is None or language not in _LANGUAGES:
        return None
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if key in _CACHE:
        return _CACHE[key]

    try:
        source = path.read_bytes()
    except OSError:
        return None

    parser = Parser(_LANGUAGES[language])
    tree = parser.parse(source)
    entry = (tree.root_node, tree.root_node.has_error)

    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = entry
    return entry


def _text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", "replace")


def _name_of(node: Node) -> str | None:
    child = node.child_by_field_name("name")
    return _text(child) if child is not None else None


def _signature(node: Node) -> str:
    """First line of a definition — enough to answer most questions without the body."""
    name = _name_of(node) or ""
    params = node.child_by_field_name("parameters")
    returns = node.child_by_field_name("return_type")
    if node.type == "class_definition":
        supers = node.child_by_field_name("superclasses")
        return f"class {name}{_text(supers) if supers else ''}"
    rendered = f"def {name}{_text(params) if params else '()'}"
    if returns is not None:
        rendered += f" -> {_text(returns)}"
    return rendered


def _docstring(node: Node) -> str | None:
    body = node.child_by_field_name("body")
    if body is None or not body.children:
        return None
    first = body.children[0]
    if first.type != "expression_statement" or not first.children:
        return None
    literal = first.children[0]
    if literal.type != "string":
        return None
    return _text(literal).strip("\"'").strip() or None


def _walk_definitions(root: Node, prefix: str = "") -> list[tuple[Node, str, SymbolKind]]:
    """Definitions with qualified names, so `PaymentService.capture` is distinguishable
    from a module-level `capture`."""
    found: list[tuple[Node, str, SymbolKind]] = []
    for child in root.children:
        if child.type == "class_definition":
            name = _name_of(child)
            if name:
                qualified = f"{prefix}{name}"
                found.append((child, qualified, "class"))
                body = child.child_by_field_name("body")
                if body is not None:
                    found.extend(_walk_definitions(body, prefix=f"{qualified}."))
        elif child.type == "function_definition":
            name = _name_of(child)
            if name:
                kind: SymbolKind = "method" if prefix else "function"
                found.append((child, f"{prefix}{name}", kind))
        elif child.type in ("import_statement", "import_from_statement"):
            found.append((child, _text(child), "import"))
        elif child.type in ("decorated_definition", "block", "if_statement"):
            found.extend(_walk_definitions(child, prefix=prefix))
    return found


def get_symbol(
    workspace: Workspace,
    name: str,
    *,
    kind: str | None = None,
    path_glob: str = "**/*.py",
    include_body: bool = False,
    max_results: int = 20,
) -> ToolResult:
    """Find where a function, class, method, or import is *defined*."""
    if not name.strip():
        return ToolResult.failure(SYMBOL_TOOL, "empty symbol name")

    symbols: list[Symbol] = []
    total = 0
    for file in sorted(workspace.root.glob(path_glob)):
        if not file.is_file():
            continue
        try:
            workspace.resolve(workspace.relative(file))
        except (PathNotAllowed, ValueError):
            continue
        parsed = _parse(file)
        if parsed is None:
            continue
        root, had_error = parsed

        for node, qualified, node_kind in _walk_definitions(root):
            simple = qualified.rsplit(".", 1)[-1]
            if node_kind == "import":
                if name not in qualified:
                    continue
            elif simple != name and qualified != name:
                continue
            if kind and node_kind != kind:
                continue

            total += 1
            if len(symbols) >= max_results:
                continue
            symbols.append(
                Symbol(
                    path=workspace.relative(file),
                    name=simple,
                    qualified_name=qualified,
                    kind=node_kind,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    signature=_signature(node) if node_kind != "import" else qualified,
                    docstring=_docstring(node) if node_kind != "import" else None,
                    body=_body(node) if include_body else None,
                    partial_parse=had_error,
                )
            )

    return ToolResult(
        tool=SYMBOL_TOOL,
        data={"symbols": symbols, "query": name},
        truncated=total > len(symbols),
        total=total,
        note=None if symbols else f"no definition of {name!r} found",
    )


def _body(node: Node) -> str:
    text = _text(node)
    lines = text.split("\n")
    if len(lines) <= MAX_BODY_LINES:
        return text
    return "\n".join([*lines[:MAX_BODY_LINES], f"... {len(lines) - MAX_BODY_LINES} more lines"])


# Node types that constitute a genuine use of an identifier, as opposed to a mention
# inside a comment or a string.
_REFERENCE_KINDS: dict[str, SymbolKind] = {
    "call": "call",
    "attribute": "attribute",
    "assignment": "assignment",
    "import_statement": "import",
    "import_from_statement": "import",
}


def _enclosing(node: Node) -> str | None:
    """Nearest enclosing function or class — "used where", not just "used"."""
    current = node.parent
    while current is not None:
        if current.type in ("function_definition", "class_definition"):
            return _name_of(current)
        current = current.parent
    return None


def _collect_references(root: Node, name: str) -> list[tuple[Node, SymbolKind]]:
    found: list[tuple[Node, SymbolKind]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        # Comments and strings are skipped wholesale: that is the entire point of
        # doing this with an AST rather than grep.
        if node.type in ("comment", "string"):
            continue
        if node.type == "identifier" and _text(node) == name:
            parent = node.parent
            kind: SymbolKind = "call"
            matched = False
            while parent is not None:
                if parent.type in _REFERENCE_KINDS:
                    kind = _REFERENCE_KINDS[parent.type]
                    matched = True
                    break
                if parent.type in ("function_definition", "class_definition", "module"):
                    break
                parent = parent.parent
            if matched:
                found.append((node, kind))
        stack.extend(node.children)
    return found


async def find_references(
    workspace: Workspace,
    name: str,
    *,
    path_glob: str = "**/*.py",
    max_results: int = 50,
) -> ToolResult:
    """Where is this symbol *used*?

    Two-pass by design: ripgrep narrows to candidate files cheaply, then Tree-sitter
    filters those to genuine references. Parsing every file in the repository to answer
    one question would be slower by orders of magnitude.

    Explicitly not a resolver — no type inference, no dynamic dispatch, no cross-module
    aliasing. Every result carries `confidence: "heuristic"` so Phase 7 weights it
    accordingly. Claiming precision it does not have is worse than admitting the limit.
    """
    if not name.strip():
        return ToolResult.failure(REFERENCE_TOOL, "empty symbol name")

    from agent.tools.search import search_code

    # Pass 1 — cheap narrowing. Word-boundary so `pay` does not match `payment`.
    hits = await search_code(workspace, rf"\b{name}\b", path_glob=path_glob, max_results=500)
    if not hits.ok:
        return ToolResult.failure(REFERENCE_TOOL, f"search pass failed: {hits.error}")

    candidates = sorted({m.path for m in hits.data["matches"]})

    references: list[Reference] = []
    total = 0
    for relative in candidates:
        try:
            file = workspace.resolve(relative)
        except PathNotAllowed:
            continue
        parsed = _parse(file)
        if parsed is None:
            continue
        root, _ = parsed

        for node, kind in sorted(_collect_references(root, name), key=lambda n: n[0].start_point):
            total += 1
            if len(references) >= max_results:
                continue
            line_no = node.start_point[0] + 1
            references.append(
                Reference(
                    path=relative,
                    line=line_no,
                    kind=kind,
                    enclosing=_enclosing(node),
                    text=_line_text(file, line_no),
                )
            )

    return ToolResult(
        tool=REFERENCE_TOOL,
        data={"references": references, "query": name},
        truncated=total > len(references),
        total=total,
        note="heuristic: no type inference or dynamic dispatch",
    )


def _line_text(path: Path, line_no: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return ""
    return lines[line_no - 1].strip() if 0 < line_no <= len(lines) else ""
