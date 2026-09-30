"""Credential-bearing HTTP requests must never follow server redirects.

urllib's default redirect handler can copy Authorization to another host.
Use a local opener so API credentials and request bodies reach only the
explicitly configured destination, without changing process-global policy.
"""

from __future__ import annotations

import urllib.error
import urllib.request


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Reject same-origin redirects too: a server-supplied destination is
        # not an additional endpoint the user configured for these secrets.
        raise urllib.error.HTTPError(
            request.full_url,
            code,
            "redirects are disabled for authenticated requests",
            headers,
            response,
        )


def authenticated_urlopen(request: urllib.request.Request, timeout: float):
    """Open once; redirects raise HTTPError for the adapter's error mapping.

    Return the standard urllib response, including context-manager support.
    This intentionally ignores any installed global opener and does not
    modify the request, environment, or global urllib redirect behavior.
    """
    opener = urllib.request.build_opener(_RejectRedirects())
    return opener.open(request, timeout=timeout)
