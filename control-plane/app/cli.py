"""Administrative commands.

Usage:
    python -m app.cli seed-system-account
    python -m app.cli bootstrap-account --slug acme --email ops@acme.test
    python -m app.cli grant-managed-capacity --account <uuid>

``bootstrap-account`` exists because every other route to an account goes through
Auth0: a human logs in, exchanges that token, and creates an account. That is the right
flow for people, but it makes the very first account impossible to create before an
identity provider is configured, and it makes automated setup depend on a browser.

This is deliberately a command rather than an endpoint. An endpoint that created
accounts without authentication would be a way for anyone to create tenants; a command
requires whoever runs it to already hold the database credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select

from app.core import scopes as scope_defs
from app.core.database import dispose_engine, get_session_factory
from app.core.tenancy import system_context
from app.models import Account, User
from app.services.accounts import create_account, ensure_system_account
from app.services.api_keys import create_api_key
from app.services.entitlements import set_managed_capacity


async def _seed_system_account() -> None:
    factory = get_session_factory()
    async with factory() as session:
        with system_context():
            account = await ensure_system_account(session)
            await session.commit()
        print(f"system account ready id={account.id} slug={account.slug}")
    await dispose_engine()


async def _bootstrap_account(
    *, slug: str, name: str | None, email: str, key_name: str, managed: bool
) -> None:
    """Create an account, an owner, and one API key, printing the key once."""
    factory = get_session_factory()
    async with factory() as session:
        with system_context():
            await ensure_system_account(session)

            existing = (
                await session.execute(select(Account).where(Account.slug == slug))
            ).scalar_one_or_none()
            if existing is not None:
                # Refusing rather than reusing: a second run should not silently mint
                # another credential for an account someone is already using.
                print(f"account {slug!r} already exists id={existing.id}; refusing to modify it")
                await dispose_engine()
                return

            # A local owner record, not an Auth0 identity. The subject is namespaced so it
            # cannot collide with a real Auth0 subject later.
            owner = User(auth0_subject=f"bootstrap|{slug}", email=email)
            session.add(owner)
            await session.flush()

            account, _membership = await create_account(
                session, slug=slug, name=name or slug, owner=owner
            )

            scopes = sorted(scope_defs.CONTROL_SCOPES | {scope_defs.INFERENCE_INVOKE})
            _record, secret = await create_api_key(
                session,
                account_id=account.id,
                name=key_name,
                requested_scopes=scopes,
                # The bootstrap principal holds every control scope by construction, so
                # delegation cannot exceed it. It does not hold system scopes, so this key
                # cannot grant capacity to itself.
                principal_scopes=frozenset(scope_defs.CONTROL_SCOPES),
                expires_in_days=None,
                service_principal_id=None,
                actor_user_id=owner.id,
            )

            if managed:
                await set_managed_capacity(
                    session,
                    account_id=account.id,
                    enabled=True,
                    actor_id="cli:bootstrap-account",
                    reason="granted during bootstrap",
                )

            await session.commit()

        print(f"account   {account.id}  slug={account.slug}")
        print(f"owner     {owner.id}  email={owner.email}")
        print(f"managed_capacity={'enabled' if managed else 'disabled'}")
        # Shown once and never stored in recoverable form: only a hash is kept.
        print(f"api_key   {secret}")
    await dispose_engine()


async def _grant_managed_capacity(account_id: uuid.UUID) -> None:
    factory = get_session_factory()
    async with factory() as session:
        with system_context():
            entitlement = await set_managed_capacity(
                session,
                account_id=account_id,
                enabled=True,
                actor_id="cli:grant-managed-capacity",
                reason="granted from the command line",
            )
            await session.commit()
        print(
            f"account {entitlement.account_id} managed_capacity="
            f"{entitlement.managed_capacity_enabled} changed={entitlement.changed}"
        )
    await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("seed-system-account")

    bootstrap = commands.add_parser("bootstrap-account")
    bootstrap.add_argument("--slug", required=True)
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--name")
    bootstrap.add_argument("--key-name", default="bootstrap")
    bootstrap.add_argument(
        "--managed-capacity",
        action="store_true",
        help="also entitle the account to Fabric's managed GPU capacity",
    )

    grant = commands.add_parser("grant-managed-capacity")
    grant.add_argument("--account", required=True, type=uuid.UUID)

    args = parser.parse_args()

    if args.command == "seed-system-account":
        asyncio.run(_seed_system_account())
    elif args.command == "bootstrap-account":
        asyncio.run(
            _bootstrap_account(
                slug=args.slug,
                name=args.name,
                email=args.email,
                key_name=args.key_name,
                managed=args.managed_capacity,
            )
        )
    elif args.command == "grant-managed-capacity":
        asyncio.run(_grant_managed_capacity(args.account))


if __name__ == "__main__":
    main()
