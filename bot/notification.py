"""
Rich notification templates for Telegram messages.
"""
from aiogram import Bot

bot_instance = None

def set_bot(bot: Bot):
    global bot_instance
    bot_instance = bot

async def notify_price_drop(telegram_id: int, product_id: int, title: str, old_price: float, new_price: float):
    if not bot_instance:
        return
    text = (
        f"📉 <b>Price Drop Alert!</b>\n\n"
        f"🛒 <b>{title}</b>\n"
        f"   Old: <s>₹{old_price:,.0f}</s>\n"
        f"   New: <b>₹{new_price:,.0f}</b>\n\n"
        f"Use /buy_now {product_id} to purchase immediately."
    )
    await bot_instance.send_message(telegram_id, text, parse_mode="HTML")

async def notify_restock(telegram_id: int, product_id: int, title: str, price: float):
    if not bot_instance:
        return
    text = (
        f"🟢 <b>Back in Stock!</b>\n\n"
        f"🛒 <b>{title}</b>\n"
        f"   Price: <b>₹{price:,.0f}</b>\n\n"
        f"Auto-buy triggered if conditions met.\n"
        f"Use /buy_now {product_id} for instant purchase."
    )
    await bot_instance.send_message(telegram_id, text, parse_mode="HTML")

async def notify_order_placed(telegram_id: int, product_id: int, title: str, total: float, mode: str):
    if not bot_instance:
        return
    text = (
        f"✅ <b>Order Placed Successfully!</b>\n\n"
        f"🛒 <b>{title}</b>\n"
        f"   Total: <b>₹{total:,.0f}</b>\n"
        f"   Payment: <b>{mode.upper()}</b>\n\n"
        f"Check your Flipkart account for order details."
    )
    await bot_instance.send_message(telegram_id, text, parse_mode="HTML")

async def notify_buy_failed(telegram_id: int, product_id: int, title: str, error: str):
    if not bot_instance:
        return
    text = (
        f"❌ <b>Auto-Buy Failed</b>\n\n"
        f"🛒 <b>{title}</b> (Product #{product_id})\n"
        f"   Error: <code>{error[:200]}</code>\n\n"
        f"The product will continue being tracked. Use /buy_now {product_id} to retry."
    )
    await bot_instance.send_message(telegram_id, text, parse_mode="HTML")

async def notify_low_stock_warning(telegram_id: int, product_id: int, title: str, stock_count: int):
    if not bot_instance:
        return
    text = (
        f"⚠️ <b>Low Stock Warning</b>\n\n"
        f"🛒 <b>{title}</b>\n"
        f"   Only <b>{stock_count}</b> units left!\n\n"
        f"Consider using /buy_now {product_id} immediately."
    )
    await bot_instance.send_message(telegram_id, text, parse_mode="HTML")

async def notify_generic(telegram_id: int, message: str):
    if not bot_instance:
        return
    await bot_instance.send_message(telegram_id, message, parse_mode="HTML")    