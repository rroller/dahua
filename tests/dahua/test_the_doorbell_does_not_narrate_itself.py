"""The VTO's protocol tracing belongs at debug, and its identifiers nowhere above it.

`vto.py` logged eleven messages at **info**, which every doorbell owner sees in their log
at Home Assistant's default level. They are not events, they are a running commentary on
the protocol: "Get version", "Get device type", "Get serial number", "Attach event
manager", and then each answer.

Two reasons that is worth a test rather than just a fix.

**The serial number was among them.** `diagnostics.py` deliberately redacts
`serial_number`, so the integration was removing it from a file the user chooses to share
while printing it into a log they may share without thinking. Whichever policy is right,
holding both at once is not.

**Log level is invisible in review.** Nothing fails when a `debug` is written as an
`info`, the change reads as harmless, and the cost lands on every user of that device
rather than on whoever wrote it. So it is asserted here instead.

This is a source scan, like the icon and name checks. It is deliberately narrow: it
forbids `info` in this one module, not anywhere else. `light.py` keeps seven, and should,
because each one reports a decision the integration took on the user's behalf about their
lighting override, which is exactly what someone reading a log at default level needs.

If a genuinely user-facing message is ever needed here, `warning` is almost certainly the
right level for it, and if it really is `info` then this test is the place to say why.
"""

import ast
import pathlib
import re

MODULE = (pathlib.Path(__file__).resolve().parents[2]
          / "custom_components" / "dahua" / "vto.py")
SOURCE = MODULE.read_text(encoding="utf-8")

# Things that identify a device or its owner's network.
IDENTIFIERS = ("serial", "Serial", "SerialNumber", "machine_name")


def _logging_calls():
    """Every _LOGGER.<level>(...) call in the module, with its level."""
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "_LOGGER"):
            yield func.attr, node


def test_the_protocol_trace_is_at_debug():
    """Eleven of these were at info and fired on every doorbell connection."""
    levels = [level for level, _node in _logging_calls()]

    assert levels, "no logging calls found, so this test is checking nothing"
    assert "info" not in levels, (
        "vto.py logs at info again; the protocol trace belongs at debug because it "
        "fires on every connection and reaches every doorbell owner's log")


def test_no_identifier_is_logged_above_debug():
    """diagnostics.py redacts the serial number. Logging it at info undoes that for
    anyone who shares a log instead of a diagnostics download."""
    offenders = []
    for level, node in _logging_calls():
        if level == "debug":
            continue
        rendered = ast.unparse(node)
        if any(word in rendered for word in IDENTIFIERS):
            offenders.append((level, rendered[:70]))

    assert not offenders, "identifiers logged above debug: %s" % offenders


def test_the_debug_calls_do_not_build_strings_they_may_not_need():
    """An f-string is formatted whether or not the level is enabled, so a debug call
    on a hot path pays for its message even with debug off. The event data line runs
    once per frame and interpolates the raw payload, which is the worst case for it.

    `%` placeholders are handed to the logger and only rendered if something will
    emit them.
    """
    eager = []
    for level, node in _logging_calls():
        if level != "debug" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.JoinedStr):
            eager.append(ast.unparse(node)[:70])

    assert not eager, (
        "debug calls that build their message eagerly: %s" % eager)


def test_the_scan_would_notice_an_info_call():
    """The guard against the three tests above passing because the scan finds nothing.

    A regex would have been fooled by `# _LOGGER.info(` in a comment; this walks the
    AST, so it is worth proving it sees a real call when there is one.
    """
    found = list(_logging_calls())

    assert len(found) > 5, "expected several logging calls in vto.py, found %d" % len(found)
    assert {"debug", "error"} <= {level for level, _node in found}, (
        "expected both debug and error calls; the scan may be missing some")


def test_every_logging_call_is_found_by_the_scan():
    """Cross-check the AST walk against a plain text count, so a call written in a shape
    the walk does not recognise cannot hide from all of this."""
    textual = len(re.findall(r"_LOGGER\.\w+\(", SOURCE))

    assert len(list(_logging_calls())) == textual, (
        "the AST scan found %d calls, the text shows %d"
        % (len(list(_logging_calls())), textual))
