"""An open socket on 443 is not an HTTPS server.

When setup fails with `cannot_connect`, the add flow probes a few ports to say
something better than "nothing answered". The first check was an open socket on
443, and its message tells the user the device is serving HTTPS and to tick the
HTTPS box.

#489 is the device that breaks that reasoning: a camera that accepts a
connection on 80 and on 443 and closes it without sending a byte, reported as
`ERR_EMPTY_RESPONSE` in a browser and as `000` by curl. An open-socket check
calls that HTTPS, sends the reporter to tick a box that cannot help, and hides
the more accurate answer sitting one check further down, which is that only
Dahua's own protocol ports are answering and the web service is switched off.

So the probe does the handshake. These tests run against real sockets on
ephemeral ports rather than against a patched probe, because what is being
tested is exactly the difference between accepting a connection and speaking
TLS, and a fake that returns True or False cannot tell them apart.
"""

import asyncio
import datetime
import ssl

import pytest

from custom_components.dahua.host import _async_probe_tcp, _async_probe_tls


def _self_signed(tmp_path):
    """A certificate for 127.0.0.1, written to disk for ssl to load.

    Self signed on purpose: it is what these cameras ship, and the probe does
    not verify it.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


async def _serve(handler, ssl_context=None):
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ssl_context)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _accept_and_close(_reader, writer):
    """What #489's camera does: the connection is taken and dropped."""
    writer.close()


def _accept_and_hold(held):
    """A socket left open and silent, which is what a plain TCP service looks
    like to anything expecting a protocol it does not speak.

    The handler returns immediately and the connection stays open, rather than
    sleeping: a handler still running at the end of the test is a lingering
    task, which Home Assistant's harness fails on, and rightly.
    """

    async def handler(_reader, writer):
        held.append(writer)

    return handler


async def _close_all(server, held=()):
    for writer in held:
        writer.close()
    server.close()
    await server.wait_closed()


# --- what the handshake can tell apart ---------------------------------------


async def test_a_tls_server_answers_the_tls_probe(tmp_path, socket_enabled):
    cert_file, key_file = _self_signed(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    held = []
    server, port = await _serve(_accept_and_hold(held), ssl_context=context)

    try:
        assert await _async_probe_tls("127.0.0.1", port, 5.0) is True
    finally:
        await _close_all(server, held)


async def test_a_socket_that_accepts_and_closes_is_not_https(socket_enabled):
    """The #489 shape, and the whole point of this change. The old check
    returned True here and the add flow told the user to tick HTTPS."""
    server, port = await _serve(_accept_and_close)

    try:
        assert await _async_probe_tls("127.0.0.1", port, 5.0) is False
        assert (
            await _async_probe_tcp("127.0.0.1", port, 5.0) is True
        ), "the socket really is open, which is why the old check was fooled"
    finally:
        await _close_all(server)


async def test_a_plain_service_holding_the_socket_open_is_not_https(socket_enabled):
    """A port that is open and silent, which is what Dahua's own 37777 looks
    like to anything that does not speak its protocol."""
    held = []
    server, port = await _serve(_accept_and_hold(held))

    try:
        assert await _async_probe_tls("127.0.0.1", port, 1.0) is False
    finally:
        await _close_all(server, held)


async def test_nothing_listening_is_not_https(socket_enabled):
    server, port = await _serve(_accept_and_close)
    await _close_all(server)

    assert await _async_probe_tls("127.0.0.1", port, 1.0) is False


# --- and what the add flow then says -----------------------------------------


def _probe(open_ports, tls_ports=()):
    async def tcp(_address, port, timeout=None):
        return port in open_ports

    async def tls(_address, port, timeout=None):
        return port in tls_ports

    return tcp, tls


async def test_a_camera_that_only_accepts_on_443_is_not_sent_to_https(monkeypatch):
    """#489 again, through the flow. 443 accepts and says nothing, Dahua's own
    port answers, and the honest message is the one about the web service."""
    from custom_components.dahua import config_flow

    tcp, tls = _probe({443, 37777}, tls_ports=())
    monkeypatch.setattr(config_flow, "_async_probe_tcp", tcp)
    monkeypatch.setattr(config_flow, "_async_probe_tls", tls)

    assert (
        await config_flow.async_refine_connection_failure("1.2.3.4", "cannot_connect")
        == "http_service_off"
    )


async def test_a_real_https_device_still_gets_the_https_message(monkeypatch):
    from custom_components.dahua import config_flow

    tcp, tls = _probe({443, 37777}, tls_ports={443})
    monkeypatch.setattr(config_flow, "_async_probe_tcp", tcp)
    monkeypatch.setattr(config_flow, "_async_probe_tls", tls)

    assert (
        await config_flow.async_refine_connection_failure("1.2.3.4", "cannot_connect")
        == "https_available"
    )


async def test_a_socket_on_443_and_nothing_else_stays_cannot_connect(monkeypatch):
    """Nothing is claimed that cannot be backed up: an open socket that will
    not speak TLS, with no Dahua port answering, is not evidence of anything
    the user can act on."""
    from custom_components.dahua import config_flow

    tcp, tls = _probe({443}, tls_ports=())
    monkeypatch.setattr(config_flow, "_async_probe_tcp", tcp)
    monkeypatch.setattr(config_flow, "_async_probe_tls", tls)

    assert (
        await config_flow.async_refine_connection_failure("1.2.3.4", "cannot_connect")
        == "cannot_connect"
    )
