"""Reading `Authorization: Bearer <credential>`, and telling a Clerk token from a code or a key."""


def bearer_token(authorization: str | None) -> str | None:
    """The token of `Bearer <token>` (the scheme is case-insensitive), else None."""
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        return None
    return token
