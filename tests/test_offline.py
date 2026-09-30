"""Offline enforcement starts during tests, not dependency installation."""
import os
import socket
import urllib.request
from pathlib import Path

import pytest


def test_ci_dead_proxy_is_scoped_to_test_step():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    setup, test = workflow.split("- name: Test (offline, all fakes)", maxsplit=1)
    assert "actions/checkout" in setup
    assert 'pip install -e ".[dev]"' in setup
    assert "HTTP_PROXY" not in setup
    assert "HTTP_PROXY:" in test and "http_proxy:" in test
    assert "run: pytest -q" in test


def test_subprocesses_inherit_dead_proxy_settings():
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        assert os.environ[name] == os.environ[name.lower()] == "http://127.0.0.1:9"
    assert os.environ["NO_PROXY"] == os.environ["no_proxy"] == ""


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_direct_ip_socket_calls_fail_before_io(method):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(AssertionError, match="network access is disabled"):
            getattr(sock, method)(("127.0.0.1", 9))


def test_direct_udp_and_dns_are_blocked():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(AssertionError, match="network access is disabled"):
            sock.sendto(b"unexpected", ("127.0.0.1", 9))
    with pytest.raises(AssertionError, match="network access is disabled"):
        socket.getaddrinfo("example.invalid", 443)


def test_http_cannot_bypass_guard_by_ignoring_proxy():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(AssertionError, match="network access is disabled"):
        opener.open("https://example.invalid", timeout=0.1)


def test_guard_blocks_socket_calls_without_allocating_os_socket():
    # No real local socket is needed by the fake-port suite. Exercise the
    # patched method itself so this proof also runs in restricted sandboxes.
    with pytest.raises(AssertionError, match="network access is disabled"):
        socket.socket.connect(object(), "/tmp/unused-control.sock")
