"""
telegram_auth_service.py

Handles Telegram *user-account* authentication via Telethon (MTProto),
and persists the resulting session strings ("tokens") in PostgreSQL.

Flow:
    1. send_code(phone_number)
       -> triggers Telegram to send a login code via SMS / the app
    2. verify_code(phone_number, code, phone_code_hash, password=None)
       -> completes login, saves the session string to Postgres

Note: Telegram's user-account API has no redirect-based OAuth2 flow
(there is no "authorization URL" to send a browser to). The closest
functional equivalent is this two-step phone-number + code exchange,
which is why the two routes in telegram_auth.py are named "start" and
"verify" rather than "authorize" and "callback".

Required env vars (in a .env file, loaded via python-dotenv):
    TELEGRAM_API_ID    - from https://my.telegram.org
    TELEGRAM_API_HASH  - from https://my.telegram.org
    DATABASE_URL       - postgresql://user:pass@host:port/dbname

Requires: telethon, asyncpg, python-dotenv
"""

import os
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
)
from dotenv import load_dotenv

load_dotenv()

API_ID_RAW = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
DATABASE_URL = os.getenv("DATABASE_URL")

if not API_ID_RAW or not API_HASH:
    raise ValueError("Missing TELEGRAM_API_ID or TELEGRAM_API_HASH in your .env")
if not DATABASE_URL:
    raise ValueError("Missing DATABASE_URL in your .env")

API_ID = int(API_ID_RAW)

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS telegram_sessions (
    id                SERIAL PRIMARY KEY,
    phone_number      TEXT UNIQUE NOT NULL,
    telegram_user_id  BIGINT,
    session_string    TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# In-memory registry of TelegramClients that are mid-login (between
# send_code and verify_code). Keyed by phone number so the second call
# can reuse the same live connection.
#
# NOTE: this only works if send_code and verify_code hit the same
# process. For a multi-worker deployment, either pin a user to one
# worker (sticky sessions) or persist enough state to reconnect a
# fresh client for step 2 instead of reusing this one. Do NOT try to
# serialize the intermediate (pre-login) session string and rehydrate
# a new TelegramClient from it in verify_code -- Telethon's login
# handshake is stateful on the live connection object itself, not just
# the session's crypto keys, so a fresh client with the same string
# session will not be able to complete sign_in. Keep the same client
# object alive across both calls, as done here.
#
# Also note: a client that's sent a code but never gets verified stays
# connected indefinitely as-is -- there's no expiry/cleanup here yet.
# Worth adding a background sweep (e.g. drop entries older than the
# code's actual TTL) before this runs somewhere with real traffic.
_pending_clients: dict[str, TelegramClient] = {}
_pending_lock = asyncio.Lock()


@dataclass
class AuthStartResult:
    phone_number: str
    phone_code_hash: str


@dataclass
class AuthVerifyResult:
    phone_number: str
    telegram_user_id: int
    already_existed: bool


class TelegramAuthRepository:
    """PostgreSQL persistence for Telegram session strings."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @classmethod
    async def create(cls) -> "TelegramAuthRepository":
        pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
        async with pool.acquire() as conn:
            await conn.execute(TABLE_DDL)
        return cls(pool)

    async def close(self):
        await self._pool.close()

    async def save_session(
        self, phone_number: str, telegram_user_id: int, session_string: str
    ) -> bool:
        """Upserts a session. Returns True if a new row was inserted."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO telegram_sessions (phone_number, telegram_user_id, session_string, updated_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (phone_number)
                DO UPDATE SET
                    telegram_user_id = EXCLUDED.telegram_user_id,
                    session_string   = EXCLUDED.session_string,
                    updated_at       = EXCLUDED.updated_at
                RETURNING (xmax = 0) AS inserted
                """,
                phone_number,
                telegram_user_id,
                session_string,
                datetime.now(timezone.utc),
            )
            # xmax = 0 means this row version was freshly inserted; a
            # nonzero xmax means an existing row was superseded by the
            # ON CONFLICT DO UPDATE path. The command tag from execute()
            # can't distinguish these -- Postgres reports "INSERT 0 1"
            # for both a fresh insert AND an update-via-conflict, so
            # checking `result.startswith("INSERT")` is always True.
            return row["inserted"]

    async def get_session_string(self, phone_number: str) -> Optional[str]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT session_string FROM telegram_sessions WHERE phone_number = $1",
                phone_number,
            )
            return row["session_string"] if row else None

    async def get_session_by_user_id(self, telegram_user_id: int) -> Optional[str]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT session_string FROM telegram_sessions WHERE telegram_user_id = $1",
                telegram_user_id,
            )
            return row["session_string"] if row else None


class TelegramAuthService:
    """Coordinates the Telethon login handshake and persists the result."""

    def __init__(self, repository: TelegramAuthRepository):
        self._repo = repository

    @classmethod
    async def create(cls) -> "TelegramAuthService":
        repo = await TelegramAuthRepository.create()
        return cls(repo)

    async def close(self):
        await self._repo.close()

    async def send_code(self, phone_number: str) -> AuthStartResult:
        """Step 1: ask Telegram to send a login code to the given phone."""
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            sent = await client.send_code_request(phone_number)
        except FloodWaitError as e:
            await client.disconnect()
            raise RuntimeError(f"Rate limited by Telegram, retry in {e.seconds}s") from e
        except Exception:
            await client.disconnect()
            raise

        # Keep this exact client connected and alive -- verify_code needs
        # to finish the handshake on this same connection.
        async with _pending_lock:
            _pending_clients[phone_number] = client

        return AuthStartResult(phone_number=phone_number, phone_code_hash=sent.phone_code_hash)

    async def verify_code(
        self,
        phone_number: str,
        code: str,
        phone_code_hash: str,
        password: Optional[str] = None,
    ) -> AuthVerifyResult:
        """Step 2: complete login with the code (and 2FA password if enabled)."""
        async with _pending_lock:
            client = _pending_clients.get(phone_number)

        if client is None:
            raise RuntimeError(
                "No pending login for this phone number. Call send_code first "
                "(or the code expired / the server restarted and you need to "
                "restart the flow)."
            )

        # Tracks whether this attempt failed specifically because a 2FA
        # password was needed but not supplied -- that's the one failure
        # mode where the caller is expected to retry with the SAME
        # pending client, so it must not be torn down in that case.
        needs_password = False

        try:
            try:
                await client.sign_in(
                    phone=phone_number, code=code, phone_code_hash=phone_code_hash
                )
            except SessionPasswordNeededError:
                if not password:
                    needs_password = True
                    raise ValueError("This account has 2FA enabled; 'password' is required.")
                await client.sign_in(password=password)
            except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
                raise ValueError("Invalid or expired code.") from e

            me = await client.get_me()
            session_string = client.session.save()
            inserted = await self._repo.save_session(
                phone_number=phone_number,
                telegram_user_id=me.id,
                session_string=session_string,
            )
            return AuthVerifyResult(
                phone_number=phone_number,
                telegram_user_id=me.id,
                already_existed=not inserted,
            )
        finally:
            # Clean up on every terminal outcome (success, bad code,
            # expired code, or any unexpected error) -- but NOT when we
            # just told the caller a password is needed, since that
            # leaves the login mid-flight and expects a follow-up call
            # on this same client.
            if not needs_password:
                await client.disconnect()
                async with _pending_lock:
                    _pending_clients.pop(phone_number, None)
