"""Find the integration's source by what it says, not by which file holds it.

Several tests here read the source rather than drive the code, because driving it
needs the whole of Home Assistant running. Each of them opened
`custom_components/dahua/__init__.py` by name, which quietly made them a coupling
to *where* the code lived rather than to what it does.

Splitting `__init__.py` into host.py and coordinator.py proved the point: three of
those tests turned into `StopIteration` from a bare `next()`, one started
reporting that issues the code still raises were missing, and one carried on
passing for no better reason than that what it looked for had not moved. None of
that is what any of them is about.

So the search is over the package. A definition that moves between modules is
still found; a definition that is deleted or renamed still fails, which is the
only failure these tests want.
"""

import ast
import io
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"

DEFINITION_KINDS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def modules() -> dict:
    """path -> source text, for every module in the integration."""
    return {
        path: io.open(path, encoding="utf-8").read()
        for path in sorted(PACKAGE.glob("*.py"))
    }


def source() -> str:
    """Every module's text at once, for a search that is about a string.

    Concatenated rather than read one file at a time because the callers asking
    for this are asking "does the code anywhere do X", and naming the files it
    may be in is the coupling this module exists to remove.
    """
    return "\n".join(modules().values())


def definition(name: str, kinds=DEFINITION_KINDS):
    """The ast node for a top level or nested definition, from whichever module.

    Raises rather than returning None, and names every module it looked in: a
    `next()` with no default gave `StopIteration` and no indication of what had
    gone missing, which cost more to diagnose than the move itself.
    """
    looked = []
    for path, text in modules().items():
        looked.append(path.name)
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, kinds) and node.name == name:
                return node
    raise AssertionError(
        "no definition named %r in the integration. Looked in: %s"
        % (name, ", ".join(looked))
    )
