"""Authenticated transport rejects redirects before forwarding credentials."""

from __future__ import annotations

import io
import urllib.error
import urllib.request
import urllib.response
from email.message import Message

import pytest

from nanodot.native.http import _RejectRedirects, authenticated_urlopen

ORIGIN = "https://api.example.test/resource"
TOKEN = "test-bearer-secret"


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "destination",
    [
        "https://untrusted.example.test/collect",
        "http://untrusted.example.test/collect",
        "https://api.example.test/elsewhere",
    ],
)
def test_redirect_handler_never_creates_a_forwarded_request(
    code: int, method: str, destination: str
) -> None:
    request = urllib.request.Request(
        ORIGIN,
        headers={"Authorization": f"Bearer {TOKEN}"},
        data=b"private-body" if method == "POST" else None,
        method=method,
    )
    response = io.BytesIO(b"redirect response")
    with pytest.raises(urllib.error.HTTPError) as caught:
        _RejectRedirects().redirect_request(
            request, response, code, "redirect", {"Location": destination}, destination
        )
    assert caught.value.code == code
    assert caught.value.url == ORIGIN
    assert TOKEN not in str(caught.value)
    assert request.full_url == ORIGIN
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    caught.value.close()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_opener_sends_bearer_only_to_initial_origin(
    monkeypatch: pytest.MonkeyPatch, code: int, method: str
) -> None:
    requests_seen = []
    real_build_opener = urllib.request.build_opener

    class FakeHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            requests_seen.append(request)
            headers = Message()
            headers["Location"] = "https://untrusted.example.test/collect"
            response = urllib.response.addinfourl(
                io.BytesIO(b"redirect response"), headers, request.full_url, code
            )
            response.msg = "redirect"
            return response

    def build_fake_opener(*handlers):
        # Exercise the real opener/HTTPErrorProcessor/redirect dispatch while
        # replacing only the network transport with an in-memory response.
        return real_build_opener(*handlers, FakeHTTPSHandler())

    monkeypatch.setattr(urllib.request, "build_opener", build_fake_opener)
    request = urllib.request.Request(
        ORIGIN,
        headers={"Authorization": f"Bearer {TOKEN}"},
        data=b"private-body" if method == "POST" else None,
        method=method,
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        authenticated_urlopen(request, timeout=7.5)
    assert caught.value.code == code
    assert [sent.full_url for sent in requests_seen] == [ORIGIN]
    assert requests_seen[0].get_header("Authorization") == f"Bearer {TOKEN}"
    caught.value.close()


def test_success_preserves_request_timeout_and_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = urllib.request.Request(
        ORIGIN, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    response = io.BytesIO(b"normal response")
    calls = []
    global_opener = urllib.request._opener

    class FakeOpener:
        def open(self, submitted, *, timeout):
            calls.append((submitted, timeout))
            return response

    def build_fake_opener(*handlers):
        assert len(handlers) == 1
        assert isinstance(handlers[0], _RejectRedirects)
        return FakeOpener()

    monkeypatch.setattr(urllib.request, "build_opener", build_fake_opener)
    with authenticated_urlopen(request, timeout=2.25) as opened:
        assert opened is response
        assert opened.read() == b"normal response"
    assert calls == [(request, 2.25)]
    assert response.closed
    assert urllib.request._opener is global_opener


def test_transport_errors_propagate_for_adapter_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = urllib.error.URLError("temporary connection failure")

    class FakeOpener:
        def open(self, request, *, timeout):
            raise error

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())
    with pytest.raises(urllib.error.URLError) as caught:
        authenticated_urlopen(urllib.request.Request(ORIGIN), timeout=3)
    assert caught.value is error
