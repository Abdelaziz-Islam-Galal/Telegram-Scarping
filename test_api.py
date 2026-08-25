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