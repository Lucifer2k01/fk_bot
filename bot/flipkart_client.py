"""
Flipkart API Client — core logic for cart, checkout, payment.
"""
import httpx
import asyncio
import re
from typing import Optional, Dict, Tuple

from bot.config import settings
from bot.utils import extract_product_id

FK_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "FKUA/website/42/website/Desktop"
)

DEVICE_FINGERPRINT = {
    "device_information": {
        "colorDepth": 24,
        "javaEnabled": False,
        "javaScriptEnabled": True,
        "language": "en-GB",
        "screenHeight": 1080,
        "screenWidth": 1920,
        "timeDifference": -330
    },
    "device_capabilities": {
        "read_sms": False,
        "phonepe_sdk": False,
        "juspay_sdk": False,
        "nda_enabled": False,
        "upi_enabled": False
    },
    "is_diff_shown_to_user": False
}

class FlipkartAPIError(Exception):
    pass

class AuthError(FlipkartAPIError):
    pass

class DCChangeError(FlipkartAPIError):
    def __init__(self, new_dc: int):
        self.new_dc = new_dc
        super().__init__(f"DC changed to {new_dc}")

class RateLimitError(FlipkartAPIError):
    pass

class CheckoutShieldError(FlipkartAPIError):
    pass

class FlipkartClient:
    def __init__(self, cookies: Dict[str, str], dc: int = 1, proxy: Optional[str] = None):
        self.cookies = cookies
        self.dc = dc
        self.proxy = proxy
        self.max_retries = settings.max_retries

        headers = {
            "X-user-agent": FK_DESKTOP_UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Origin": "https://www.flipkart.com",
            "Referer": "https://www.flipkart.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Connection": "keep-alive",
            "DNT": "1",
        }

        proxy_config = {"http://": proxy, "https://": proxy} if proxy else None
        timeout = httpx.Timeout(15.0, connect=5.0)
        limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)

        self.session = httpx.AsyncClient(
            cookies=cookies,
            headers=headers,
            timeout=timeout,
            limits=limits,
            proxies=proxy_config,
            follow_redirects=True,
            http2=False,
        )

    def _rome_url(self, version: int, endpoint: str) -> str:
        return f"https://{self.dc}.rome.api.flipkart.com/api/{version}/{endpoint}"

    def _payments_url(self, endpoint: str) -> str:
        return f"https://{self.dc}.payments.flipkart.com/fkpay/api/v3/payments/{endpoint}"

    def _rebuild_url(self, url: str) -> str:
        """Replace DC subdomain in a URL after a DC change."""
        return re.sub(r"https://\d+\.", f"https://{self.dc}.", url, count=1)

    def _handle_status(self, data: dict, context: str = ""):
        if not isinstance(data, dict):
            raise FlipkartAPIError(f"Invalid response type: {type(data).__name__}")
        
        status = data.get("STATUS_CODE")
        msg = data.get("ERROR_MESSAGE", "")

        if status == 401:
            raise AuthError(f"Session expired ({context})")
        if status == 406 and msg == "DC Change":
            new_dc = data.get("META_INFO", {}).get("dcInfo", {}).get("id", self.dc)
            if new_dc and new_dc != self.dc:
                self.dc = new_dc
                raise DCChangeError(new_dc)
            raise FlipkartAPIError("DC Change response missing new DC info")
        if status == 429:
            raise RateLimitError(f"Rate limited ({context})")
        if status == 400:
            err_code = data.get("RESPONSE", {}).get("errorCode", "")
            if err_code == "CHECKOUT_SHIELD_RESTRICTED_ITEM":
                raise CheckoutShieldError(data.get("RESPONSE", {}).get("errorMessage", ""))

    async def _post(self, url: str, payload: dict, retries: int = 0) -> dict:
        try:
            resp = await self.session.post(url, json=payload)
            data = resp.json()
            self._handle_status(data)
            return data
        except DCChangeError:
            if retries < self.max_retries:
                await asyncio.sleep(0.2)
                return await self._post(self._rebuild_url(url), payload, retries + 1)
            raise
        except RateLimitError:
            if retries < self.max_retries:
                delay = min(200 * (2 ** retries), 5000)
                await asyncio.sleep(delay / 1000)
                return await self._post(url, payload, retries + 1)
            raise

    async def _get(self, url: str, retries: int = 0) -> dict:
        try:
            resp = await self.session.get(url)
            data = resp.json()
            self._handle_status(data)
            return data
        except DCChangeError:
            if retries < self.max_retries:
                await asyncio.sleep(0.1)
                return await self._get(self._rebuild_url(url), retries + 1)
            raise
        except RateLimitError:
            if retries < self.max_retries:
                delay = min(200 * (2 ** retries), 5000)
                await asyncio.sleep(delay / 1000)
                return await self._get(url, retries + 1)
            raise

    async def warm_up_session(self) -> bool:
        try:
            resp = await self.session.get(
                "https://www.flipkart.com",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=httpx.Timeout(15.0, connect=5.0)
            )
            if resp.status_code == 302 and "login" in (resp.headers.get("location") or "").lower():
                return False
            if resp.status_code == 401:
                return False

            await asyncio.sleep(0.5)

            test_payload = {
                "pageUri": "/",
                "pageContext": {"trackingContext": {"context": {}}},
                "locationContext": {"pincode": None, "changed": False}
            }
            api_resp = await self.session.post(
                self._rome_url(4, "page/fetch?cacheFirst=false"),
                json=test_payload,
                timeout=httpx.Timeout(10.0, connect=5.0)
            )
            api_data = api_resp.json()
            status = api_data.get("STATUS_CODE", 0)
            if status == 401:
                return False
            if status == 406 and api_data.get("ERROR_MESSAGE") == "DC Change":
                new_dc = api_data.get("META_INFO", {}).get("dcInfo", {}).get("id", self.dc)
                if new_dc:
                    self.dc = new_dc

            return True
        except Exception:
            return True

    async def ping_keep_alive(self) -> bool:
        try:
            resp = await self.session.get(
                "https://www.flipkart.com",
                headers={"Accept": "text/html"},
                timeout=httpx.Timeout(10.0, connect=5.0)
            )
            if resp.status_code == 302 and "login" in (resp.headers.get("location") or "").lower():
                return False
            if resp.status_code == 401:
                return False

            test_payload = {
                "pageUri": "/",
                "pageContext": {"trackingContext": {"context": {}}},
                "locationContext": {"pincode": None, "changed": False}
            }
            api_resp = await self.session.post(
                self._rome_url(4, "page/fetch?cacheFirst=false"),
                json=test_payload,
                timeout=httpx.Timeout(10.0, connect=5.0)
            )
            api_data = api_resp.json()
            status = api_data.get("STATUS_CODE", 0)
            if status == 401:
                return False
            if status == 406 and api_data.get("ERROR_MESSAGE") == "DC Change":
                new_dc = api_data.get("META_INFO", {}).get("dcInfo", {}).get("id", self.dc)
                if new_dc:
                    self.dc = new_dc
            return True
        except Exception:
            return True

    async def set_lst(self, product_url: str) -> Optional[str]:
        payload = {
            "pageUri": product_url,
            "pageContext": {
                "trackingContext": {
                    "context": {"eVar61": "direct_product"}
                }
            },
            "locationContext": {"pincode": None, "changed": False}
        }
        url = self._rome_url(4, "page/fetch?cacheFirst=false")
        data = await self._post(url, payload)
        try:
            response = (data.get("RESPONSE") or {}) if isinstance(data, dict) else {}
            page_data = response.get("pageData") or {}
            page_context = page_data.get("pageContext") or {}
            return page_context.get("listingId")
        except (KeyError, TypeError, AttributeError):
            return None

    async def clear_cart(self) -> bool:
        payload = {
            "pageUri": "/viewcart?exploreMode=TRUE&preference=FLIPKART",
            "pageContext": {
                "trackingContext": {
                    "context": {
                        "eVar51": "productRecommendation/p2p-cross",
                        "eVar61": "reco"
                    }
                },
                "networkSpeed": 10000
            },
            "locationContext": {"pincode": None, "changed": False}
        }
        url = self._rome_url(4, "page/fetch?cacheFirst=false")
        data = await self._post(url, payload)
        if data.get("STATUS_CODE") != 200:
            return False

        slots = (data.get("RESPONSE") or {}).get("slots", [])
        for item in slots:
            try:
                listing_id = item["widget"]["data"]["productInfo"]["baseInfo"]["value"]["listingId"]
                product_id = item["widget"]["data"]["productInfo"]["baseInfo"]["value"]["id"]
                await self._cart_item_remove(listing_id, product_id)
            except Exception:
                continue
        return True

    async def _cart_item_remove(self, listing_id: str, product_id: str):
        payload = {
            "actionRequestContext": {
                "pageUri": "/viewcart?exploreMode=true&marketplace=FLIPKART",
                "type": "CART_REMOVE",
                "pageNumber": 1,
                "items": [{"listingId": listing_id, "productId": product_id}]
            }
        }
        url = self._rome_url(1, "action/view")
        await self._post(url, payload)

    async def add_to_cart(self, lst: str, qty: int = 1, product_id: str = "") -> Tuple[bool, dict]:
        payload = {
            "cartContext": {
                lst: {
                    "productId": product_id,
                    "quantity": qty,
                    "cashifyDiscountApplied": False,
                    "vulcanDiscountApplied": False
                }
            }
        }
        url = self._rome_url(5, "cart")
        data = await self._post(url, payload)
        response = (data.get("RESPONSE") or {}) if isinstance(data, dict) else {}
        cart_response = (response.get("cartResponse") or {}) if isinstance(response, dict) else {}
        cart_resp = cart_response.get(lst, {}) if isinstance(cart_response, dict) else {}
        in_cart = cart_resp.get("presentInCart", False) if isinstance(cart_resp, dict) else False
        return in_cart, data

    async def initiate_checkout(self, lst: str, qty: int = 1) -> dict:
        payload = {
            "cartRequest": {
                "cartContext": {
                    lst: {
                        "quantity": qty,
                        "payWithEMISelected": False
                    }
                }
            },
            "checkoutType": "PHYSICAL"
        }
        url = self._rome_url(5, "checkout?loginFlow=false&view=FLIPKART")
        return await self._post(url, payload)

    async def get_payment_token(self) -> Optional[str]:
        url = self._rome_url(3, "checkout/paymentToken")
        data = await self._get(url)
        if not isinstance(data, dict):
            raise FlipkartAPIError(f"Invalid payment token response type: {type(data).__name__}")
        
        response = data.get("RESPONSE") or {}
        get_payment_token = response.get("getPaymentToken") or {}
        token = get_payment_token.get("token") if isinstance(get_payment_token, dict) else None
        if token:
            return token
        
        alert_msg_obj = get_payment_token.get("alertMessage") if isinstance(get_payment_token, dict) else {}
        if alert_msg_obj and isinstance(alert_msg_obj, dict):
            msg = alert_msg_obj.get("message", "Unknown alert")
            raise FlipkartAPIError(f"Payment token alert: {msg}")
        
        status = data.get("STATUS_CODE", 0)
        if status == 401:
            raise AuthError("Session expired during payment token fetch")
        
        # If we get here with 200 but no token, the checkout session is likely stale
        if status == 200:
            raise FlipkartAPIError("Checkout session returned empty token — session may be stale")
        
        return None

    async def cod_payout(self, token: str, retries: int = 0) -> dict:
        if not token:
            raise FlipkartAPIError("COD payout called with empty token")
        
        payload = {
            "token": token,
            "payment_instrument": "COD",
            "remove_captcha_page": True,
            **DEVICE_FINGERPRINT
        }
        url = self._payments_url("pay?instrument=COD")
        data = await self._post(url, payload)
        
        if not isinstance(data, dict):
            raise FlipkartAPIError(f"Invalid COD response type: {type(data).__name__}")
        
        if data.get("response_type") == "PAYMENT_SUCCESS":
            return data
        if retries < 5:
            await asyncio.sleep(0.2 * (retries + 1))
            return await self.cod_payout(token, retries + 1)
        raise FlipkartAPIError("COD payment failed after max retries")

    async def _auto_buy_once(self, product_url: str, lst: str, qty: int, payment_mode: str,
                             conditional_price: float = 0, use_gst: bool = True,
                             use_supercoins: bool = False) -> dict:
        """
        Single attempt at the complete auto-buy pipeline.
        """
        # Ensure session is warm before critical flow
        await self.ping_keep_alive()
        await asyncio.sleep(0.2)

        product_id = extract_product_id(product_url)

        # 1. Clear cart
        await self.clear_cart()
        await asyncio.sleep(0.2)

        # 2. Add to cart
        in_cart = False
        attempt = 0
        while not in_cart and attempt < 30:
            in_cart, _ = await self.add_to_cart(lst, qty, product_id=product_id)
            if in_cart:
                break
            attempt += 1
            await asyncio.sleep(0.6)

        if not in_cart:
            raise FlipkartAPIError("Failed to add item to cart after 30 attempts")

        # 3. Initiate checkout
        checkout_data = await self.initiate_checkout(lst, qty)

        response = (checkout_data.get("RESPONSE") or {}) if isinstance(checkout_data, dict) else {}
        order_summary = response.get("orderSummary") or {}
        
        stores = order_summary.get("requestedStores") or [{}]
        store = stores[0] if isinstance(stores, list) and stores and isinstance(stores[0], dict) else {}
        
        buyable_items = store.get("buyableStateItems") or [{}]
        item = buyable_items[0] if isinstance(buyable_items, list) and buyable_items and isinstance(buyable_items[0], dict) else {}
        
        item_info = item.get("itemPromiseInfo") or {}

        if not item_info.get("serviceable", True):
            txt = item_info.get("serviceabilityText", "Not serviceable")
            raise FlipkartAPIError(f"Not serviceable: {txt}")

        checkout_summary = order_summary.get("checkoutSummary") or {}
        grand_total = checkout_summary.get("grandTotal", 0)

        # 4. Conditional buy check
        if conditional_price > 0 and grand_total > conditional_price:
            raise FlipkartAPIError(
                f"Price too high: ₹{grand_total} > threshold ₹{conditional_price}"
            )

        # Small delay to let checkout session propagate on Flipkart's side
        await asyncio.sleep(0.5)

        # 5. Get payment token
        token = await self.get_payment_token()
        if not token:
            raise FlipkartAPIError("Could not obtain payment token")

        # 6. Execute payment (COD only for simplicity)
        if payment_mode == "cod":
            pay_data = await self.cod_payout(token)
            return {
                "payment_mode": "cod",
                "token": token,
                "data": pay_data,
                "status": "success",
                "total": grand_total
            }
        else:
            raise FlipkartAPIError(f"Payment mode '{payment_mode}' not yet supported")

    async def auto_buy(self, product_url: str, lst: str, qty: int, payment_mode: str,
                       conditional_price: float = 0, use_gst: bool = True,
                       use_supercoins: bool = False, **kwargs) -> dict:
        """
        Complete auto-buy pipeline with retry for transient session expiry.
        """
        last_error = None
        for attempt in range(2):
            try:
                return await self._auto_buy_once(
                    product_url, lst, qty, payment_mode,
                    conditional_price, use_gst, use_supercoins
                )
            except (FlipkartAPIError, AuthError) as e:
                last_error = e
                err_msg = str(e).lower()
                if ("session has expired" in err_msg or 
                    "session expired" in err_msg or
                    "stale" in err_msg or
                    "could not obtain payment token" in err_msg):
                    if attempt == 0:
                        print(f"[AutoBuy] Session issue on attempt 1, retrying after delay... ({e})")
                        await asyncio.sleep(1.5)
                        continue
                raise
        raise last_error

    async def close(self):
        await self.session.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()