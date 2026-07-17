"""
Background worker: Price tracking, auto-buy orchestration, session keep-alive.
"""
import asyncio
from datetime import datetime, timedelta

from bot.models import SessionLocal, Product, FlipkartAccount, Order, PriceHistory
from bot.flipkart_client import FlipkartClient, FlipkartAPIError, AuthError, DCChangeError
from bot.utils import decrypt_text, parse_cookie_string
from bot.config import settings
from bot.notification import (
    notify_price_drop, notify_restock, notify_order_placed, 
    notify_buy_failed, notify_generic
)

async def track_all_products():
    """Main scheduler task — checks all tracking products."""
    db = SessionLocal()
    try:
        products = db.query(Product).filter(
            Product.status.in_(["tracking", "buying"]),
            Product.next_check_at <= datetime.utcnow()
        ).all()

        if not products:
            return

        for product in products:
            product_id = product.id
            try:
                await check_product(db, product)
            except Exception as e:
                db.rollback()
                fresh = db.query(Product).filter(Product.id == product_id).first()
                if fresh:
                    fresh.error_count += 1
                    fresh.last_error = str(e)[:500]
                    db.commit()
                print(f"[Worker] Error checking product {product_id}: {e}")
    finally:
        db.close()

async def keep_all_sessions_alive():
    """
    Ping all active Flipkart accounts every few minutes to keep sessions warm.
    Prevents SN cookie expiry due to inactivity.
    """
    db = SessionLocal()
    try:
        accounts = db.query(FlipkartAccount).join(Product).filter(
            Product.status.in_(["tracking", "buying"]),
            FlipkartAccount.is_active == True
        ).distinct().all()

        for account in accounts:
            try:
                cookies = parse_cookie_string(decrypt_text(account.cookies_encrypted))
                async with FlipkartClient(cookies, dc=account.dc_preference) as client:
                    alive = await client.ping_keep_alive()
                    if not alive:
                        products = db.query(Product).filter(
                            Product.account_id == account.id,
                            Product.status.in_(["tracking", "buying"])
                        ).all()
                        for p in products:
                            p.status = "failed"
                            p.last_error = "Session expired — re-link account"
                        db.commit()
                        await notify_generic(account.user.telegram_id,
                            "❌ <b>Your Flipkart session expired.</b>\n\n"
                            "Please re-link your account with 👤 Account")
                        continue

                    if client.dc != account.dc_preference:
                        account.dc_preference = client.dc
                        db.commit()

                print(f"[KeepAlive] Account {account.id} OK (DC={account.dc_preference})")
            except Exception as e:
                print(f"[KeepAlive] Account {account.id} ping failed: {e}")
    finally:
        db.close()

async def _handle_buy_result(db, product, result, client):
    """Common result handling after execute_buy."""
    if result.get("success"):
        product.status = "bought"
        order = Order(
            user_id=product.user_id,
            product_id=product.id,
            status="success",
            total_amount=result.get("total", 0),
            payment_mode=product.payment_mode,
            response_data=result
        )
        db.add(order)
        db.commit()
        await notify_order_placed(product.user.telegram_id, product.id, 
            product.product_url[:40], result.get("total", 0), product.payment_mode)
    else:
        product.status = "failed"
        product.last_error = result.get("error", "Unknown error")
        db.commit()
        await notify_buy_failed(product.user.telegram_id, product.id, 
            product.product_url[:40], result.get("error", "Unknown"))
        # Reset to tracking so we retry
        product.status = "tracking"
        product.error_count += 1
        db.commit()

async def check_product(db, product):
    """Check a single product: fetch price, decide if buy."""
    # Re-query to avoid ObjectDeletedError if product was deleted concurrently
    product = db.query(Product).filter(Product.id == product.id).first()
    if not product or product.status not in ["tracking", "buying"]:
        return

    account = db.query(FlipkartAccount).filter(FlipkartAccount.id == product.account_id).first()
    if not account:
        product.status = "failed"
        product.last_error = "No linked account"
        db.commit()
        return

    cookies = parse_cookie_string(decrypt_text(account.cookies_encrypted))

    async with FlipkartClient(cookies, dc=account.dc_preference) as client:
        warmed = await client.warm_up_session()
        if not warmed:
            product.status = "failed"
            product.last_error = "Session expired -- re-link account"
            db.commit()
            await notify_generic(product.user.telegram_id,
                "❌ <b>Your Flipkart session expired.</b>\n\n"
                "Please re-link your account with 👤 Account")
            return
        
        if not product.listing_id:
            lst = await client.set_lst(product.product_url)
            if lst:
                product.listing_id = lst
                db.commit()
            else:
                product.last_error = "Could not resolve listing ID"
                product.next_check_at = datetime.utcnow() + timedelta(seconds=30)
                db.commit()
                return

        # ─── IMMEDIATE BUY: skip price check to avoid double checkout & rate limits ───
        if not product.conditional_buy:
            product.status = "buying"
            db.commit()
            result = await execute_buy(db, product, client)

            # Record price from result if available
            if result.get("total"):
                price_entry = PriceHistory(
                    product_id=product.id,
                    price=result.get("total", 0),
                    in_stock=True,
                    raw_data=result
                )
                db.add(price_entry)
                db.commit()

            await _handle_buy_result(db, product, result, client)
            return

        # ─── CONDITIONAL BUY: do price check before deciding ───
        try:
            checkout_data = await client.initiate_checkout(product.listing_id, product.quantity)
        except DCChangeError as dce:
            account.dc_preference = dce.new_dc
            db.commit()
            product.last_error = f"DC changed to {dce.new_dc}"
            product.next_check_at = datetime.utcnow() + timedelta(seconds=product.check_interval)
            db.commit()
            return
        except AuthError:
            product.status = "failed"
            product.last_error = "Session expired — re-link account"
            db.commit()
            await notify_generic(product.user.telegram_id, 
                "❌ <b>Your Flipkart session expired.</b>\n\nPlease re-link your account with /account")
            return
        except FlipkartAPIError as e:
            product.last_error = str(e)[:500]
            product.next_check_at = datetime.utcnow() + timedelta(seconds=product.check_interval)
            db.commit()
            return

        # Extract price & stock — fully defensive
        try:
            response = (checkout_data.get("RESPONSE") or {}) if isinstance(checkout_data, dict) else {}
            order_summary = response.get("orderSummary") or {}
            
            checkout_summary = order_summary.get("checkoutSummary") or {}
            grand_total = checkout_summary.get("grandTotal", 0)
            
            stores = order_summary.get("requestedStores") or [{}]
            store = stores[0] if isinstance(stores, list) and stores and isinstance(stores[0], dict) else {}
            
            buyable_items = store.get("buyableStateItems") or [{}]
            item = buyable_items[0] if isinstance(buyable_items, list) and buyable_items and isinstance(buyable_items[0], dict) else {}
            
            item_info = item.get("itemPromiseInfo") or {}
            serviceable = item_info.get("serviceable", False)
        except (KeyError, IndexError, TypeError):
            product.last_error = "Could not parse checkout response"
            product.next_check_at = datetime.utcnow() + timedelta(seconds=product.check_interval)
            db.commit()
            return

        price_entry = PriceHistory(
            product_id=product.id,
            price=grand_total,
            in_stock=serviceable,
            raw_data=checkout_data
        )
        db.add(price_entry)

        old_price = product.current_price
        product.current_price = grand_total
        db.commit()

        if old_price and grand_total < old_price:
            await notify_price_drop(product.user.telegram_id, product.id, 
                product.product_url[:40], old_price, grand_total)

        if not serviceable:
            product.next_check_at = datetime.utcnow() + timedelta(seconds=product.check_interval)
            db.commit()
            return

        should_buy = False
        if product.conditional_buy:
            if grand_total <= product.target_price:
                should_buy = True
        else:
            should_buy = True

        if should_buy:
            product.status = "buying"
            db.commit()
            result = await execute_buy(db, product, client)
            await _handle_buy_result(db, product, result, client)
        else:
            product.next_check_at = datetime.utcnow() + timedelta(seconds=product.check_interval)
            db.commit()

    # Persist any DC change discovered during the session
    if client.dc != account.dc_preference:
        account.dc_preference = client.dc
        db.commit()

async def execute_buy(db, product, client):
    """Execute the full auto-buy pipeline."""
    try:
        result = await client.auto_buy(
            product_url=product.product_url,
            lst=product.listing_id,
            qty=product.quantity,
            payment_mode=product.payment_mode,
            conditional_price=product.target_price if product.conditional_buy else 0,
            use_gst=True,
            use_supercoins=False
        )
        if result is None:
            return {"success": False, "error": "auto_buy returned None"}
        if not isinstance(result, dict):
            return {"success": False, "error": f"Unexpected response type: {type(result).__name__}"}
        return {"success": True, "total": result.get("total", 0), **result}
    except FlipkartAPIError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Unexpected: {str(e)}"}