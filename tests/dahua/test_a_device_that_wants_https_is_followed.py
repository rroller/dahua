"""A device that says where its HTTPS is gets asked there, not described.

#947. With HTTPS switched on, a Dahua box answers port 80 with a redirect to
itself:

    HTTP/1.1 302 Moved Temporarily
    Location: https://192.168.178.54:443/cgi-bin/magicBox.cgi?action=getMachineName

Until now the add form recognised that and printed "Tick the HTTPS option and
use port 443". The device had already said both of those things, and the
integration already acts on them by itself for an entry that exists: the
`http_dead_https_available` repair moves the entry to 443 on one confirm press.
Only the add flow made the user do it by hand.

Two facts make following the redirect the right move rather than a guess:

* **The credentials have not been offered yet.** aiohttp drops the
  Authorization header when a redirect changes origin, and a change of scheme
  and port is a change of origin (`aiohttp/client.py`, beside "Invalid redirect
  URL origin"). So the 401 at the end of that chain is the device answering an
  unauthenticated request, which is why it says nothing about the password, and
  the HTTPS attempt is the first login the device is given. That is the same
  reasoning `async_explain_http_service_off` uses to justify its one DHIP
  attempt, and it matters on hardware that locks an account out.
* **The port is the device's to name.** 443 is what Dahua sends and what the
  probe path assumes, but the Location header is read rather than assumed, so a
  recorder behind a port mapping is followed to where it actually answers.

What succeeded has to be stored, not just used: an entry created on the port
that does not answer comes up broken and raises the repair card this change
exists to avoid. That is what TRANSPORT_FIX carries, and the derive check at
the bottom is what keeps a future call site from dropping it.

Mutations, each applied to the real source:

| reintroduced | caught by |
| --- | --- |
| the retry removed | `is_asked_over_https`, `handed_back_to_be_stored`, `named_by_the_device`, `only_listens_on_443`, `a_wrong_password_over_https` |
| the already-HTTPS guard removed | `an_identical_attempt_is_not_made_twice` |
| the redirect's port ignored, 443 assumed | `the_port_named_by_the_device` |
| the fix not applied in the user step | `the_user_step_stores_what_answered`, `every_caller_stores_the_transport` |
| the HTTPS failure reported as the reason that sent us there | `a_wrong_password_over_https` |
| the retry refining a second time | `the_retry_does_not_probe_again` |
| the marker read rather than popped | **nothing** |

That last row is in the table because it failed to be caught, not because it
was. Nothing merges the whole `data` dict into what gets stored, so `pop` and
`get` cannot be told apart from the outside today. The `pop` stays as the
narrower of the two, and this says plainly that no test defends it.

Those runs were of these assertions against `config_flow.py` with Home
Assistant stubbed, since the module cannot be imported without it. The flow
level test is marked where it needs a real `hass`, and ran first on CI.
"""

import ast

import pytest
from aiohttp import ClientConnectorError, ClientResponseError
from yarl import URL

from custom_components.dahua import config_flow
from custom_components.dahua.config_flow import (
    DEFAULT_HTTPS_PORT,
    DahuaFlowHandler,
    TRANSPORT_FIX,
    https_redirect_port,
)

from .integration_source import modules

ADDRESS = "192.168.178.54"
SERIAL = "4L07AD6PAG5F0F2"
NAME = "NVR-D516"


class _Redirect:
    """One hop of aiohttp's `history`, which holds responses rather than URLs."""

    status = 302


def _redirected(port="443"):
    """The 401 that ends a redirect chain, as `raise_for_status` builds it."""
    info = type(
        "RequestInfo",
        (),
        {
            "url": URL(
                "https://%s:%s/cgi-bin/magicBox.cgi?action=getMachineName"
                % (ADDRESS, port)
            )
        },
    )()
    return ClientResponseError(info, (_Redirect(),), status=401, message="x")


def _refused_login():
    """A plain 401, with no redirect behind it: the credentials are wrong."""
    return ClientResponseError(None, (), status=401, message="x")


def _nothing_answered():
    return ClientConnectorError(
        type("Key", (), {"ssl": None, "host": ADDRESS, "port": 80, "is_ssl": False})(),
        OSError("refused"),
    )


class _Box:
    """A device that answers on one transport and does something else on others.

    Records every client that was built, which is how "asked once more" is told
    apart from "asked three times".
    """

    def __init__(self, answers_on=("443", True), otherwise=None, on_https=None):
        self.answers_on = answers_on
        self.otherwise = otherwise or _redirected()
        # What the device does once the retry reaches it, when that differs: a
        # box that redirects on 80 and refuses the login on 443.
        self.on_https = on_https
        self.asked = []

    def client(self, username, password, address, port, rtsp_port, session, use_https):
        box = self
        on_https = bool(use_https) or str(port) == DEFAULT_HTTPS_PORT
        box.asked.append((str(port), on_https))

        class _Client:
            identity_derived_from_credentials = False

            def _answer(self):
                if (str(port), on_https) == box.answers_on:
                    return None
                if on_https and box.on_https is not None:
                    raise box.on_https
                raise box.otherwise

            async def get_machine_name(self):
                self._answer()
                return {"name": NAME}

            async def async_get_system_info(self, strict_auth=False):
                self._answer()
                return {"serialNumber": SERIAL}

        return _Client()


@pytest.fixture
def no_probes(monkeypatch):
    """Nothing in these tests may touch the network.

    `async_refine_connection_failure` opens TCP sockets and the DHIP explainer
    offers the credentials, so both are replaced. The counter is what proves the
    retry does not pay for the refinement a second time.
    """
    calls = []

    async def refine(address, reason):
        calls.append(reason)
        return reason

    monkeypatch.setattr(config_flow, "async_refine_connection_failure", refine)

    async def explain(address, username, password, reason):
        # `(reason, data)` since #949: data is a monitor the flow can add, and
        # None for everything this file is about.
        return reason, None

    monkeypatch.setattr(config_flow, "async_explain_http_service_off", explain)
    return calls


def _flow(box, monkeypatch):
    monkeypatch.setattr(config_flow, "DahuaClient", box.client)
    return object.__new__(DahuaFlowHandler)


async def _ask(box, monkeypatch, port="80", use_https=None):
    return await _flow(box, monkeypatch)._test_credentials(
        "admin", "pw", ADDRESS, port, "554", 0, use_https
    )


# --- the reported case -------------------------------------------------------


def test_the_port_is_read_from_the_redirect_rather_than_assumed():
    assert https_redirect_port(_redirected("443")) == "443"
    assert https_redirect_port(_redirected("8443")) == "8443"
    assert https_redirect_port(_refused_login()) is None


async def test_a_device_that_redirects_is_asked_over_https(no_probes, monkeypatch):
    box = _Box()

    data, error = await _ask(box, monkeypatch)

    assert error is None
    assert data["name"] == NAME
    assert box.asked == [("80", False), ("443", True)], (
        "once where the user said, once where the device said. A third entry "
        "means the retry is looping"
    )


async def test_the_transport_that_answered_is_handed_back_to_be_stored(
    no_probes, monkeypatch
):
    """Not just used. An entry created on port 80 comes up broken, which is the
    `http_dead_https_available` repair this change exists to avoid."""
    data, _ = await _ask(_Box(), monkeypatch)

    assert data[TRANSPORT_FIX] == {"port": "443", "use_https": True}


async def test_the_port_named_by_the_device_is_the_one_used(no_probes, monkeypatch):
    """A recorder behind a port mapping redirects somewhere else, and 443 would
    be the wrong guess for it."""
    box = _Box(answers_on=("8443", True), otherwise=_redirected("8443"))

    data, error = await _ask(box, monkeypatch)

    assert error is None
    assert data[TRANSPORT_FIX] == {"port": "8443", "use_https": True}
    assert box.asked == [("80", False), ("8443", True)]


async def test_a_device_that_only_listens_on_443_is_tried_there(no_probes, monkeypatch):
    """The other way in: nothing answered at all, and the probe found 443 open.
    There is no redirect to read a port from, so 443 is the fallback."""
    box = _Box(otherwise=_nothing_answered())

    async def refine(address, reason):
        return "https_available"

    monkeypatch.setattr(config_flow, "async_refine_connection_failure", refine)

    data, error = await _ask(box, monkeypatch)

    assert error is None
    assert data[TRANSPORT_FIX] == {"port": "443", "use_https": True}


# --- what must still be believed ---------------------------------------------


async def test_a_wrong_password_over_https_is_reported_as_a_wrong_password(
    no_probes, monkeypatch
):
    """The retry's own answer, not the reason that sent us there. A 401 with no
    redirect behind it is the device refusing the login, and "tick HTTPS and use
    443" is not advice once that has been done."""
    box = _Box(
        answers_on=("nothing", False),
        otherwise=_redirected(),
        on_https=_refused_login(),
    )

    data, error = await _ask(box, monkeypatch)

    assert data is None
    assert error == "auth", "the redirect was hiding a password the device refuses"
    assert box.asked == [("80", False), ("443", True)]


async def test_an_attempt_that_was_already_https_is_not_repeated(
    no_probes, monkeypatch
):
    """Asking the same transport twice would double what a device with a lockout
    counter sees, for no new information."""
    box = _Box(answers_on=("nothing", False), otherwise=_refused_login())

    data, error = await _ask(box, monkeypatch, port="443")

    assert data is None
    assert error == "auth"
    assert box.asked == [("443", True)]


async def test_an_identical_attempt_is_not_made_twice(monkeypatch):
    """The case the guard is actually for. An entry already on 443 over HTTPS
    that cannot connect still makes `https_available` true, because the probe
    only asks whether 443 is listening. Acting on it would re-send the same
    request to the same place, and on hardware that counts failed logins that
    is a second one for no new information."""
    box = _Box(answers_on=("nothing", False), otherwise=_nothing_answered())

    async def refine(address, reason):
        return "https_available"

    monkeypatch.setattr(config_flow, "async_refine_connection_failure", refine)

    async def explain(address, username, password, reason):
        # `(reason, data)` since #949: data is a monitor the flow can add, and
        # None for everything this file is about.
        return reason, None

    monkeypatch.setattr(config_flow, "async_explain_http_service_off", explain)

    data, error = await _ask(box, monkeypatch, port="443")

    assert data is None
    assert error == "https_available"
    assert box.asked == [("443", True)]


async def test_https_on_the_default_port_is_also_not_repeated(no_probes, monkeypatch):
    """The flag rather than the port: an entry can be HTTPS on 8443."""
    box = _Box(answers_on=("nothing", False), otherwise=_refused_login())

    data, error = await _ask(box, monkeypatch, port="8443", use_https=True)

    assert data is None
    assert box.asked == [("8443", True)]


async def test_the_retry_does_not_probe_again(no_probes, monkeypatch):
    """`async_refine_connection_failure` opens sockets and the explainer offers
    the credentials over DHIP. Its docstring argues that is safe because it
    happens once, so the retry must not make it twice."""
    box = _Box(answers_on=("nothing", False), otherwise=_redirected())

    await _ask(box, monkeypatch)

    assert len(no_probes) == 1, "refined once, for the attempt that could act on it"


# --- what the step stores ----------------------------------------------------


def _user_step(data):
    """The add step, with everything but the field handling stubbed out.

    No `hass`: the three things the step does with one are replaced, so what is
    left is the part this is about. `init_info` is what
    `async_step_name` creates the entry from, so it is where the answer has to
    have landed.
    """
    flow = object.__new__(DahuaFlowHandler)
    flow._discovered = {}
    flow._errors = {}

    async def credentials(*args, **kwargs):
        return dict(data), None

    async def discover(user_input=None):
        return "discover"

    async def set_unique_id(unique_id):
        return None

    flow._test_credentials = credentials
    flow.async_step_discover = discover
    flow.async_set_unique_id = set_unique_id
    flow._async_heal_siblings = lambda unique_id, address: None
    flow._abort_if_unique_id_configured = lambda **kwargs: None
    return flow


TYPED = {
    "username": "admin",
    "password": "pw",
    "address": ADDRESS,
    "port": "80",
    "rtsp_port": "554",
    "channel": 0,
}

ANSWERED = {
    "name": NAME,
    "serialNumber": SERIAL,
    TRANSPORT_FIX: {"port": "443", "use_https": True},
}


async def test_the_user_step_stores_what_answered():
    """An entry created on the port that does not answer comes up broken and
    raises the repair card, which is the whole point of following the redirect
    during setup rather than after it."""
    flow = _user_step(ANSWERED)

    await flow.async_step_user(dict(TYPED))

    assert flow.init_info["port"] == "443"
    assert flow.init_info["use_https"] is True


async def test_a_device_that_needed_no_correction_is_stored_as_typed():
    flow = _user_step({"name": NAME, "serialNumber": SERIAL})

    await flow.async_step_user(dict(TYPED))

    assert flow.init_info["port"] == "80"
    assert "use_https" not in flow.init_info


# --- every caller stores what answered ---------------------------------------


def _functions_calling(name):
    """Every function in the integration that calls `name`, bar its own body.

    Over the whole package rather than one file, because which module holds a
    flow step is not what this is about, and a step that moves must not take
    itself out of the check on the way.
    """
    found = {}
    for path, text in modules().items():
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == name:
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and getattr(inner.func, "attr", None) == name
                ):
                    found["%s.%s" % (path.name, node.name)] = node
    return found


def test_every_caller_stores_the_transport_that_answered():
    """Derived per function rather than counted, because a count passes when one
    caller applies the fix twice and another not at all.

    A step that validates and then stores the port the user typed creates an
    entry that cannot come up, which is exactly the repair card this change
    removes the need for.
    """
    callers = _functions_calling("_test_credentials")

    assert len(callers) >= 4, "the scan found %s callers, so it proves little" % len(
        callers
    )
    missing = sorted(
        name
        for name, node in callers.items()
        if TRANSPORT_FIX
        not in {
            inner.value
            for inner in ast.walk(node)
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str)
        }
        and not any(
            getattr(inner, "id", None) == "TRANSPORT_FIX" for inner in ast.walk(node)
        )
    )
    assert not missing, "validates the transport then stores another: %s" % missing
