"""
telegram_auth.py

FastAPI routes for the Telegram login handshake implemented in
telegram_auth_service.py.

    POST /auth/telegram/start:
        - sends the login code 
        (stands in for the OAuth "redirect to authorization URL" step; Telegram has no such URL)
    POST /auth/telegram/verify  
        - submits the code (stands in for the OAuth "receive tokens" callback step)

Wire-up example:

    from fastapi import FastAPI
    from telegram_auth_service import TelegramAuthService
    from telegram_auth import router as telegram_auth_router

    app = FastAPI()
    app.include_router(telegram_auth_router)

    @app.on_event("startup")
    async def startup():
        app.state.telegram_auth_service = await TelegramAuthService.create()

    @app.on_event("shutdown")
    async def shutdown():
        await app.state.telegram_auth_service.close()
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from telegram_auth_service import TelegramAuthService

router = APIRouter(prefix="/auth/telegram", tags=["telegram-auth"])


class StartAuthRequest(BaseModel):
    phone_number: str = Field(..., examples=["+15551234567"])


class StartAuthResponse(BaseModel):
    status: str
    phone_number: str
    phone_code_hash: str


class VerifyAuthRequest(BaseModel):
    phone_number: str
    code: str
    phone_code_hash: str
    password: Optional[str] = None  # only needed if the account has 2FA enabled


class VerifyAuthResponse(BaseModel):
    status: str
    telegram_user_id: int
    already_existed: bool


def get_auth_service(request: Request) -> TelegramAuthService:
    service = getattr(request.app.state, "telegram_auth_service", None)
    if service is None:
        raise HTTPException(status_code=500, detail="Auth service not initialized")
    return service


@router.post("/start", response_model=StartAuthResponse)
async def start_auth(payload: StartAuthRequest, request: Request):
    """Step 1: request that Telegram send a login code to the phone number."""
    service = get_auth_service(request)
    try:
        result = await service.send_code(payload.phone_number)
    except RuntimeError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to send code: {e}")

    return StartAuthResponse(
        status="code_sent",
        phone_number=result.phone_number,
        phone_code_hash=result.phone_code_hash,
    )


@router.post("/verify", response_model=VerifyAuthResponse)
async def verify_auth(payload: VerifyAuthRequest, request: Request):
    """Step 2: submit the received code (and 2FA password, if any) to finish login."""
    service = get_auth_service(request)
    try:
        result = await service.verify_code(
            phone_number=payload.phone_number,
            code=payload.code,
            phone_code_hash=payload.phone_code_hash,
            password=payload.password,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Login failed: {e}")

    return VerifyAuthResponse(
        status="authenticated",
        telegram_user_id=result.telegram_user_id,
        already_existed=result.already_existed,
    )
