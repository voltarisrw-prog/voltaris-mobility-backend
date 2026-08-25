"""Create or update a user account with explicit roles.

    python scripts/create_user.py --email a@b.com --name "A B" --roles ADMIN

Why this exists rather than doing it through the API: registration always yields
BUYER, deliberately, and only a SUPER_ADMIN can assign roles. Someone has to
create the first SUPER_ADMIN directly against the database, and once that exists
the console can do the rest.

Passwords are generated here and printed once. They are never accepted as a
command-line argument, because arguments are visible in `ps` to every user on
the machine and are written to shell history. `--password-stdin` is available
for piping from a password manager.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import string
import sys
import uuid
from datetime import UTC, datetime

sys.path.insert(0, ".")

from app.core.config import get_settings
from app.core.security import hash_password
from app.infrastructure.database.client import Collections, connect, disconnect
from app.infrastructure.database.indexes import ensure_indexes
from app.modules.users.models import ROLE_PERMISSIONS, Role, UserStatus

#: Unambiguous alphabet: no O/0, l/I/1. These get read aloud and retyped.
ALPHABET = (
    "".join(c for c in string.ascii_letters + string.digits if c not in "O0lI1") + "!@#$%^&*-_=+"
)


def generate_password(length: int = 20) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def parse_roles(raw: str) -> list[Role]:
    valid = {role.value: role for role in Role}
    chosen: list[Role] = []
    for name in (part.strip().upper() for part in raw.split(",") if part.strip()):
        if name not in valid:
            raise SystemExit(
                f"'{name}' is not a role.\nValid roles: {', '.join(sorted(valid))}\n"
                "Note there is no STAFF role — you probably want SALES_AGENT, "
                "FINANCE, or CONTENT_MANAGER."
            )
        chosen.append(valid[name])
    if not chosen:
        raise SystemExit("at least one role is required")
    return chosen


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--roles", required=True, help="comma separated, e.g. ADMIN,FINANCE")
    parser.add_argument("--phone", default=None)
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of generating one",
    )
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="if the email already exists, replace its roles instead of failing",
    )
    args = parser.parse_args()

    email = args.email.strip().lower()
    roles = parse_roles(args.roles)
    settings = get_settings()

    if args.password_stdin:
        password = sys.stdin.readline().strip()
        if len(password) < settings.password_min_length:
            raise SystemExit(f"password must be at least {settings.password_min_length} characters")
        generated = False
    else:
        password = generate_password()
        generated = True

    db = await connect()
    await ensure_indexes(db)

    existing = await db[Collections.USERS].find_one({"email": email})
    now = datetime.now(UTC)

    if existing is not None:
        if not args.update_existing:
            raise SystemExit(
                f"{email} already exists with roles {existing['roles']}.\n"
                "Pass --update-existing to change them, or use a different address."
            )
        # Roles only. Deliberately does not touch the password: an operator
        # adjusting someone's access should not silently reset their credentials.
        await db[Collections.USERS].update_one(
            {"_id": existing["_id"]},
            {"$set": {"roles": [r.value for r in roles], "updated_at": now}},
        )
        print(f"\n  Updated {email}")
        print(f"  Roles:  {existing['roles']}  ->  {[r.value for r in roles]}")
        print("  Password unchanged.\n")
        await disconnect()
        return

    user_id = uuid.uuid4().hex
    await db[Collections.USERS].insert_one(
        {
            "_id": user_id,
            "name": args.name,
            "email": email,
            "phone": args.phone,
            "password_hash": hash_password(password),
            "roles": [role.value for role in roles],
            # Operator-created accounts skip the verification email — there is no
            # address to confirm that the operator does not already control.
            "status": UserStatus.ACTIVE.value,
            "email_verified": True,
            "phone_verified": False,
            "mfa_enabled": False,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }
    )

    await db[Collections.AUDIT_LOGS].insert_one(
        {
            "_id": uuid.uuid4().hex,
            "actor_id": None,
            "action": "user.created_by_operator",
            "entity_type": "user",
            "entity_id": user_id,
            "before": None,
            "after": {"email": email, "roles": [r.value for r in roles]},
            "request_id": "cli",
            "at": now,
        }
    )

    permissions = sorted({p.value for role in roles for p in ROLE_PERMISSIONS[role]})
    print(f"\n  Created {email}")
    print(f"  Roles:       {', '.join(r.value for r in roles)}")
    print(f"  Permissions: {len(permissions)}")
    if generated:
        print(f"\n  Password:    {password}")
        print("  This is shown once. Store it in a password manager now.")
    print()

    await disconnect()


if __name__ == "__main__":
    asyncio.run(main())
