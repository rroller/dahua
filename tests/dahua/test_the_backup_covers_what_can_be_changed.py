"""A backup that misses a table is worse than no backup.

`backup_config` reads back every configuration table this integration can
change, so somebody can see what a setting was before something changed it.
That is worth having because a Dahua config write is frequently not reversible
from here: `VideoAnalyseRule` enables over CGI and will not disable, and
several writes are accepted and then ignored.

The risk is the obvious one. `BACKED_UP_TABLES` is a list in `const.py` of what
another file does, and the moment `client.py` grows a write path for a table
nobody adds here, the backup quietly stops covering it. Nothing fails, the
service still returns something that looks complete, and the gap is found by
somebody who needed the value and has not got it.

So the list is checked against the code rather than maintained by hand: every
`action=setConfig&<Table>` in `client.py` has to appear in it.

Pure `ast`, like the other guards here, so it runs without Home Assistant.
"""

import ast
import io
import pathlib
import re

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "dahua"

# A setConfig URL names its table in two shapes, and the first version of
# this file only knew one of them:
#
#   "...action=setConfig&VideoColor[0][0].Brightness=50"   -> VideoColor
#   "...&Lighting[{channel}][{profile}].Mode={mode}"       -> Lighting
#
# The second is how a URL built up in pieces reads, and matching only the
# first missed `Lighting`, `LightingScheme` and `LeLensMask` -- one of which
# nothing else had noticed was missing from the backup at all. Zero hits for a
# spelling is a claim about the code, not a result.
_WRITES = re.compile(
    r"action=setConfig&([A-Za-z_][A-Za-z0-9_]*)|&([A-Z][A-Za-z0-9_]*)\["
)


def _backed_up() -> tuple:
    """BACKED_UP_TABLES, read out of const.py without importing it."""
    tree = ast.parse(io.open(PACKAGE / "const.py", encoding="utf-8").read())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "BACKED_UP_TABLES"
            for target in node.targets
        ):
            continue
        return tuple(
            element.value
            for element in node.value.elts
            if isinstance(element, ast.Constant)
        )
    raise AssertionError("const.py no longer defines BACKED_UP_TABLES")


def _tables_written() -> set:
    """Every config table name that appears in a setConfig URL in client.py."""
    source = io.open(PACKAGE / "client.py", encoding="utf-8").read()
    # One group per shape, so each match has an empty half.
    return {name for pair in _WRITES.findall(source) for name in pair if name}


def test_every_table_the_integration_writes_is_backed_up():
    """The one that matters. A write path with no backup entry means a setting
    this integration can change and cannot report."""
    missing = sorted(_tables_written() - set(_backed_up()))

    assert not missing, (
        "client.py writes these config tables and backup_config does not read "
        "them back: %s.\nAdd them to BACKED_UP_TABLES in const.py. A backup "
        "that silently misses a table is worse than no backup, because the "
        "person relying on it does not find out until they need the value."
        % ", ".join(missing)
    )


def test_the_scan_really_found_the_writes():
    """A regex that matched nothing would make the test above pass for ever.

    A floor rather than an exact number, so adding a write does not fail this
    while a change of URL shape does.
    """
    found = _tables_written()

    assert len(found) >= 15, (
        "found only %d setConfig tables in client.py, which means the scan has "
        "stopped seeing them rather than that the integration writes less: %s"
        % (len(found), sorted(found))
    )


def test_nothing_is_backed_up_that_is_never_written():
    """The other direction, which is a weaker claim and still worth making.

    A table here that nothing writes is either a read-only table that does not
    belong in a *backup* -- the get_config service reads any table at all, and
    diagnostics reports the rest -- or a write path that has been removed. Both
    are worth a look.

    This had an exception list for `LightingScheme` while the scan knew only
    one of the two URL shapes. Widening the scan removed the need for it, which
    is the right way round: an exception list is a place for a real gap to hide.
    """
    extra = sorted(set(_backed_up()) - _tables_written())

    assert not extra, "BACKED_UP_TABLES names tables nothing writes: %s" % ", ".join(
        extra
    )


def test_the_list_is_sorted_and_unique():
    """So a merge conflict in it is a conflict about content, not about order,
    and so a table cannot be read twice on every call."""
    tables = _backed_up()

    assert list(tables) == sorted(tables), "BACKED_UP_TABLES is not in order"
    assert len(set(tables)) == len(tables), "BACKED_UP_TABLES repeats a table"
