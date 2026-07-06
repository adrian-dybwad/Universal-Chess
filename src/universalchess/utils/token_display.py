"""Display helpers for secret tokens.

Kept as a leaf utility (no menus/services imports) so both the menu layer and the
services layer can mask a token for display without creating an import cycle
between them.
"""


def mask_token(token: str) -> str:
    """Mask a secret token for display, revealing only a short prefix/suffix.

    Never returns the full token. Short tokens are reduced to a fixed mask so the
    length of a small secret is not leaked either.
    """
    if not token:
        return "Not set"
    if len(token) <= 8:
        return token[:2] + "..." + token[-2:] if len(token) > 4 else "****"
    return token[:6] + "..." + token[-4:]
