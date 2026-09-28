"""Producer authentication (spec §5, §9).

A bearer token maps to exactly one configured source name. The source is
never taken from the request body.
"""

from __future__ import annotations

import hmac


class ProducerAuth:
    def __init__(self, producer_keys: dict[str, str]) -> None:
        # Stored as bytes for hmac.compare_digest.
        self._keys = [(source, token.encode("utf-8")) for source, token in producer_keys.items()]

    def authenticate(self, authorization_header: str | None) -> str | None:
        """Return the source name for a valid ``Bearer <token>`` header, else None.

        Every configured token is compared (no early exit) with a constant-time
        comparison, so response timing does not reveal which token matched.
        """
        if not authorization_header:
            return None
        scheme, _, credential = authorization_header.partition(" ")
        credential = credential.strip()
        if scheme.lower() != "bearer" or not credential:
            return None
        try:
            presented = credential.encode("ascii")
        except UnicodeEncodeError:
            return None

        matched: str | None = None
        for source, token in self._keys:
            if hmac.compare_digest(presented, token) and matched is None:
                matched = source
        return matched
