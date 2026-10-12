"""A repeating request failure has to say whether asking again could ever work.

`_request` logged `ClientError fetching information from <url>` and nothing else. On
#832 that line appears every poll for two different hosts, on
`configManager.cgi?action=getConfig&name=MotionDetect`, and the question it raises is
the one it cannot answer:

* a **400** or **404** means the device does not serve this and never will, so asking
  once per poll for ever is waste and the answer should be remembered;
* a **500** or a refused connection means the device is having a bad day, and asking
  again is exactly right.

Those are opposite conclusions from an identical log line, so the reporter's debug log
could not be acted on and the next step was another round trip to ask them for the
status. The status was in the exception the whole time.

Only the status, deliberately. A connector error's `str()` runs to a paragraph naming
the host, the port and sometimes a certificate chain, and the URL is already on the
line, so anything without a status is named by its class instead.
"""

import socket

import aiohttp
import pytest

from custom_components.dahua.client import _describe_client_error

from .integration_source import modules


def _response_error(status, message=""):
    """An aiohttp.ClientResponseError the way aiohttp raises one."""
    return aiohttp.ClientResponseError(
        request_info=None, history=(), status=status, message=message
    )


# --- the two answers that mean opposite things ------------------------------


def test_a_refusal_names_its_status():
    """400 is the one that means stop asking."""
    assert (
        _describe_client_error(_response_error(400, "Bad Request"))
        == "HTTP 400 Bad Request"
    )


def test_a_server_error_names_its_status_too():
    """500 is the one that means try again, and it is what #832's event stream gets."""
    assert _describe_client_error(_response_error(500, "Internal Server Error")) == (
        "HTTP 500 Internal Server Error"
    )


@pytest.mark.parametrize("status", [400, 401, 404, 500, 501, 503])
def test_every_status_survives_into_the_text(status):
    """The number is the part that decides what to do, so it is the part a reporter
    has to be able to read back."""
    assert str(status) in _describe_client_error(_response_error(status))


def test_a_status_with_no_message_is_still_readable():
    """aiohttp leaves `message` empty for some responses, and "HTTP 404 " with a
    trailing space reads like something went missing."""
    assert _describe_client_error(_response_error(404)) == "HTTP 404"


# --- and the errors that carry no status ------------------------------------


def test_a_name_resolution_failure_is_named_by_its_kind():
    assert _describe_client_error(socket.gaierror(-2, "Name not known")) == "gaierror"


def test_a_connection_error_does_not_paste_its_paragraph():
    """The reason this is not `str(exception)`. A connector error repeats the host and
    port that are already in the URL on the same line, and can carry a certificate
    chain with it."""

    class _Long(aiohttp.ClientError):
        def __str__(self):
            return (
                "Cannot connect to host 192.168.1.218:8086 ssl:default "
                "[Connect call failed ('192.168.1.218', 8086)]"
            )

    described = _describe_client_error(_Long())

    assert described == "_Long"
    assert "Connect call failed" not in described
    assert "192.168.1.218" not in described


def test_a_timeout_is_not_confused_for_a_status():
    """It has no `status`, and reporting one it does not have would be worse than
    saying nothing."""
    assert _describe_client_error(TimeoutError()) == "TimeoutError"


# --- and the line actually uses it ------------------------------------------


def test_the_request_failure_line_passes_the_description():
    """Without this, reverting the call site leaves every test above passing while
    the log line goes back to saying nothing. Searched across the package rather than
    in a named module, for the reason `integration_source` exists."""
    source = "".join(modules().values())

    assert (
        "_describe_client_error(exception)" in source
    ), "the ClientError debug line no longer reports what the device answered"
    assert "ClientError fetching information from %s: %s" in source


# --- and so does the line that reports a device failing to come up -----------


def test_the_initialize_failure_line_describes_the_exception():
    """#1055 arrived as a line with nothing after the colon:

        Failed to initialize device at x.x.x.x:

    which is what `"%s" % exception` produces for a bare TimeoutError, whose
    str() is empty. That is the failure the line reports most often, so it said
    nothing exactly when it was needed, and the reporter could not tell us what
    the exception had been because the line had already discarded it.

    Derived from the call rather than from a copy of the message: every
    _LOGGER call in the package whose format begins this way has to hand its
    exception through the describer, wherever that call lives.
    """
    import ast

    offenders = []
    for path, text in modules().items():
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "warning"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            if not str(node.args[0].value).startswith("Failed to initialize device"):
                continue
            described = [
                argument
                for argument in node.args[1:]
                if isinstance(argument, ast.Call)
                and getattr(argument.func, "id", "") == "_describe_client_error"
            ]
            if not described:
                offenders.append("%s:%d" % (path.name, node.lineno))

    assert not offenders, (
        "these report a failed device by interpolating the exception, which is "
        "blank for a bare TimeoutError: %s" % offenders
    )


def test_the_scan_above_finds_the_lines_it_is_policing():
    """Otherwise a renamed message makes the guard pass by matching nothing."""
    import ast

    found = 0
    for _path, text in modules().items():
        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "warning"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).startswith("Failed to initialize device")
            ):
                found += 1

    assert found >= 2, "expected the two initialize-failure lines, found %d" % found


def test_a_bare_timeout_is_what_made_the_line_blank():
    """The premise of the two tests above, stated rather than assumed."""
    assert str(TimeoutError()) == ""
    assert _describe_client_error(TimeoutError()) == "TimeoutError"
