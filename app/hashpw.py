"""Print a DASHBOARD_PASSWORD_HASH for .env:  python -m app.hashpw  (prompts for the password)."""
from __future__ import annotations

import getpass
import sys

from .auth import hash_password


def main() -> None:
    password = sys.argv[1] if len(sys.argv) > 1 else getpass.getpass("Dashboard password: ")
    if len(password) < 10:
        sys.exit("Use at least 10 characters.")
    print(hash_password(password))


if __name__ == "__main__":
    main()
