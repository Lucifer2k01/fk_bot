"""
Telegram bot handlers — fully button-driven with persistent menu.
"""
import asyncio
import re
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.models import SessionLocal, User, Product, FlipkartAccount, Order, PriceHistory
from bot.utils import (
    extract_product_id, format_inr, encrypt_text, decrypt_text,
    parse_cookie_string, validate_cookies
)
from bot.flipkart_client import FlipkartClient, FlipkartAPIError, AuthError
from bot.config import settings
from bot.worker import execute_buy
from bot.notification import notify_order_placed, notify_buy_failed, notify_generic

router = Router()

# ─── In-memory buffers for auto-merging split cookie pastes ───
_cookie_buffers: dict[int, dict] = {}

# ─── Recently processed partials (for delayed second messages) ───
_recent_partials: dict[int, dict] = {}

# ─── Network Resilience Wrapper ───
import functools

async def _safe_send(message_func, max_retries=3, delay=1.0):
    """Wrap message sending with retry logic for transient network failures."""
    for attempt in range(max_retries):
        try:
            return await message_func()
        except Exception as e:
            if "Cannot connect to host" in str(e) or "ConnectionReset" in str(e):
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay * (attempt + 1))
                    continue
            raise


# ─── FSM States ───
class AccountSetup(StatesGroup):
    waiting_for_cookies = State()

class ProductSetup(StatesGroup):
    waiting_for_price = State()
    waiting_for_quantity = State()
    waiting_for_payment = State()
    confirm = State()

# ─── Keyboards ───
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔗 Track Product"), KeyboardButton(text="📊 My Products")],
            [KeyboardButton(text="👤 Account"), KeyboardButton(text="⚡ Buy Now")],
            [KeyboardButton(text="📈 Status"), KeyboardButton(text="❓ Help")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an action..."
    )

def account_menu_kb(has_account: bool) -> InlineKeyboardMarkup:
    if has_account:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Re-link Cookies", callback_data="menu_account")],
            [InlineKeyboardButton(text="🗑️ Delete Account", callback_data="delete_account")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="back_main")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Link Account", callback_data="menu_account")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back_main")],
    ])

def relink_account_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Re-link Account", callback_data="menu_account")],
    ])

def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_main")]
    ])

# ─── Cookie paste merge helpers ───

def _looks_like_cookie_continuation(text: str) -> bool:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return not re.match(r'^[A-Za-z0-9_\-]+(?:\t|\s{2,})', first_line)


def _merge_cookie_paste(part1: str, part2: str) -> str:
    part1 = part1.rstrip("\n")
    part2 = part2.lstrip("\n")

    if not part1 or not part2:
        return part1 + "\n" + part2

    lines1 = part1.splitlines()
    lines2 = part2.splitlines()

    if _looks_like_cookie_continuation(part2):
        lines1[-1] = lines1[-1] + lines2[0]
        lines2 = lines2[1:]
        return "\n".join(lines1 + lines2)

    return part1 + "\n" + part2


def _count_core_cookies(cookies: dict) -> dict:
    return {
        "auth": sum(1 for k in ["at", "rt"] if k in cookies),
        "session": sum(1 for k in ["SN", "T", "ud", "S"] if k in cookies),
        "device": sum(1 for k in ["vh", "vw", "dpr"] if k in cookies),
        "user": sum(1 for k in ["ULSN", "vd"] if k in cookies),
    }


# ─── Helper: Finalize account linking ───
async def _finalize_account_linking(message: Message, state: FSMContext, cookie_str: str):
    cookies = parse_cookie_string(cookie_str)

    if not validate_cookies(cookies):
        counts = _count_core_cookies(cookies)
        await message.answer(
            "❌ <b>That doesn't look like Flipkart cookies.</b>\n\n"
            f"Detected: {len(cookies)} cookies\n"
            f"  Auth: {counts['auth']}/2 | Session: {counts['session']}/4 | "
            f"Device: {counts['device']}/3 | User: {counts['user']}/2\n\n"
            "Please use this method:\n"
            "1. F12 → Application → Cookies → www.flipkart.com\n"
            "2. Right-click → Copy all\n"
            "3. Paste here\n\n"
            "Or type /cancel to abort.",
            parse_mode="HTML"
        )
        return False

    await message.answer("🔐 Testing cookies with Flipkart... Please wait.")

    session_valid = False
    try:
        async with FlipkartClient(cookies) as client:
            test_payload = {
                "pageUri": "/",
                "pageContext": {"trackingContext": {"context": {}}},
                "locationContext": {"pincode": None, "changed": False}
            }
            resp = await client.session.post(
                "https://1.rome.api.flipkart.com/api/4/page/fetch?cacheFirst=false",
                json=test_payload
            )
            data = resp.json()
            status = data.get("STATUS_CODE", 0)

            if status == 401:
                await message.answer(
                    "❌ <b>Session expired or invalid.</b>\n\n"
                    "Please login to Flipkart again and copy fresh cookies.\n"
                    "Type /cancel to abort.",
                    parse_mode="HTML"
                )
                return False

            session_valid = True

    except Exception as e:
        session_valid = True
        await message.answer(f"⚠️ Network issue during validation: {str(e)[:80]}\nSaving cookies anyway...")

    encrypted = encrypt_text(cookie_str)
    db = SessionLocal()
    try:
        user = get_or_create_user(message.from_user.id)
        old = db.query(FlipkartAccount).filter(FlipkartAccount.user_id == user.id).first()
        if old:
            db.delete(old)

        account = FlipkartAccount(
            user_id=user.id,
            account_name="Primary",
            cookies_encrypted=encrypted,
            is_active=True
        )
        db.add(account)
        db.commit()

        has_sn = "SN" in cookies
        has_at = "at" in cookies
        has_rt = "rt" in cookies

        status_text = []
        if has_sn:
            status_text.append("✅ SN (session)")
        if has_at:
            status_text.append("✅ at (auth)")
        if has_rt:
            status_text.append("✅ rt (refresh)")

        if not status_text:
            status_text.append("⚠️ Limited cookies")

        status_line = " | ".join(status_text)

        await message.answer(
            f"✅ <b>Account linked!</b>\n\n"
            f"{status_line}\n"
            f"Total cookies: {len(cookies)}\n\n"
            f"Tap <b>🔗 Track Product</b> to start.",
            parse_mode="HTML",
            reply_markup=main_menu_kb()
        )
    finally:
        db.close()
    await state.clear()
    return True


# ─── Helper: Accumulate split messages and auto-process ───
async def _accumulate_cookies(message: Message, state: FSMContext):
    user_id = message.from_user.id

    text = ""
    if message.text:
        text = message.text.strip()
    elif message.document:
        try:
            file = await message.bot.get_file(message.document.file_id)
            bio = await message.bot.download_file(file.file_path)
            text = bio.read().decode("utf-8", errors="replace").strip()
        except Exception as e:
            await message.answer(f"❌ Could not read file: {str(e)[:100]}")
            return

    if not text:
        return

    if user_id in _recent_partials:
        recent = _recent_partials[user_id]
        age = asyncio.get_event_loop().time() - recent["time"]
        if age < 5.0:
            combined = _merge_cookie_paste(recent["text"], text)
            _recent_partials.pop(user_id, None)
            await message.answer("🔄 Detected delayed second part — merging and re-processing...")
            success = await _finalize_account_linking(message, state, combined)
            if not success:
                _recent_partials[user_id] = {
                    "text": combined,
                    "time": asyncio.get_event_loop().time()
                }
            return
        else:
            _recent_partials.pop(user_id, None)

    if user_id in _cookie_buffers:
        old_task = _cookie_buffers[user_id].get("task")
        if old_task and not old_task.done():
            old_task.cancel()
            try:
                await old_task
            except asyncio.CancelledError:
                pass

        existing_text = _cookie_buffers[user_id]["text"]
        combined = _merge_cookie_paste(existing_text, text)
        _cookie_buffers[user_id]["text"] = combined
        _cookie_buffers[user_id]["message"] = message
    else:
        _cookie_buffers[user_id] = {
            "text": text,
            "message": message,
            "task": None
        }
        if len(text) > 1000:
            await message.answer("⏳ Receiving cookies... auto-merging parts...")

    async def _delayed_process():
        try:
            await asyncio.sleep(3.0)
            buffer = _cookie_buffers.pop(user_id, None)
            if not buffer:
                return

            final_text = buffer["text"]
            msg = buffer["message"]

            if not final_text:
                return

            success = await _finalize_account_linking(msg, state, final_text)

            if not success and len(final_text) > 2000:
                _recent_partials[user_id] = {
                    "text": final_text,
                    "time": asyncio.get_event_loop().time()
                }
                async def _cleanup_recent():
                    await asyncio.sleep(5.0)
                    _recent_partials.pop(user_id, None)
                asyncio.create_task(_cleanup_recent())

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if user_id in _cookie_buffers:
                del _cookie_buffers[user_id]
            try:
                await message.answer(f"❌ Error processing cookies: {str(e)[:200]}")
            except Exception:
                pass

    task = asyncio.create_task(_delayed_process())
    _cookie_buffers[user_id]["task"] = task


# ─── Helper: Get or create user ───
def get_or_create_user(telegram_id: int, username: str = None) -> User:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            user = User(telegram_id=telegram_id, username=username)
            db.add(user)
            db.commit()
            db.refresh(user)
        return user
    finally:
        db.close()

# ─── /start ───
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "⚡ <b>Welcome to FlashCart Bot!</b>\n\n"
        "I auto-buy Flipkart products when prices drop.\n\n"
        "Use the buttons below to navigate. You can also type commands if you prefer.",
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )

# ─── Text menu handlers ───
@router.message(F.text == "❓ Help")
async def btn_help(message: Message):
    await message.answer(
        "<b>How to use FlashCart</b>\n\n"
        "1. Tap <b>👤 Account</b> and paste your Flipkart cookies.\n"
        "2. Tap <b>🔗 Track Product</b> and send a Flipkart link.\n"
        "3. Set your target price, quantity, and payment mode.\n"
        "4. I monitor the price and buy automatically when it drops!\n\n"
        "<b>Available commands:</b>\n"
        "/start — Show menu\n"
        "/cancel — Cancel current operation\n"
        "/clear_all — Delete all your tracked products\n\n"
        "All actions are also available via the buttons below.",
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )

@router.message(F.text == "👤 Account")
async def btn_account(message: Message, state: FSMContext):
    db = SessionLocal()
    try:
        user = get_or_create_user(message.from_user.id, message.from_user.username)
        account = db.query(FlipkartAccount).filter(
            FlipkartAccount.user_id == user.id
        ).first()

        if account:
            last_used = account.last_used.strftime("%Y-%m-%d %H:%M") if account.last_used else "Never"
            text = (
                f"✅ <b>Account linked</b> (DC: {account.dc_preference})\n"
                f"Last used: {last_used}\n\n"
                f"What would you like to do?"
            )
        else:
            text = (
                "👤 <b>No Flipkart account linked.</b>\n\n"
                "Tap <b>🔗 Link Account</b> below to get started."
            )

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=account_menu_kb(bool(account))
        )
    finally:
        db.close()

@router.message(F.text == "🔗 Track Product")
async def btn_track(message: Message, state: FSMContext):
    await state.clear()
    db = SessionLocal()
    try:
        user = get_or_create_user(message.from_user.id)
        account = db.query(FlipkartAccount).filter(
            FlipkartAccount.user_id == user.id,
            FlipkartAccount.is_active == True
        ).first()
        if not account:
            await message.answer(
                "❌ <b>No account linked.</b>\n\nPlease tap 👤 Account first.",
                parse_mode="HTML",
                reply_markup=main_menu_kb()
            )
            return
    finally:
        db.close()

    await state.set_state(ProductSetup.waiting_for_price)
    await message.answer(
        "🔗 <b>Send a Flipkart product link</b> to start tracking.\n\n"
        "Example:\n"
        "<code>https://www.flipkart.com/...</code>\n\n"
        "Type /cancel to abort.",
        parse_mode="HTML",
        reply_markup=back_main_kb()
    )

@router.message(F.text == "📊 My Products")
async def btn_products(message: Message):
    await cmd_products(message)

@router.message(F.text == "⚡ Buy Now")
async def btn_buy_now(message: Message, state: FSMContext):
    await state.clear()
    db = SessionLocal()
    try:
        user = get_or_create_user(message.from_user.id)
        products = db.query(Product).filter(
            Product.user_id == user.id,
            Product.status.in_(["tracking", "failed", "paused"])
        ).order_by(Product.created_at.desc()).all()

        if not products:
            await message.answer(
                "📭 <b>No products available to buy.</b>\n\nTrack a product first.",
                parse_mode="HTML",
                reply_markup=main_menu_kb()
            )
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"#{p.id} — {'₹'+f'{p.target_price:,.0f}' if p.conditional_buy else 'Immediate'}",
                callback_data=f"instant_buy_{p.id}"
            )] for p in products
        ] + [[InlineKeyboardButton(text="⬅️ Back", callback_data="back_main")]])

        await message.answer(
            "⚡ <b>Select a product to buy immediately:</b>",
            parse_mode="HTML",
            reply_markup=kb
        )
    finally:
        db.close()

@router.message(F.text == "📈 Status")
async def btn_status(message: Message):
    await cmd_status(message)

# ─── /cancel — Universal escape hatch ───
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Nothing to cancel.", reply_markup=main_menu_kb())
        return

    user_id = message.from_user.id
    if user_id in _cookie_buffers:
        old_task = _cookie_buffers[user_id].get("task")
        if old_task and not old_task.done():
            old_task.cancel()
        _cookie_buffers.pop(user_id, None)
    _recent_partials.pop(user_id, None)

    await state.clear()
    await message.answer("❌ Cancelled.", reply_markup=main_menu_kb())

# ─── Account callbacks ───
@router.callback_query(F.data == "menu_account")
async def cb_menu_account(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "👤 <b>Link your Flipkart account</b>\n\n"
        "Paste your Flipkart cookies below.\n\n"
        "<b>How to get cookies:</b>\n"
        "1. Open flipkart.com and login\n"
        "2. Press F12 → Application tab\n"
        "3. Storage → Cookies → www.flipkart.com\n"
        "4. Right-click → Copy all\n"
        "5. Paste here\n\n"
        "<i>💡 Tip: If your cookies are very long and get split by Telegram, "
        "save them to a .txt file and send as a document instead.</i>",
        parse_mode="HTML"
    )
    await state.set_state(AccountSetup.waiting_for_cookies)
    await callback.answer()

@router.callback_query(F.data == "delete_account")
async def cb_delete_account(callback: CallbackQuery):
    db = SessionLocal()
    try:
        user = get_or_create_user(callback.from_user.id)
        account = db.query(FlipkartAccount).filter(FlipkartAccount.user_id == user.id).first()
        if account:
            db.delete(account)
            db.commit()
            await callback.message.edit_text("🗑️ Account deleted.", reply_markup=back_main_kb())
        else:
            await callback.answer("No account to delete.", show_alert=True)
    finally:
        db.close()

# ─── Cookie handlers (text + document) ───
@router.message(AccountSetup.waiting_for_cookies, F.text)
async def process_cookies_text(message: Message, state: FSMContext):
    await _accumulate_cookies(message, state)

@router.message(AccountSetup.waiting_for_cookies, F.document)
async def process_cookies_document(message: Message, state: FSMContext):
    await _accumulate_cookies(message, state)

# ─── Product URL handler ───
@router.message(F.text.contains("flipkart.com"))
async def handle_product_url(message: Message, state: FSMContext):
    current = await state.get_state()
    if current != ProductSetup.waiting_for_price.state:
        await btn_track(message, state)
        return await handle_product_url(message, state)

    url = message.text.strip()
    product_id = extract_product_id(url)

    if not product_id:
        await message.answer("❌ Could not extract product ID. Make sure it's a valid Flipkart product link.")
        return

    db = SessionLocal()
    try:
        user = get_or_create_user(message.from_user.id)
        account = db.query(FlipkartAccount).filter(
            FlipkartAccount.user_id == user.id,
            FlipkartAccount.is_active == True
        ).first()

        if not account:
            await message.answer(
                "❌ <b>No Flipkart account linked.</b>\n\n"
                "Please link your account first with 👤 Account",
                parse_mode="HTML",
                reply_markup=main_menu_kb()
            )
            return

        await state.update_data(product_url=url, product_id=product_id)
        await state.set_state(ProductSetup.waiting_for_price)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Buy immediately", callback_data="price_immediate")],
            [InlineKeyboardButton(text="💰 Set target price", callback_data="price_threshold")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_setup")],
        ])

        await message.answer(
            f"🔗 <b>Product detected!</b>\n\n"
            f"ID: <code>{product_id}</code>\n\n"
            f"<b>Buy immediately or wait for a price drop?</b>",
            parse_mode="HTML",
            reply_markup=kb
        )
    finally:
        db.close()

@router.callback_query(F.data == "price_immediate")
async def cb_price_immediate(callback: CallbackQuery, state: FSMContext):
    await state.update_data(conditional_buy=False, target_price=0)
    await ask_quantity(callback.message, state)
    await callback.answer()

@router.callback_query(F.data == "price_threshold")
async def cb_price_threshold(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProductSetup.waiting_for_price)
    await callback.message.edit_text(
        "💰 <b>Enter your target price (in ₹):</b>\n\n"
        "Example: <code>24999</code>\n"
        "I'll buy only when the price drops to or below this amount.\n\n"
        "Type /cancel to abort.",
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(ProductSetup.waiting_for_price)
async def process_price(message: Message, state: FSMContext):
    text = message.text.replace(",", "").replace("₹", "").strip()
    try:
        price = float(text)
        if price <= 0:
            raise ValueError
        await state.update_data(conditional_buy=True, target_price=price)
        await ask_quantity(message, state)
    except ValueError:
        await message.answer("❌ Please enter a valid positive number. Example: 24999\n\nType /cancel to abort.")

async def ask_quantity(message: Message, state: FSMContext):
    await state.set_state(ProductSetup.waiting_for_quantity)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1", callback_data="qty_1"),
         InlineKeyboardButton(text="2", callback_data="qty_2"),
         InlineKeyboardButton(text="3", callback_data="qty_3")],
        [InlineKeyboardButton(text="4", callback_data="qty_4"),
         InlineKeyboardButton(text="5", callback_data="qty_5")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_setup")],
    ])
    await message.answer("📦 <b>Select quantity:</b>", parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("qty_"))
async def cb_quantity(callback: CallbackQuery, state: FSMContext):
    qty = int(callback.data.replace("qty_", ""))
    await state.update_data(quantity=qty)
    await ask_payment_mode(callback.message, state)
    await callback.answer()

async def ask_payment_mode(message: Message, state: FSMContext):
    await state.set_state(ProductSetup.waiting_for_payment)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Cash on Delivery", callback_data="pay_cod")],
        [InlineKeyboardButton(text="🏦 NetBanking", callback_data="pay_netbank")],
        [InlineKeyboardButton(text="💳 Credit Card", callback_data="pay_card")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_setup")],
    ])
    await message.answer("💳 <b>Select payment mode:</b>", parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("pay_"))
async def cb_payment_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.replace("pay_", "")
    if mode in ("netbank", "card"):
        await callback.answer("Only COD auto-buy is implemented. NetBanking and Card coming soon!", show_alert=True)
        return
    await state.update_data(payment_mode=mode)
    await show_summary(callback.message, state)
    await callback.answer()

async def show_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    conditional = f"🟢 Yes — wait for ₹{data.get('target_price', 0):,.0f}" if data.get("conditional_buy") else "🔴 No — buy immediately"
    mode = data.get('payment_mode', 'cod')
    mode_display = {"cod": "💵 COD", "netbank": "🏦 NetBanking", "card": "💳 Credit Card"}.get(mode, "💵 COD")

    summary = (
        f"📋 <b>Product Setup Summary</b>\n\n"
        f"🔗 URL: <code>{data['product_url'][:60]}...</code>\n"
        f"💳 Payment: <b>{mode_display}</b>\n"
        f"📦 Quantity: <b>{data['quantity']}</b>\n"
        f"💰 Conditional Buy: {conditional}\n\n"
        f"Confirm to start tracking?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm & Track", callback_data="confirm_track")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_setup")],
    ])
    await message.answer(summary, parse_mode="HTML", reply_markup=kb)
    await state.set_state(ProductSetup.confirm)

@router.callback_query(F.data == "confirm_track")
async def cb_confirm_track(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    db = SessionLocal()
    try:
        user = get_or_create_user(callback.from_user.id)
        account = db.query(FlipkartAccount).filter(
            FlipkartAccount.user_id == user.id,
            FlipkartAccount.is_active == True
        ).first()

        if not account:
            await callback.message.answer("❌ No active account found. Link one with 👤 Account")
            await state.clear()
            await callback.answer()
            return

        cookies = parse_cookie_string(decrypt_text(account.cookies_encrypted))
        lst = None
        try:
            async with FlipkartClient(cookies, dc=account.dc_preference) as client:
                lst = await client.set_lst(data["product_url"])
        except Exception as e:
            await callback.message.answer(f"⚠️ Could not resolve product: {str(e)[:100]}\n\nProduct saved anyway, will retry.")

        product = Product(
            user_id=user.id,
            account_id=account.id,
            product_url=data["product_url"],
            listing_id=lst,
            quantity=data["quantity"],
            payment_mode=data.get("payment_mode", "cod"),
            conditional_buy=data.get("conditional_buy", False),
            target_price=data.get("target_price", 0),
            status="tracking"
        )
        db.add(product)
        db.commit()
        db.refresh(product)

        price_info = f"Target price: ₹{product.target_price:,.0f}" if product.conditional_buy else "Will buy immediately when in stock."

        await callback.message.answer(
            f"✅ <b>Product #{product.id} is now being tracked!</b>\n\n"
            f"{price_info}\n\n"
            f"Tap 📊 My Products to view all tracked items.",
            parse_mode="HTML",
            reply_markup=main_menu_kb()
        )
    finally:
        db.close()
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "cancel_setup")
async def cb_cancel_setup(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("❌ Setup cancelled.", reply_markup=main_menu_kb())
    await state.clear()
    await callback.answer()

# ─── /products ───
@router.message(Command("products"))
async def cmd_products(message: Message):
    db = SessionLocal()
    try:
        user = get_or_create_user(message.from_user.id)
        products = db.query(Product).filter(Product.user_id == user.id).order_by(Product.created_at.desc()).all()

        if not products:
            await message.answer(
                "📭 <b>No products being tracked.</b>\n\nTap 🔗 Track Product to start.",
                parse_mode="HTML",
                reply_markup=main_menu_kb()
            )
            return

        text = "<b>📊 Your Tracked Products:</b>\n\n"
        for p in products:
            emoji = {"tracking": "🔍", "buying": "🛒", "bought": "✅", "failed": "❌", "paused": "⏸️"}.get(p.status, "❓")
            price_info = f"Target: ₹{p.target_price:,.0f}" if p.conditional_buy else "Immediate buy"
            listing_status = "✅ Listing resolved" if p.listing_id else "⏳ Resolving..."
            text += (
                f"{emoji} <b>#{p.id}</b> — {p.status.upper()}\n"
                f"   💰 {price_info} | Qty: {p.quantity}\n"
                f"   💳 {p.payment_mode.upper()}\n"
                f"   {listing_status}\n\n"
            )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="refresh_products")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="back_main")]
        ])
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    finally:
        db.close()

@router.callback_query(F.data == "refresh_products")
async def cb_refresh_products(callback: CallbackQuery):
    await cmd_products(callback.message)
    await callback.answer()

# ─── Instant Buy from product list ───
@router.callback_query(F.data.startswith("instant_buy_"))
async def cb_instant_buy(callback: CallbackQuery):
    product_id = int(callback.data.replace("instant_buy_", ""))
    db = SessionLocal()
    try:
        user = get_or_create_user(callback.from_user.id)
        product = db.query(Product).filter(Product.id == product_id, Product.user_id == user.id).first()
        if not product:
            await callback.answer("Product not found.", show_alert=True)
            return

        account = db.query(FlipkartAccount).filter(FlipkartAccount.id == product.account_id).first()
        if not account:
            await callback.answer("No account linked.", show_alert=True)
            return

        await callback.message.answer(
            f"🚀 <b>Buying Product #{product_id}...</b>\n\nThis may take a few seconds.",
            parse_mode="HTML"
        )
        await callback.answer()

        cookies = parse_cookie_string(decrypt_text(account.cookies_encrypted))

        try:
            async with FlipkartClient(cookies, dc=account.dc_preference) as client:
                if not product.listing_id:
                    product.listing_id = await client.set_lst(product.product_url)
                    db.commit()

                result = await execute_buy(db, product, client)

            if client.dc != account.dc_preference:
                account.dc_preference = client.dc
                db.commit()

            if result.get("success"):
                product.status = "bought"
                order = Order(
                    user_id=user.id,
                    product_id=product.id,
                    status="success",
                    total_amount=result.get("total", 0),
                    payment_mode=product.payment_mode,
                    response_data=result
                )
                db.add(order)
                db.commit()
                await notify_order_placed(user.telegram_id, product.id, product.product_url[:40], result.get("total", 0), product.payment_mode)
                await callback.message.answer(
                    f"✅ <b>Order placed!</b>\n\n"
                    f"Product #{product.id}\n"
                    f"Total: ₹{result.get('total', 0):,.0f}\n"
                    f"Payment: {product.payment_mode.upper()}\n"
                    f"Check your Flipkart account for details.",
                    parse_mode="HTML",
                    reply_markup=main_menu_kb()
                )
            else:
                product.status = "failed"
                product.last_error = result.get("error", "Unknown")
                db.commit()
                await notify_buy_failed(user.telegram_id, product.id, product.product_url[:40], result.get("error", "Unknown"))
                await callback.message.answer(
                    f"❌ <b>Buy failed:</b> <code>{result.get('error', 'Unknown')[:200]}</code>\n\n"
                    f"The product will continue being tracked.",
                    parse_mode="HTML",
                    reply_markup=main_menu_kb()
                )
        except Exception as e:
            await callback.message.answer(f"❌ Error during buy: {str(e)[:200]}")
    finally:
        db.close()

# ─── Legacy /buy_now command ───
@router.message(Command("buy_now"))
async def cmd_buy_now(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /buy_now <product_id>\nExample: /buy_now 5")
        return

    try:
        product_id = int(args[1])
    except ValueError:
        await message.answer("❌ Invalid product ID. Use 📊 My Products to see IDs.")
        return

    db = SessionLocal()
    try:
        user = get_or_create_user(message.from_user.id)
        product = db.query(Product).filter(Product.id == product_id, Product.user_id == user.id).first()

        if not product:
            await message.answer("❌ Product not found.")
            return

        account = db.query(FlipkartAccount).filter(FlipkartAccount.id == product.account_id).first()
        if not account:
            await message.answer("❌ No account linked to this product.")
            return

        await message.answer(f"🚀 <b>Buying Product #{product_id}...</b>\n\nThis may take a few seconds.", parse_mode="HTML")

        cookies = parse_cookie_string(decrypt_text(account.cookies_encrypted))

        try:
            async with FlipkartClient(cookies, dc=account.dc_preference) as client:
                if not product.listing_id:
                    product.listing_id = await client.set_lst(product.product_url)
                    db.commit()

                result = await execute_buy(db, product, client)

            if client.dc != account.dc_preference:
                account.dc_preference = client.dc
                db.commit()

            if result.get("success"):
                product.status = "bought"
                order = Order(
                    user_id=user.id,
                    product_id=product.id,
                    status="success",
                    total_amount=result.get("total", 0),
                    payment_mode=product.payment_mode,
                    response_data=result
                )
                db.add(order)
                db.commit()
                await notify_order_placed(user.telegram_id, product.id, product.product_url[:40], result.get("total", 0), product.payment_mode)
                await message.answer(
                    f"✅ <b>Order placed!</b>\n\n"
                    f"Product #{product.id}\n"
                    f"Total: ₹{result.get('total', 0):,.0f}\n"
                    f"Payment: {product.payment_mode.upper()}\n"
                    f"Check your Flipkart account for details.",
                    parse_mode="HTML"
                )
            else:
                product.status = "failed"
                product.last_error = result.get("error", "Unknown")
                db.commit()
                await notify_buy_failed(user.telegram_id, product.id, product.product_url[:40], result.get("error", "Unknown"))
                await message.answer(
                    f"❌ <b>Buy failed:</b> <code>{result.get('error', 'Unknown')[:200]}</code>\n\n"
                    f"The product will continue being tracked.",
                    parse_mode="HTML"
                )
        except Exception as e:
            await message.answer(f"❌ Error during buy: {str(e)[:200]}")
    finally:
        db.close()

# ─── /delete command — fixed to clear price_history first ───
@router.message(Command("delete"))
async def cmd_delete(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /delete <product_id>\nExample: /delete 5")
        return
    try:
        product_id = int(args[1])
    except ValueError:
        await message.answer("❌ Invalid product ID.")
        return

    db = SessionLocal()
    try:
        user = get_or_create_user(message.from_user.id)
        product = db.query(Product).filter(Product.id == product_id, Product.user_id == user.id).first()
        if product:
            # Explicitly delete price history first to avoid FK NOT NULL issues
            db.query(PriceHistory).filter(PriceHistory.product_id == product.id).delete(synchronize_session=False)
            db.delete(product)
            db.commit()
            await message.answer(f"🗑️ Product #{product_id} deleted.", reply_markup=main_menu_kb())
        else:
            await message.answer("❌ Product not found.")
    finally:
        db.close()

# ─── /clear_all — delete all products for this user ───
@router.message(Command("clear_all"))
async def cmd_clear_all(message: Message):
    db = SessionLocal()
    try:
        user = get_or_create_user(message.from_user.id)
        products = db.query(Product).filter(Product.user_id == user.id).all()
        if not products:
            await message.answer("📭 You have no products to clear.", reply_markup=main_menu_kb())
            return

        count = 0
        for product in products:
            db.query(PriceHistory).filter(PriceHistory.product_id == product.id).delete(synchronize_session=False)
            db.delete(product)
            count += 1
        db.commit()
        await message.answer(f"🗑️ <b>Cleared {count} products</b> and all their history.", parse_mode="HTML", reply_markup=main_menu_kb())
    finally:
        db.close()

# ─── /status ───
@router.message(Command("status"))
async def cmd_status(message: Message):
    db = SessionLocal()
    try:
        user = get_or_create_user(message.from_user.id)
        products = db.query(Product).filter(Product.user_id == user.id).all()
        orders = db.query(Order).filter(Order.user_id == user.id).all()

        tracking = sum(1 for p in products if p.status == "tracking")
        buying = sum(1 for p in products if p.status == "buying")
        bought = sum(1 for p in products if p.status == "bought")
        failed = sum(1 for p in products if p.status == "failed")

        text = (
            f"<b>⚡ FlashCart Status</b>\n\n"
            f"📊 Products: {len(products)} total\n"
            f"   🔍 Tracking: {tracking}\n"
            f"   🛒 Buying: {buying}\n"
            f"   ✅ Bought: {bought}\n"
            f"   ❌ Failed: {failed}\n\n"
            f"📦 Orders: {len(orders)} total\n"
            f"⏱️ Check Interval: {settings.check_interval_seconds}s"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=main_menu_kb())
    finally:
        db.close()

# ─── Back to main menu ───
@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "🏠 <b>Main Menu</b>",
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )
    await callback.answer()

# ─── Fallback handler ───
@router.message()
async def fallback_handler(message: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        await message.answer(
            "I'm waiting for your input. Type /cancel to abort, or follow the instructions above."
        )
    else:
        await message.answer(
            "I didn't understand that. Use the buttons below or send a Flipkart product link.",
            reply_markup=main_menu_kb()
        )