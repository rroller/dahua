"""Every read in the poll's fan-out has to survive being refused.

`_async_update_data` builds a list of coroutines and awaits them with
`asyncio.gather(*coros)` -- **no `return_exceptions`**. So one read that raises
takes the whole refresh with it: every entity on the channel goes unavailable,
a failure is recorded against the count all of a recorder's channels share, and
the poll backs off. On a first refresh it is `ConfigEntryNotReady`, which is
`setup_retry` and no entities at all.

That is #1006, which I shipped: the picture-adjustment read went into the
fan-out unprobed and unwrapped, and an account without config-write permission
got a 403 that failed setup for the whole device. Two people reported it within
an hour of each other.

A fix per read does not stop the next one. This is the structural version: it
walks the fan-out and insists that each read is either wrapped where it is
called or handled in the client method it calls. A read added without a guard
fails here rather than on somebody's recorder.

What it deliberately does not check: `ClientError` and `TimeoutError` still
propagate, and should. A device that cannot be reached at all *is* a failed
poll, and the layers above it -- the host failure count, the backoff, the
unreachable repair -- exist to say so. What must not fail the poll is a device
that answers and declines one table, which is an HTTP status, so
`ClientResponseError` is the exception this is about.

Pure `ast`, like the entity-name guards, so it runs without Home Assistant.
"""

import ast
import io
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "dahua"

# A handler counts if it would catch a refused read. `Exception` and a bare
# `except` are deliberately accepted: the existing wrappers use the broad form
# on purpose, because a refusal arrives in more shapes than one (an HTTP
# status, an Rpc2MethodRefused, a device returning a body that will not parse).
CATCHES_A_REFUSAL = ("ClientResponseError", "Exception", "ClientError", "bare")


def _tree(name):
    return ast.parse(io.open(PACKAGE / name, encoding="utf-8").read())


def _class(tree, name):
    return next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name
    )


def _methods(cls):
    return {
        n.name: n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _handles_a_refusal(node) -> bool:
    """Does this function body turn a refused read into data?

    Catching is not handling. Every handler in `client._request` matches one of
    the names above and then **logs and re-raises** -- they are there to say
    what happened, not to absorb it. An earlier version of this file counted
    them, recursed into them from every read through `self.get`, and so passed
    while five reads in the fan-out were wide open. It was caught by
    reintroducing each bug and finding the guard silent, not by reading it.

    So a handler counts only if some path out of it returns instead of raising.
    `async_get_config_lighting` is the shape that has to keep counting: it
    returns {} for a 400 and re-raises anything else, which does absorb the
    refusal this is about.
    """
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Try):
            continue
        for handler in inner.handlers:
            kind = ast.unparse(handler.type) if handler.type else "bare"
            if not any(name in kind for name in CATCHES_A_REFUSAL):
                continue
            absorbs = any(
                isinstance(stmt, (ast.Return, ast.Pass)) for stmt in ast.walk(handler)
            )
            if absorbs:
                return True
    return False


def _poll():
    coordinator = _class(_tree("coordinator.py"), "DahuaDataUpdateCoordinator")
    return _methods(coordinator)["_async_update_data"], _methods(coordinator)


def _reads_in_the_fan_out(poll):
    """Every expression handed to `coros.append`, unwrapped of ensure_future."""
    reads = []
    for node in ast.walk(poll):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "append"
            and isinstance(func.value, ast.Name)
            and func.value.id == "coros"
        ):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Call) and "ensure_future" in ast.unparse(arg.func):
            arg = arg.args[0]
        reads.append(arg)
    return reads


def _local_coroutines(poll):
    """Coroutines defined inside the poll itself, like `_ptz_position`."""
    return {
        n.name: n
        for n in ast.walk(poll)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


# The request path itself, which is never a guard for the read above it.
#
# This is the second thing the negative control caught. `_request` really does
# absorb some exceptions -- that is how it falls back from RPC2 to CGI for a
# table RPC2 will not serve -- so "is there an absorbing handler down there?"
# answered yes for every read in the file, through `self.get`, and the guard
# passed with five reads unprotected. Those handlers serve `_request`'s own
# transport choices; what happens to a refusal afterwards is decided by the
# read method, which is where this has to look.
TRANSPORT = frozenset({"get", "_request", "_rpc2_shared_call"})


def _client_handles(name, methods, depth=0) -> bool:
    """The client method, or a config helper it delegates to -- not the transport."""
    node = methods.get(name)
    if node is None or depth > 3:
        return False
    if _handles_a_refusal(node):
        return True
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        called = ast.unparse(inner.func)
        if not called.startswith("self."):
            continue
        tail = called.split(".")[-1]
        if tail in TRANSPORT or tail == name or tail not in methods:
            continue
        if _client_handles(tail, methods, depth + 1):
            return True
    return False


def _verdict(read, poll_methods, local, client_methods):
    """Why this read is safe, or None if it is not."""
    expression = ast.unparse(read)

    # `_ptz_position()` -- defined in the poll, guarded there.
    if isinstance(read, ast.Call) and isinstance(read.func, ast.Name):
        node = local.get(read.func.id)
        if node is not None:
            return "guarded where it is defined" if _handles_a_refusal(node) else None

    if isinstance(read, ast.Call) and isinstance(read.func, ast.Attribute):
        target = read.func
        # self._async_fetch_x() -- a coordinator wrapper.
        if isinstance(target.value, ast.Name) and target.value.id == "self":
            node = poll_methods.get(target.attr)
            if node is not None:
                return (
                    "wrapped in the coordinator" if _handles_a_refusal(node) else None
                )
        # self.client.async_get_x() -- handled in the client, or not.
        if ast.unparse(target.value) == "self.client":
            if _client_handles(target.attr, client_methods):
                return "handled in the client"
            return None

    raise AssertionError(
        "a read was added to the poll in a shape this guard cannot classify: %s.\n"
        "Teach it that shape rather than removing the read from the scan -- an "
        "unclassified read is the one that fails a whole entry." % expression
    )


def test_the_gather_still_has_no_return_exceptions():
    """The premise. If this ever changes, the rest of this file is about a
    danger that no longer exists and should be reconsidered rather than kept.

    It should not change: a read that genuinely cannot reach the device has to
    fail the poll, because that is how the host failure count, the backoff and
    the unreachable repair card are driven.
    """
    poll, _ = _poll()
    gathers = [
        node
        for node in ast.walk(poll)
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("gather")
    ]
    fan_out = [g for g in gathers if any(isinstance(a, ast.Starred) for a in g.args)]

    assert len(fan_out) == 1, "expected one fan-out gather, found %d" % len(fan_out)
    assert not [
        k for k in fan_out[0].keywords if k.arg == "return_exceptions"
    ], "the fan-out now passes return_exceptions; this file's premise has changed"


def test_every_read_in_the_fan_out_survives_a_refusal():
    """The guard. Each read is wrapped, or handled in the client it calls."""
    poll, poll_methods = _poll()
    local = _local_coroutines(poll)
    client_methods = _methods(_class(_tree("client.py"), "DahuaClient"))

    unguarded = sorted(
        ast.unparse(read)
        for read in _reads_in_the_fan_out(poll)
        if _verdict(read, poll_methods, local, client_methods) is None
    )

    assert not unguarded, (
        "these reads are in the poll's fan-out and a refusal from any one of "
        "them fails the whole refresh, taking every entity on the channel with "
        "it:\n  %s\n"
        "Either wrap the call in a coordinator method that catches the refusal "
        "and carries the last answer (see _async_fetch_video_color), or handle "
        "it in the client method -- a getConfig read can simply go through "
        "async_get_config, which returns {} for a refused table. #1006 is what "
        "this looks like in a report." % "\n  ".join(unguarded)
    )


def test_the_scan_actually_found_the_fan_out():
    """A scan that silently found nothing would pass the test above for ever.

    The count is deliberately a floor rather than an exact number, so adding a
    read does not fail this, while a refactor that moves the fan-out somewhere
    this file cannot see does.
    """
    poll, _ = _poll()

    assert len(_reads_in_the_fan_out(poll)) >= 15, (
        "found only %d reads in the poll fan-out, which means the scan has "
        "stopped seeing them rather than that the poll got smaller"
        % len(_reads_in_the_fan_out(poll))
    )
