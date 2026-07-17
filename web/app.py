"""
FastAPI web dashboard for cookie login and account management.
"""
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os

from bot.models import SessionLocal, User, FlipkartAccount
from bot.utils import encrypt_text, parse_cookie_string, validate_cookies
from bot.flipkart_client import FlipkartClient, FlipkartAPIError

app = FastAPI(title="FlashCart Bot Dashboard")

# Serve static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

class LinkAccountRequest(BaseModel):
    telegram_id: int
    cookies: str
    account_name: str = "Web"

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open(os.path.join(os.path.dirname(__file__), "dashboard.html"), "r") as f:
        return f.read()

@app.post("/api/link-account")
async def link_account(req: LinkAccountRequest):
    """Validate cookies and link Flipkart account to Telegram user."""
    cookies = parse_cookie_string(req.cookies)

    if not validate_cookies(cookies):
        raise HTTPException(status_code=400, detail="Invalid or incomplete cookies")

    # Validate session by making a test API call
    try:
        async with FlipkartClient(cookies) as client:
            # Try to get any page — if 401, cookies are bad
            test_url = "https://www.flipkart.com"
            resp = await client.session.get(test_url)
            if resp.status_code == 401 or "login" in resp.text.lower()[:500]:
                raise HTTPException(status_code=401, detail="Session expired. Please login to Flipkart again.")
    except FlipkartAPIError as e:
        raise HTTPException(status_code=401, detail=f"Flipkart API error: {str(e)}")

    # Encrypt and store
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == req.telegram_id).first()
        if not user:
            user = User(telegram_id=req.telegram_id, username="web_user")
            db.add(user)
            db.commit()
            db.refresh(user)

        # Check if account already exists
        existing = db.query(FlipkartAccount).filter(
            FlipkartAccount.user_id == user.id,
            FlipkartAccount.account_name == req.account_name
        ).first()

        encrypted = encrypt_text(req.cookies)

        if existing:
            existing.cookies_encrypted = encrypted
            existing.is_active = True
            existing.last_used = datetime.utcnow()
        else:
            account = FlipkartAccount(
                user_id=user.id,
                account_name=req.account_name,
                cookies_encrypted=encrypted
            )
            db.add(account)

        db.commit()
        return {"success": True, "message": "Account linked successfully"}
    finally:
        db.close()

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
