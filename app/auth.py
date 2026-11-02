"""Who is calling. Firebase signs people in; this checks the pass it gives them.

After a Google sign-in, Firebase gives the browser an ID token: a short-lived pass
signed by Google that names the user. The browser sends it with each request, and
this module checks the signature, so a made-up token can't name someone else.
"""
from __future__ import annotations

import os
from typing import Callable, Protocol


class NotSignedIn(Exception):
    """There is no token, or it isn't a valid one."""


class Tokens(Protocol):
    def user_of(self, token: str) -> str:
        """Return the user's ID, or raise NotSignedIn."""
        ...


def bearer_token(header: str) -> str:
    """Read the token from an `Authorization: Bearer <token>` header."""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise NotSignedIn()
    return token.strip()


class FirebaseTokens:
    """Checks tokens with Firebase's own library, which fetches Google's public
    keys and checks the signature, project, issuer and expiry.

    Any other failure, such as Google's keys being unreachable, is left to raise.
    The caller then refuses the request, because a token that can't be checked
    must not count as a good one.
    """

    def __init__(self, verify: Callable[[str], dict] | None = None):
        self._verify = verify or self._verify_with_firebase
        self._app = None

    def user_of(self, token: str) -> str:
        from firebase_admin import auth

        try:
            claims = self._verify(token)
        except (ValueError, auth.InvalidIdTokenError) as problem:
            raise NotSignedIn() from problem
        user = claims.get("uid")
        if not user:
            raise NotSignedIn()
        return user

    def _verify_with_firebase(self, token: str) -> dict:
        import firebase_admin
        from firebase_admin import auth

        if self._app is None:
            project = os.environ.get("GOOGLE_CLOUD_PROJECT")
            self._app = firebase_admin.initialize_app(options={"projectId": project}, name="coach")
        return auth.verify_id_token(token, app=self._app)
