# FlashCart Bot — Complete Technical Context

> **Version:** 1.0.0  
> **Purpose:** Telegram bot replacement for the FlashCart Chrome Extension (v4.0.1)  
> **Architecture:** Python async (Aiogram 3 + FastAPI + APScheduler)  
> **Flipkart APIs:** Rome API (`rome.api.flipkart.com`) + Payment API (`payments.flipkart.com`)

---

## 1. What This Bot Does

The FlashCart Bot automates Flipkart purchases by:

1. **Monitoring** product prices in the background
2. **Detecting** price drops, restocks, or sale events
3. **Auto-buying** the product instantly when conditions are met
4. **Notifying** the user via Telegram with rich status updates

Unlike the Chrome extension which runs inside the browser, this bot runs as a **standalone server** that maintains Flipkart session cookies and calls Flipkart's internal APIs directly.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              TELEGRAM USER                                  │
│  • Sends product URL                                                        │
│  • Sets price threshold, payment mode, quantity                           │
│  • Receives notifications (price drop, order placed, errors)              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TELEGRAM BOT SERVER                               │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────┐   │
│  │  Aiogram 3      │  │  FastAPI        │  │  APScheduler            │   │
│  │  (handlers.py)  │  │  (web/app.py)   │  │  (worker.py)            │   │
│  │                 │  │                 │  │                         │   │
│  │  /start         │  │  /api/link-acc  │  │  track_all_products()   │   │
│  │  /account       │  │  /health        │  │  check_product()        │   │
│  │  /products      │  │  dashboard.html │  │  execute_buy()          │   │
│  │  /cards         │  │                 │  │                         │   │
│  │  /buy_now       │  │                 │  │  Runs every N seconds   │   │
│  └─────────────────┘  └─────────────────┘  └─────────────────────────┘   │
│                                    │                                        │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────┐   │
│  │  SQLite DB      │  │  Redis          │  │  FlipkartClient         │   │
│  │  (models.py)    │  │  (celery queue) │  │  (flipkart_client.py)   │   │
│  │                 │  │                 │  │                         │   │
│  │  users          │  │  task queue     │  │  httpx.AsyncClient      │   │
│  │  flipkart_accs  │  │  rate limiting  │  │  session cookies        │   │
│  │  products       │  │  caching        │  │  DC routing             │   │
│  │  credit_cards   │  │                 │  │  retry logic            │   │
│  │  orders         │  │                 │  │  payment execution      │   │
│  │  price_history  │  │                 │  │                         │   │
│  └─────────────────┘  └─────────────────┘  └─────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           FLIPKART SERVERS                                  │
│  ┌─────────────────────────────┐  ┌─────────────────────────────────────┐  │
│  │  Rome API Gateway           │  │  Payment Subdomain                  │  │
│  │  {dc}.rome.api.flipkart.com│  │  {dc}.payments.flipkart.com        │  │
│  │                             │  │                                     │  │
│  │  POST /api/4/page/fetch    │  │  POST /fkpay/api/v3/payments/pay   │  │
│  │  POST /api/1/action/view   │  │  POST /fkpay/api/v3/payments/...   │  │
│  │  POST /api/5/cart          │  │  GET  /fkpay/api/v3/payments/...   │  │
│  │  POST /api/5/checkout      │  │                                     │  │
│  │  PUT  /api/5/checkout      │  │                                     │  │
│  │  GET  /api/3/checkout/...  │  │                                     │  │
│  └─────────────────────────────┘  └─────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. How Authentication Works (The Cookie Problem)

Flipkart does not have a public API with OAuth. The bot must use **session cookies** from a logged-in browser session.

### 3.1 Cookie Extraction Flow

```
User opens flipkart.com in browser
        │
        ▼
Logs in with email/phone + OTP/password
        │
        ▼
Opens DevTools → Application → Cookies → www.flipkart.com
        │
        ▼
Copies all cookies as string (contains SN, _t_wid, vid, etc.)
        │
        ▼
PASTE into bot via:
  • Telegram: /account → paste cookies
  • Web Dashboard: guided UI at / (dashboard.html)
        │
        ▼
Bot encrypts with AES-256-GCM (PBKDF2 key derivation)
        │
        ▼
Stored in SQLite flipkart_accounts.cookies_encrypted
```

### 3.2 Required Cookies

| Cookie | Purpose |
|--------|---------|
| `SN` | Session identifier (primary) |
| `_t_wid` | Tracking / session wid |
| `vid` | Visitor ID |
| `s_v_web_id` | Web view ID |

### 3.3 Encryption

```python
# utils.py
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

kdf = PBKDF2HMAC(
    algorithm=hashes.SHA256(),
    length=32,
    salt=b"flashcart_salt_v1",
    iterations=100000,
)
key = base64.urlsafe_b64encode(kdf.derive(ENCRYPTION_KEY.encode()))
fernet = Fernet(key)

# Encrypt before storage
cookies_encrypted = fernet.encrypt(cookie_string.encode()).decode()

# Decrypt before API calls
cookie_string = fernet.decrypt(cookies_encrypted.encode()).decode()
```

### 3.4 Session Validation

Before storing, the bot makes a test API call to verify the cookies are valid:

```python
async with FlipkartClient(cookies) as client:
    resp = await client.session.get("https://www.flipkart.com")
    if resp.status_code == 401 or "login" in resp.text.lower():
        raise AuthError("Session expired")
```

---

## 4. The Auto-Buy Pipeline (Step by Step)

This is the core logic, ported from the Chrome extension's JavaScript to Python.

### 4.1 Trigger: User Sends Product URL

```
User sends: https://www.flipkart.com/.../p/itmabc123
        │
        ▼
Bot extracts product ID: itmabc123
        │
        ▼
Bot asks: Payment mode? (COD / NetBanking / Credit Card / EMI)
        │
        ▼
Bot asks: Price threshold? (Buy immediately / Wait for ₹X)
        │
        ▼
Bot asks: Quantity? (1-9)
        │
        ▼
Product saved to DB with status="tracking"
```

### 4.2 Background Worker: Price Monitoring

Every `CHECK_INTERVAL_SECONDS` (default: 5s), APScheduler runs `track_all_products()`:

```python
async def track_all_products():
    products = db.query(Product).filter(
        Product.status.in_(["tracking", "buying"]),
        Product.next_check_at <= datetime.utcnow()
    ).all()

    for product in products:
        await check_product(db, product)
```

### 4.3 Per-Product Check Flow

```
check_product(db, product)
        │
        ├── Step 1: Get FlipkartAccount cookies (decrypt)
        │
        ├── Step 2: Resolve Listing ID (if not cached)
        │   POST https://{DC}.rome.api.flipkart.com/api/4/page/fetch
        │   Payload: {"pageUri": "...", "pageContext": {...}}
        │   Response: RESPONSE.pageData.pageContext.listingId
        │
        ├── Step 3: Initiate checkout (lightweight price check)
        │   POST https://{DC}.rome.api.flipkart.com/api/5/checkout
        │   Payload: {"cartRequest": {"cartContext": {lst: {quantity: N}}}}
        │   Response: grandTotal, serviceable, cartItemRefId, gstInfo
        │
        ├── Step 4: Record price history
        │   INSERT INTO price_history (product_id, price, in_stock, raw_data)
        │
        ├── Step 5: Decision
        │   ├─ NOT serviceable → skip, notify on restock
        │   ├─ conditional_buy=True AND price > target → skip, continue tracking
        │   └─ price <= target OR immediate buy → TRIGGER AUTO-BUY
        │
        └── Step 6: Execute buy (see 4.4)
```

### 4.4 Execute Buy (Full Pipeline)

```
execute_buy(db, product, client)
        │
        ├── 1. CLEAR CART
        │   POST /api/4/page/fetch (fetch cart contents)
        │   For each item: POST /api/1/action/view (CART_REMOVE)
        │   Sleep 200ms
        │
        ├── 2. ADD TO CART (loop until success, max 30 attempts, 600ms interval)
        │   POST /api/5/cart
        │   Payload: {"cartContext": {lst: {productId: "", quantity: N}}}
        │   Check: RESPONSE.cartResponse.{lst}.presentInCart == true
        │
        ├── 3. INITIATE CHECKOUT
        │   POST /api/5/checkout?loginFlow=false&view=FLIPKART
        │   Extract: grandTotal, serviceable, cartItemRefId, gstInfo
        │   If !serviceable → retry after 1s
        │   If conditional_buy AND grandTotal > target → abort, continue tracking
        │
        ├── 4. REMOVE GST (if user disabled GST)
        │   POST /api/1/action/view
        │   Payload: CHECKOUT_UPDATE_GST with businessDetails, selected=false
        │
        ├── 5. APPLY SUPERCOINS (if enabled)
        │   PUT /api/5/checkout
        │   Payload: {"serviceType": "USE_COINS", "checkoutUpdateData": [...]}
        │
        ├── 6. GET PAYMENT TOKEN
        │   GET /api/3/checkout/paymentToken
        │   Extract: RESPONSE.getPaymentToken.token
        │   Retry with backoff on failure
        │
        └── 7. EXECUTE PAYMENT
            ├─ COD:
            │   POST payments.flipkart.com/fkpay/api/v3/payments/pay?instrument=COD
            │   Payload: token, payment_instrument="COD", remove_captcha_page=true, device fingerprint
            │   Success: response_type == "PAYMENT_SUCCESS"
            │   → Extract primary_action.target, http_method, parameters
            │   → Submit form (order placed!)
            │
            ├─ NetBanking:
            │   POST .../paywithdetails?instrument=NET_OPTIONS
            │   Payload: token, bank_code, device fingerprint
            │   Success: response_status == "SUCCESS"
            │   → Bank redirect URL returned
            │
            ├─ Credit Card:
            │   POST .../paywithdetails?instrument=CREDIT
            │   Payload: card_number, expiry_month, expiry_year, cvv, device fingerprint
            │   → 3DS redirect or success
            │
            ├─ EMI:
            │   → Browser redirect to /payments/emi/banks/plans?token=...
            │
            └─ Default / OFF:
            │   → Browser redirect to /payments?token=...
```

### 4.5 Device Fingerprint (Shared Across All Payment Calls)

```json
{
  "device_information": {
    "colorDepth": 24,
    "javaEnabled": false,
    "javaScriptEnabled": true,
    "language": "en-GB",
    "screenHeight": 1080,
    "screenWidth": 1920,
    "timeDifference": -330
  },
  "device_capabilities": {
    "read_sms": false,
    "phonepe_sdk": false,
    "juspay_sdk": false,
    "nda_enabled": false,
    "upi_enabled": false
  },
  "is_diff_shown_to_user": false
}
```

---

## 5. Error Handling & Retry Logic

### 5.1 Data Center (DC) Changes

Flipkart routes requests through different data centers. If the wrong DC is used:

```json
{
  "STATUS_CODE": 406,
  "ERROR_MESSAGE": "DC Change",
  "META_INFO": {"dcInfo": {"id": 2}}
}
```

**Handler:** Update `DC` variable, retry same request after 100-200ms delay.

### 5.2 Rate Limiting (429)

```python
if status == 429:
    delay = min(200 * (2 ** retry_count), 5000)  # Exponential backoff, cap 5s
    await asyncio.sleep(delay / 1000)
    retry
```

### 5.3 Authentication (401)

```python
if status == 401:
    # Stop retrying
    # Notify user: "Session expired — re-link your Flipkart account"
    # Set product status = "failed"
```

### 5.4 Checkout Shield (400)

```python
if status == 400 and errorCode == "CHECKOUT_SHIELD_RESTRICTED_ITEM":
    # Hard stop — Flipkart flagged the account
    # Alert user: "This account cannot order this item/quantity"
```

### 5.5 Serviceability Failures

If item is not deliverable to the pincode:
- Show serviceabilityText (e.g., "Not deliverable to your pincode")
- Retry `initiateCheckout()` after 1000ms
- Continue looping until item becomes serviceable (e.g., flash sale starts)

---

## 6. Database Schema

### 6.1 Entity Relationships

```
User (1) ───► (N) FlipkartAccount
User (1) ───► (N) Product
User (1) ───► (N) CreditCard
User (1) ───► (N) Order
Product (1) ───► (N) Order
Product (1) ───► (N) PriceHistory
```

### 6.2 Table Definitions

| Table | Key Fields | Purpose |
|-------|-----------|---------|
| `users` | telegram_id, username, is_active | Telegram user mapping |
| `flipkart_accounts` | user_id, cookies_encrypted, dc_preference, is_active | Flipkart session storage |
| `products` | user_id, account_id, product_url, listing_id, target_price, status, payment_mode | Products being tracked |
| `credit_cards` | user_id, number_encrypted, expiry_encrypted, cvv_encrypted, card_name | Payment cards |
| `orders` | user_id, product_id, flipkart_order_id, status, total_amount, response_data | Order records |
| `price_history` | product_id, price, in_stock, raw_data, created_at | Price tracking data |

---

## 7. Telegram Bot Commands

| Command | Description | FSM State |
|---------|-------------|-----------|
| `/start` | Welcome + quick start guide | — |
| `/help` | Full command reference | — |
| `/account` | Link/manage Flipkart accounts | `AccountSetup` |
| `/cards` | Add/manage credit cards | `CardSetup` |
| `/products` | List all tracked products | — |
| `/buy_now <id>` | Instant buy a tracked product | — |
| `/pause <id>` | Pause tracking | — |
| `/resume <id>` | Resume tracking | — |
| `/delete <id>` | Remove product from tracking | — |
| `/history` | View order history | — |
| `/status` | Bot system status | — |
| **Send URL** | Auto-detects Flipkart link, starts `ProductSetup` FSM | `ProductSetup` |

### 7.1 Product Setup Conversation Flow

```
User sends flipkart.com URL
        │
        ▼
[State: waiting_for_payment_mode]
├─ Inline keyboard: COD / NetBanking / Credit Card / EMI
        │
        ▼
[If NetBanking → State: waiting_for_bank]
├─ Inline keyboard: HDFC / SBI / ICICI / Axis / Kotak / More...
        │
        ▼
[If Credit Card → State: waiting_for_card]
├─ Show saved cards, or prompt to add via /cards
        │
        ▼
[State: waiting_for_price]
├─ Inline keyboard: Buy immediately / Set price threshold
├─ If threshold: prompt for ₹ amount
        │
        ▼
[State: waiting_for_quantity]
├─ Inline keyboard: 1 / 2 / 3 / 4 / 5
        │
        ▼
[State: confirm]
├─ Show summary, Confirm / Cancel
        │
        ▼
Product saved to DB, status="tracking"
```

---

## 8. Web Dashboard

A FastAPI-served web page at `/` provides a guided cookie login experience:

```
Step 1: Enter Telegram User ID
        │
        ▼
Step 2: Copy cookies from Flipkart (with visual instructions)
        │
        ▼
Step 3: Bot validates session → Encrypts → Stores → Confirms
```

**Why a web dashboard?**
- Non-technical users struggle with `/account` in Telegram
- DevTools cookie copying is easier with visual guidance
- Reduces bot support load

---

## 9. New Features (Beyond the Extension)

| Feature | Extension | Bot | Implementation |
|---------|-----------|-----|----------------|
| Multi-account | ❌ Single browser | ✅ Multiple accounts per user | `flipkart_accounts` table |
| Price history | ❌ None | ✅ Full SQLite tracking | `price_history` table |
| Price prediction | ❌ None | ✅ Linear regression | `analytics.py` |
| Restock alerts | ❌ Manual refresh | ✅ Automatic detection | Worker checks `in_stock` |
| Notifications | 🔊 Browser sound | 📱 Telegram push + rich text | `notification.py` |
| Web cookie login | ❌ None | ✅ Guided UI | `web/dashboard.html` |
| Proxy rotation | ❌ None | ✅ Configurable pool | `proxy_manager.py` |
| Distributed queue | ❌ None | ✅ Celery + Redis | `celery_app.py` |
| Order tracking | ❌ None | ✅ Full lifecycle | `orders` table |
| Analytics | ❌ None | ✅ Trend, volatility, best time | `analytics.py` |

---

## 10. Security Considerations

| Concern | Mitigation |
|---------|-----------|
| Cookie theft | AES-256-GCM encryption at rest |
| Card data theft | Same encryption, never logged |
| Session hijacking | Cookies bound to specific Telegram user |
| Rate limiting | Exponential backoff, proxy rotation |
| Account ban | Multiple accounts, proxy distribution |
| Server compromise | No plaintext storage; key in env var |

---

## 11. Deployment

### 11.1 Local Development

```bash
cd flashcart-bot
cp .env.example .env
# Edit .env with BOT_TOKEN and ENCRYPTION_KEY
pip install -r requirements.txt
python -m bot.main  # Polling mode
```

### 11.2 Docker

```bash
docker-compose up -d
# Bot + Redis running
```

### 11.3 Production (Webhook)

```bash
# Set WEBHOOK_URL=https://yourdomain.com/webhook
# Reverse proxy (nginx/caddy) → FastAPI on port 8000
# Telegram sends updates to /webhook endpoint
```

---

## 12. File Reference

| File | Lines | Purpose |
|------|-------|---------|
| `bot/main.py` | ~80 | Entry point, lifespan management, scheduler |
| `bot/config.py` | ~20 | Pydantic settings from .env |
| `bot/models.py` | ~120 | SQLAlchemy ORM definitions |
| `bot/utils.py` | ~60 | Encryption, cookie parsing, URL extraction |
| `bot/flipkart_client.py` | ~350 | Core Flipkart API client (all endpoints) |
| `bot/handlers.py` | ~450 | Telegram command handlers & FSM states |
| `bot/worker.py` | ~150 | Background price tracker & auto-buy trigger |
| `bot/notification.py` | ~80 | Rich Telegram message templates |
| `bot/analytics.py` | ~100 | Price stats, trend detection, prediction |
| `bot/proxy_manager.py` | ~40 | Proxy rotation pool |
| `bot/celery_app.py` | ~30 | Optional Celery configuration |
| `web/app.py` | ~80 | FastAPI dashboard backend |
| `web/dashboard.html` | ~150 | Guided cookie login UI |

---

## 13. API Endpoint Mapping (Extension → Bot)

| Extension (JS) | Bot (Python) | Endpoint |
|----------------|--------------|----------|
| `setLst()` | `FlipkartClient.set_lst()` | `POST /api/4/page/fetch` |
| `clearCart()` | `FlipkartClient.clear_cart()` | `POST /api/4/page/fetch` + `POST /api/1/action/view` |
| `addToCart()` | `FlipkartClient.add_to_cart()` | `POST /api/5/cart` |
| `initiateCheckout()` | `FlipkartClient.initiate_checkout()` | `POST /api/5/checkout` |
| `removeGST()` | `FlipkartClient.remove_gst()` | `POST /api/1/action/view` |
| `applySupercoins()` | `FlipkartClient.apply_supercoins()` | `PUT /api/5/checkout` |
| `getPaymentToken()` | `FlipkartClient.get_payment_token()` | `GET /api/3/checkout/paymentToken` |
| `noCaptchaCOD()` | `FlipkartClient.cod_payout()` | `POST /fkpay/api/v3/payments/pay?instrument=COD` |
| `netbankingPayout()` | `FlipkartClient.netbanking_payout()` | `POST /fkpay/api/v3/payments/paywithdetails?instrument=NET_OPTIONS` |
| `creditCardPayout()` | `FlipkartClient.credit_card_payout()` | `POST /fkpay/api/v3/payments/paywithdetails?instrument=CREDIT` |
| `getCaptcha()` | *(legacy, bypassed)* | `GET /fkpay/api/v3/payments/captcha/{token}` |

---

*End of Technical Context*
