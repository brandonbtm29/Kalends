"""0.16.1: email through a mail server on this computer (Proton Mail Bridge) and clearer errors."""

from __future__ import annotations

import base64
import ipaddress
import smtplib
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from kalends.notify import Note, Notifier, _safe_error, on_this_computer, tls_context

# --- which hosts count as this computer --------------------------------------------------------


LOCAL = ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST.", "x.localhost"]


@pytest.mark.parametrize("host", LOCAL)
def test_this_computer(host):
    assert on_this_computer(host)


@pytest.mark.parametrize(
    "host", ["smtp.gmail.com", "10.0.0.5", "192.168.1.2", "100.64.0.1", "localhost.example.com", "", "::"]
)
def test_not_this_computer(host):
    assert not on_this_computer(host)


def test_certificates_checked_except_on_this_computer():
    strict = tls_context("smtp.example.com")
    assert strict.verify_mode == ssl.CERT_REQUIRED and strict.check_hostname
    local = tls_context("127.0.0.1")
    assert local.verify_mode == ssl.CERT_NONE and not local.check_hostname


# --- readable errors -----------------------------------------------------------------------------


def test_not_tls_error_suggests_starttls():
    exc = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1082)")
    assert "STARTTLS" in _safe_error(exc)


def test_untrusted_certificate_error_says_so():
    exc = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate")
    assert "certificate isn't trusted" in _safe_error(exc)


# --- a Bridge-like server: STARTTLS on 127.0.0.1 with a self-signed certificate -----------------


def _self_signed(tmp: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    san = x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(san, False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return cert_path, key_path


class BridgeLike:
    """Just enough SMTP: EHLO, STARTTLS, AUTH PLAIN, MAIL, RCPT, DATA, QUIT."""

    def __init__(self, tmp: Path):
        cert, key = _self_signed(tmp)
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(cert, key)
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(10)
        self.port = self.sock.getsockname()[1]
        self.got: list[dict] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                self._session(conn)
            except (OSError, ssl.SSLError):
                pass
            finally:
                conn.close()

    def _session(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        seen: dict = {"tls": False, "user": None, "data": b""}
        self.got.append(seen)
        f = conn.makefile("rb")
        conn.sendall(b"220 127.0.0.1 ESMTP Service Ready\r\n")
        while line := f.readline():
            cmd = line.strip().decode("ascii", "replace")
            verb = cmd.split(" ", 1)[0].upper()
            if verb == "EHLO":
                tls = [] if seen["tls"] else [b"250-STARTTLS"]
                lines = [b"250-127.0.0.1", *tls, b"250-AUTH PLAIN LOGIN", b"250 8BITMIME"]
                conn.sendall(b"\r\n".join(lines) + b"\r\n")
            elif verb == "STARTTLS":
                conn.sendall(b"220 Ready to start TLS\r\n")
                conn = self.tls.wrap_socket(conn, server_side=True)
                f = conn.makefile("rb")
                seen["tls"] = True
            elif verb == "AUTH":
                creds = base64.b64decode(cmd.split(" ")[2]).split(b"\0")
                seen["user"] = creds[1].decode()
                conn.sendall(b"235 Authenticated\r\n")
            elif verb in ("MAIL", "RCPT"):
                conn.sendall(b"250 OK\r\n")
            elif verb == "DATA":
                conn.sendall(b"354 Go ahead\r\n")
                while (chunk := f.readline()) not in (b".\r\n", b""):
                    seen["data"] += chunk
                conn.sendall(b"250 Queued\r\n")
            elif verb == "QUIT":
                conn.sendall(b"221 Bye\r\n")
                return
            else:
                conn.sendall(b"502 Not implemented\r\n")

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def bridge(tmp_path):
    b = BridgeLike(tmp_path)
    yield b
    b.close()


def _notifier(cfg, port: int, security: str, host: str = "127.0.0.1") -> Notifier:
    c = cfg.model_copy(deep=True)
    e = c.notify.email
    e.enabled, e.smtp_host, e.smtp_port, e.security = True, host, port, security
    e.username, e.password = "me@proton.example", "bridge-pw"
    return Notifier(lambda: c)


def test_email_through_bridge_with_starttls(cfg, bridge):
    n = _notifier(cfg, bridge.port, "starttls")
    res = n.send("email", Note(title="Kalends test", body="It works", kind="summary"))
    assert res.ok, res.detail
    s = bridge.got[-1]
    assert s["tls"] and s["user"] == "me@proton.example"
    assert b"Subject: Kalends test" in s["data"] and b"It works" in s["data"]


def test_ssl_on_a_starttls_port_says_choose_starttls(cfg, bridge):
    n = _notifier(cfg, bridge.port, "ssl")
    res = n.send("email", Note(title="Kalends test", body="x", kind="summary"))
    assert not res.ok and "bridge-pw" not in res.detail
    # OpenSSL words this differently between versions; the hint covers the ones it uses for "not TLS"
    assert "STARTTLS" in res.detail or "Couldn't reach it" in res.detail


def test_other_servers_certificates_are_still_checked(bridge):
    """The same self-signed server, reached as if it were elsewhere, is refused."""
    srv = smtplib.SMTP("127.0.0.1", bridge.port, timeout=10)
    try:
        srv.ehlo()
        with pytest.raises(ssl.SSLCertVerificationError) as err:
            srv.starttls(context=tls_context("mail.example.com"))
        assert "certificate isn't trusted" in _safe_error(err.value)
    finally:
        srv.close()
