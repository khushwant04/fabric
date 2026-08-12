"""Administrative commands.

Usage:
    python -m app.cli seed-system-account
"""

from __future__ import annotations

import argparse
import asyncio

from app.core.database import dispose_engine, get_session_factory
from app.services.accounts import ensure_system_account


async def _seed_system_account() -> None:
    factory = get_session_factory()
    async with factory() as session:
        account = await ensure_system_account(session)
        await session.commit()
        print(f"system account ready id={account.id} slug={account.slug}")
    await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    parser.add_argument("command", choices=("seed-system-account",))
    args = parser.parse_args()
    if args.command == "seed-system-account":
        asyncio.run(_seed_system_account())


if __name__ == "__main__":
    main()
