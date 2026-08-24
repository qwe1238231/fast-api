"""Mint per-user, single-use admission tokens for Test A2 (the real admission path).

Pairs with loadtest/tokens.json BY INDEX: admission[i] is bound to the same user
(lt_i) as bearer tokens[i]. verify_admission checks the JWT's `sub` == the caller's
DB user id (not the username) and the event_id, and enforces single-use — so each
request in the run must consume a distinct token. Writes loadtest/admission.json,
aligned to tokens.json (both ordered lt_0 .. lt_{N-1}).

Run from the repo root (after seed.py):
    N_USERS=2000 EVENT_ID=1 PYTHONPATH=. .venv/bin/python loadtest/seed_admission.py
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import select

from app.core.security import create_admission_token
from app.db.session import AsyncSessionLocal
from app.models import User

N_USERS = int(os.getenv("N_USERS", "2000"))
EVENT_ID = int(os.getenv("EVENT_ID", "1"))
TTL_SECONDS = int(os.getenv("TTL_SECONDS", "600"))   # headroom over the run window
OUT_PATH = Path(__file__).parent / "admission.json"


async def main() -> None:
    usernames = [f"lt_{i}" for i in range(N_USERS)]
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(select(User.username, User.id).where(User.username.in_(usernames)))
        ).all()

    id_by_name = {name: uid for name, uid in rows}
    missing = [u for u in usernames if u not in id_by_name]
    if missing:
        raise SystemExit(f"{len(missing)} seeded users missing — run seed.py first (e.g. {missing[:3]})")

    # sub = the user's DB id (str), matching verify_admission's check against current_user.id.
    tokens = [
        create_admission_token(user_id=id_by_name[u], event_id=EVENT_ID, ttl_seconds=TTL_SECONDS)
        for u in usernames
    ]
    OUT_PATH.write_text(json.dumps(tokens))
    print(f"minted {len(tokens)} admission tokens for event {EVENT_ID} (ttl={TTL_SECONDS}s) -> {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
