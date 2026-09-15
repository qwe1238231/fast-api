"""Offline load-test seeding: bulk-create users and mint bearer tokens.

Writes loadtest/tokens.json — a JSON array of access tokens — for k6 to load via
SharedArray. Tokens are minted directly with the app's signing key (no HTTP login,
no per-user argon2, no login rate limit), so seeding thousands is near-instant.

The users must exist in Postgres: get_current_user() resolves the token's `sub`
(the username) to a row on every authenticated request.

Run from the repo root:
    PYTHONPATH=. .venv/bin/python loadtest/seed.py
    N_USERS=5000 PYTHONPATH=. .venv/bin/python loadtest/seed.py
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.security import create_access_token, get_password_hash
from app.db.session import AsyncSessionLocal
from app.models import User

N_USERS = int(os.getenv("N_USERS", "2000"))
USERNAME_PREFIX = "lt_"
INSERT_CHUNK = 5000
OUT_PATH = Path(__file__).parent / "tokens.json"


async def main() -> None:
    # One argon2 hash reused for every seeded user: these are throwaway fixtures,
    # not real credentials, and per-user hashing would burn thousands of CPU-seconds.
    shared_hash = get_password_hash("loadtest-shared-password")
    usernames = [f"{USERNAME_PREFIX}{i}" for i in range(N_USERS)]

    async with AsyncSessionLocal() as db:
        for start in range(0, len(usernames), INSERT_CHUNK):
            batch = usernames[start : start + INSERT_CHUNK]
            # ON CONFLICT DO NOTHING → re-running is idempotent (existing users skipped).
            stmt = (
                pg_insert(User)
                .values([{"username": u, "hashed_password": shared_hash} for u in batch])
                .on_conflict_do_nothing(index_elements=["username"])
            )
            await db.execute(stmt)
        await db.commit()

        # `sub` 必須是 user.id 的字串:get_current_user 只接受數字 sub(2026-08 把 /token
        # 與 /refresh 統一之後),舊的 sub=username token 一律 401。所以先把 id 查回來 ——
        # ON CONFLICT DO NOTHING 之後 RETURNING 只會給新插入的那些,要另外查。
        rows = await db.execute(
            select(User.username, User.id).where(User.username.in_(usernames))
        )
        id_by_username: dict[str, int] = dict(rows.all())

    # tokens[i] 對應 usernames[i](= lt_{i});admission.json 靠這個順序配對,不能亂。
    tokens = [create_access_token(subject=str(id_by_username[u])) for u in usernames]
    OUT_PATH.write_text(json.dumps(tokens))
    print(f"seeded {N_USERS} users; wrote {len(tokens)} tokens -> {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
