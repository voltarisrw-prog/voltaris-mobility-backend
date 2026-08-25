"""Set a password on an existing account.

    python scripts/reset_password.py --email buyer@voltaris.rw
    python scripts/reset_password.py --all-test-accounts --write-file

Use when a generated password was lost, or to set known credentials on the six
test accounts so each role's experience can be checked.

    --write-file  saves them to credentials.local.txt, which .gitignore already
                  covers. Never commit it, and delete it when you are done.

The two real accounts — the ADMIN and the SUPER_ADMIN — are excluded from
--all-test-accounts on purpose. They hold genuine privilege over live data, and
a password chosen for convenience is the wrong trade there.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, ".")

from app.core.config import get_settings
from app.core.security import hash_password
from app.infrastructure.database.client import Collections, connect, disconnect

sys.path.insert(0, "scripts")
from create_user import generate_password

#: The role-check accounts. Real people are absent by design — see the docstring.
TEST_ACCOUNTS = [
    "buyer@voltaris.rw",
    "seller@voltaris.rw",
    "dealer@voltaris.rw",
    "sales@voltaris.rw",
    "finance@voltaris.rw",
    "content@voltaris.rw",
]

CREDENTIALS_FILE = Path("credentials.local.txt")


def _write_credentials(lines: list[str]) -> None:
    CREDENTIALS_FILE.write_text(
        "# Voltaris test accounts. NOT for real users.\n"
        "# Delete this file when you are done. It is gitignored, not secret.\n"
        f"# Written {datetime.now(UTC).isoformat()}\n\n" + "\n".join(lines) + "\n"
    )
    # 0600: readable only by the account that created it.
    CREDENTIALS_FILE.chmod(0o600)


async def set_password(db, email: str, password: str) -> bool:
    result = await db[Collections.USERS].update_one(
        {"email": email.lower()},
        {
            "$set": {
                "password_hash": hash_password(password),
                "updated_at": datetime.now(UTC),
            }
        },
    )
    if result.matched_count:
        # Existing sessions survive a password change unless revoked. Someone who
        # already has a token keeps it, which is not what "reset the password"
        # means to anyone.
        await db[Collections.SESSIONS].update_many(
            {"user_id": {"$exists": True}, "revoked_at": None},
            {"$set": {"revoked_at": datetime.now(UTC), "revoked_reason": "password_reset"}},
        )
    return bool(result.matched_count)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email")
    parser.add_argument("--all-test-accounts", action="store_true")
    parser.add_argument("--write-file", action="store_true", help="save to credentials.local.txt")
    parser.add_argument(
        "--password",
        action="store_true",
        help="prompt for one password to apply, instead of generating",
    )
    args = parser.parse_args()

    if not args.email and not args.all_test_accounts:
        raise SystemExit("pass --email ADDRESS or --all-test-accounts")

    settings = get_settings()
    chosen: str | None = None
    if args.password:
        chosen = getpass.getpass("Password: ")
        if len(chosen) < settings.password_min_length:
            raise SystemExit(f"must be at least {settings.password_min_length} characters")

    db = await connect()
    targets = TEST_ACCOUNTS if args.all_test_accounts else [args.email]
    lines: list[str] = []

    print()
    for email in targets:
        password = chosen or generate_password()
        if await set_password(db, email, password):
            roles = (await db[Collections.USERS].find_one({"email": email.lower()}))["roles"]
            print(f"  {email:32s} {','.join(roles):16s} {password}")
            lines.append(f"{email}\t{','.join(roles)}\t{password}")
        else:
            print(f"  {email:32s} NOT FOUND — run scripts/create_team.sh first")

    if args.write_file and lines:
        # Off the event loop: blocking file I/O inside an async function stalls
        # every other task, and ruff's ASYNC240 flags it for exactly that reason.
        await asyncio.to_thread(_write_credentials, lines)
        print(f"\n  Written to {CREDENTIALS_FILE} (mode 600). Delete it when finished.")

    print("\n  All existing sessions were revoked; everyone must sign in again.\n")
    await disconnect()


if __name__ == "__main__":
    asyncio.run(main())
