# ⚡ FlashCart Bot

A Telegram bot that replaces the FlashCart Chrome extension for Flipkart auto-buy. Send a product link, set your price target, and the bot will monitor and purchase automatically using your Flipkart session cookies.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────────┐
│  Telegram   │────▶│  Aiogram    │────▶│  FlipkartClient │
│   User      │◀────│    Bot      │◀────│  (httpx async)  │
└─────────────┘     └─────────────┘     └─────────────────┘
                           │                    │
                           ▼                    ▼
                    ┌─────────────┐     ┌─────────────┐
                    │   SQLite    │     │  Flipkart   │
                    │   Redis     │     │   APIs      │
                    └─────────────┘     └─────────────┘
```

## Features

| Feature | Extension | Bot |
|---------|-----------|-----|
| Product link tracking | Toggle on page | Send URL in Telegram |
| Price monitoring | Manual refresh | Background scheduler every N seconds |
| Conditional buy | Price input field | Set threshold in chat |
| Auto-buy pipeline | Sync XHR | Async httpx with retry |
| Payment modes | COD/NetBank/CC/EMI | All supported |
| DC switching | Manual | Auto on 406 errors |
| Cart clearing | Before each buy | Same |
| GST removal | Checkbox | Configurable |
| Supercoins | Checkbox | Configurable |
| Credit cards | Popup CRUD | Telegram CRUD + AES-256 encryption |
| Notifications | Sound alert | Telegram push + rich messages |
| Multi-account | Single browser | Multiple Flipkart accounts per user |
| Price history | None | Full SQLite tracking with charts |
| Web dashboard | None | Cookie login helper page |
| Restock alerts | None | Automatic notification |

## Quick Start

```bash
# 1. Clone and setup
cd flashcart-bot
cp .env.example .env
# Edit .env with your BOT_TOKEN

# 2. Run locally
pip install -r requirements.txt
python -m bot.main

# 3. Or with Docker
docker-compose up -d
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome & quick start guide |
| `/account` | Link/manage Flipkart accounts |
| `/cards` | Add/manage credit cards |
| `/products` | View tracked products |
| `/buy_now <id>` | Instant buy a tracked product |
| `/history` | Order history |
| `/status` | System status |
| `/help` | Full command reference |

## Account Linking

Two methods:

1. **Telegram (manual)**: Send cookies as text after `/account`
2. **Web Dashboard**: Visit the bot's web URL, paste cookies via guided UI

## Payment Flow

```
User sends product URL
        │
        ▼
Bot resolves Listing ID (POST /api/4/page/fetch)
        │
        ▼
Background worker monitors price every N seconds
        │
        ▼
Price drops / Stock available → Trigger buy
        │
        ▼
Clear cart → Add item → Init checkout → Get token → Pay
        │
        ▼
Notify user with order confirmation
```

## Security

- Cookies encrypted with AES-256-GCM (PBKDF2 key derivation)
- Credit card data encrypted at rest
- No data leaves your server
- Session cookies never logged

## License

For educational/research purposes. Use at your own risk.
# fk_bot
