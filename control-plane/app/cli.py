"""Administrative commands.

Usage:
    python -m app.cli seed-system-account
    python -m app.cli bootstrap-account --slug acme --email ops@acme.test
    python -m app.cli issue-system-key --name fleet-operator
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
from app.services.service_principals import create_service_principal


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


async def _issue_system_key(*, name: str) -> None:
    """Issue an API key on the Fabric system account, printing it once.

    Managed stamps are Fabric's own capacity serving many tenants, so enrolling one is
    refused under a customer account: a tenant must not own the fleet. That rule leaves
    Fabric's operators needing a credential of their own, and this is where it comes from.

    The key deliberately does not carry ``system:capacity:write``. Granting an account
    access to managed capacity happens through ``grant-managed-capacity``, which needs
    database credentials, so no long-lived key can widen entitlements on its own.
    """
    factory = get_session_factory()
    async with factory() as session:
        with system_context():
            account = await ensure_system_account(session)
            # A service principal, not a user: this is a machine credential for Fabric's
            # own fleet operations and there is no person behind it.
            principal = await create_service_principal(
                session,
                account_id=account.id,
                name=name,
                actor_type="system",
                actor_user_id=None,
            )
            await session.flush()
            _record, secret = await create_api_key(
                session,
                account_id=account.id,
                name=name,
                requested_scopes=sorted(scope_defs.CONTROL_SCOPES),
                principal_scopes=frozenset(scope_defs.CONTROL_SCOPES),
                expires_in_days=None,
                service_principal_id=principal.id,
                actor_user_id=None,
            )
            await session.commit()
        print(f"account   {account.id}  slug={account.slug}")
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

    system_key = commands.add_parser("issue-system-key")
    system_key.add_argument("--name", default="fleet-operator")

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
    elif args.command == "issue-system-key":
        asyncio.run(_issue_system_key(name=args.name))
    elif args.command == "grant-managed-capacity":
        asyncio.run(_grant_managed_capacity(args.account))


if __name__ == "__main__":
    main()
