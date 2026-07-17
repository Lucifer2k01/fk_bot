"""Utility helpers: encryption, cookie parsing, URL extraction."""
import re
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from bot.config import settings

def _get_fernet():
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"flashcart_salt_v1",
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(settings.encryption_key.encode()))
    return Fernet(key)

def encrypt_text(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()

def decrypt_text(cipher: str) -> str:
    return _get_fernet().decrypt(cipher.encode()).decode()

def parse_cookie_string(cookie_str: str) -> dict:
    """Parse browser cookie string into dict. Handles multiple formats."""
    cookies = {}
    cookie_str = cookie_str.strip()
    if not cookie_str:
        return cookies

    # Handle tab-separated format (DevTools Application → Cookies → drag copy)
    if "\t" in cookie_str:
        for line in cookie_str.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # Skip header rows
            if line.lower().startswith("name\t") or line.lower().startswith("cookie\t"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[0].strip()
                value = parts[1].strip()
                # Skip rows where name looks like a column header
                if name and value and name not in ("Name", "Value", "Domain", "Path", "Expires"):
                    cookies[name] = value
    else:
        # Handle semicolon-separated format (document.cookie)
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k and v:
                    cookies[k] = v
    return cookies

def extract_product_id(url: str) -> str:
    """Extract Flipkart product ID from URL."""
    m = re.search(r"/p/itm[a-zA-Z0-9]+", url)
    if m:
        return m.group(0).split("/")[-1]
    m = re.search(r"itm[a-zA-Z0-9]+", url)
    if m:
        return m.group(0)
    return ""

def format_inr(amount: float) -> str:
    return f"₹{amount:,.0f}"

def validate_cookies(cookies: dict) -> bool:
    """
    Validate that we have the minimum cookies needed for Flipkart auth.
    """
    if not cookies or len(cookies) < 2:
        return False
    # Must have at least one core auth cookie
    core_auth = ["SN", "at", "rt", "T"]
    has_core = any(k in cookies for k in core_auth)
    # And at least one session cookie
    session = ["SN", "T", "ud", "vd", "S"]
    has_session = any(k in cookies for k in session)
    return has_core and has_session