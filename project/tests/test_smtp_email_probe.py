"""smtp_email_probe.py: the split connect/command timeout and port25_reachable.

Measured on 2026-09-02-gcc-receptionist: 24 of 71 probes were doomed before the
first byte (18 "SMTP unreachable", 6 "no MX") and each paid the full 10 s
because smtplib.SMTP(timeout=10) covers the TCP connect AND every read. Now the
handshake gets SMTP_CONNECT_TIMEOUT (3 s) and the commands keep 10 s.

Everything here is offline: socket calls are monkeypatched.
"""
import socket
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

sep = pytest.importorskip("smtp_email_probe")


class FakeSock:
    def __init__(self):
        self.timeouts: list[float | None] = []
        self.closed = False
        self.connected_to = None

    def settimeout(self, t):
        self.timeouts.append(t)

    def connect(self, addr):
        self.connected_to = addr

    def close(self):
        self.closed = True


def _addrinfo(n_addrs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"10.0.0.{i}", 25)) for i in range(n_addrs)]


def test_get_socket_uses_connect_timeout_then_switches_to_command_timeout(monkeypatch):
    made: list[FakeSock] = []

    def fake_socket(*a, **k):
        s = FakeSock()
        made.append(s)
        return s

    monkeypatch.setattr(sep.socket, "getaddrinfo", lambda *a, **k: _addrinfo(1))
    monkeypatch.setattr(sep.socket, "socket", fake_socket)
    monkeypatch.setattr(sep, "_local_hostname", lambda: "test.local")

    server = sep._SMTP(connect_timeout=3.0, timeout=10.0)
    sock = server._get_socket("mx.example.test", 25, server.timeout)
    assert sock is made[0]
    assert sock.timeouts == [3.0, 10.0], "connect with 3 s, then every command with 10 s"
    assert sock.connected_to == ("10.0.0.0", 25)
    assert server.timeout == 10.0


def test_connect_is_bounded_to_two_addresses(monkeypatch):
    """socket.create_connection walks EVERY resolved address with the full
    timeout each (mx1.hotmail.com: ~15 addresses -> 45 s with timeout=3). The
    probe's connect tries at most two."""
    attempts: list[tuple] = []

    class Refusing(FakeSock):
        def connect(self, addr):
            attempts.append(addr)
            raise socket.timeout("timed out")

    monkeypatch.setattr(sep.socket, "getaddrinfo", lambda *a, **k: _addrinfo(15))
    monkeypatch.setattr(sep.socket, "socket", lambda *a, **k: Refusing())
    with pytest.raises(OSError):
        sep._connect("mx.example.test", 25, 3.0)
    assert len(attempts) == 2


def test_probe_domain_reports_unreachable_without_raising(monkeypatch):
    monkeypatch.setattr(sep.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("nx")))
    monkeypatch.setattr(sep, "_local_hostname", lambda: "test.local")
    codes = sep._probe_domain("mx.nowhere.test", "verify@example.com", ["a@nowhere.test"], 10.0, 3.0)
    assert codes == {"a@nowhere.test": None}


def test_port25_reachable_true_when_any_host_connects(monkeypatch):
    tried: list[str] = []

    def fake_connect(host, port, timeout, max_addrs=2):
        tried.append(host)
        assert port == 25
        if host.startswith("gmail"):
            raise OSError("filtered")
        return FakeSock()

    monkeypatch.setattr(sep, "_connect", fake_connect)
    assert sep.port25_reachable(timeout=1.0) is True
    assert len(tried) == 2


def test_port25_reachable_false_when_every_host_fails(monkeypatch):
    monkeypatch.setattr(sep, "_connect", lambda *a, **k: (_ for _ in ()).throw(socket.timeout("timed out")))
    assert sep.port25_reachable(timeout=1.0) is False


def test_port25_reachable_signature_and_default():
    import inspect
    sig = inspect.signature(sep.port25_reachable)
    assert list(sig.parameters) == ["timeout"]
    assert sig.parameters["timeout"].default == 3.0


def test_mx_cache_is_shared_safely_across_threads(monkeypatch):
    import threading
    calls = []

    class Ans:
        def __init__(self, pref, ex):
            self.preference, self.exchange = pref, ex

    def fake_resolve(domain, rtype):
        calls.append(domain)
        return [Ans(10, "mx1.x.test."), Ans(5, "mx0.x.test.")]

    monkeypatch.setattr(sep.dns.resolver, "resolve", fake_resolve)
    sep._mx_cache.clear()
    out = []
    ts = [threading.Thread(target=lambda: out.append(sep.get_mx_hosts("x.test"))) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert all(o == ["mx0.x.test", "mx1.x.test"] for o in out)
    assert sep._mx_cache["x.test"] == ["mx0.x.test", "mx1.x.test"]
