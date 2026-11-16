"""Small maintenance CLI.

Usage:
    python -m app.cli hash-password [PASSWORD]

If PASSWORD is omitted, it is read interactively without echo.  The printed
value can be placed into WEB_PASSWORD_HASH so the plaintext password is not
stored in the environment file.
"""

from __future__ import annotations

import getpass
import sys

from app.security import hash_password


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "hash-password":
        print("Usage: python -m app.cli hash-password [PASSWORD]", file=sys.stderr)
        return 2
    password = args[1] if len(args) > 1 else getpass.getpass("Пароль: ")
    if not password:
        print("Пароль не может быть пустым", file=sys.stderr)
        return 2
    print(hash_password(password))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
