# Copyright (c) 2026 Andrew Thomas (OPTD, onepersontradedesk.com)
# MIT License - see LICENSE, which also carries the risk disclosure.
# This code places real orders on a real account. You are responsible
# for every order it sends.
"""TopstepX / ProjectX Gateway API client.

Thin sync wrapper around the endpoints the manual bridge needs. No business logic
here - just HTTP + auth + token refresh. Bridge owns dedupe, retry policy,
and reconciliation.

Endpoints (from swagger /swagger/v1/swagger.json):
  POST /api/Auth/loginKey
  POST /api/Order/place
  POST /api/Order/cancel
  POST /api/Order/search        (by time window)
  POST /api/Order/searchOpen
  POST /api/Position/searchOpen
  POST /api/Position/closeContract
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests


log = logging.getLogger("tsx.client")

BASE_URL = "https://api.topstepx.com"

# OrderType enum (swagger)
ORDER_TYPE_LIMIT        = 1
ORDER_TYPE_MARKET       = 2
ORDER_TYPE_STOP_LIMIT   = 3
ORDER_TYPE_STOP         = 4
ORDER_TYPE_TRAILING     = 5

# OrderSide enum (swagger)
SIDE_BID = 0  # buy
SIDE_ASK = 1  # sell


class TSXError(Exception):
    """Raised when an API call returns success=false or HTTP error."""

    def __init__(self, endpoint: str, error_code: int | None, message: str):
        self.endpoint = endpoint
        self.error_code = error_code
        self.message = message
        super().__init__(f"{endpoint}: code={error_code} msg={message}")


class TSXAuthError(TSXError):
    """Raised on auth failure - bridge should refresh token and retry once."""


@dataclass
class Credentials:
    user_name: str
    api_key: str


class TSXClient:
    def __init__(self, creds: Credentials, base_url: str = BASE_URL, timeout: float = 10.0):
        self._creds = creds
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._token: str | None = None
        self._token_lock = threading.Lock()
        self._session = requests.Session()

    # ---------- auth ----------

    def login(self) -> None:
        with self._token_lock:
            url = f"{self._base_url}/api/Auth/loginKey"
            payload = {"userName": self._creds.user_name, "apiKey": self._creds.api_key}
            log.info("auth: logging in as %s", self._creds.user_name)
            r = self._session.post(url, json=payload, timeout=self._timeout)
            r.raise_for_status()
            body = r.json()
            if not body.get("success"):
                raise TSXAuthError(
                    "loginKey", body.get("errorCode"), body.get("errorMessage") or "login failed"
                )
            token = body.get("token")
            if not token:
                raise TSXAuthError("loginKey", None, "no token in response")
            self._token = token
            log.info("auth: token acquired")

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            raise TSXAuthError("headers", None, "not logged in")
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any], retry_on_401: bool = True) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            r = self._session.post(url, json=payload, headers=self._headers(), timeout=self._timeout)
        except requests.RequestException as exc:
            raise TSXError(path, None, f"network: {exc}") from exc

        if r.status_code == 401 and retry_on_401:
            log.warning("%s: 401, refreshing token and retrying", path)
            self.login()
            return self._post(path, payload, retry_on_401=False)

        if not r.ok:
            raise TSXError(path, r.status_code, f"http {r.status_code}: {r.text[:200]}")

        body = r.json()
        if not body.get("success", False):
            raise TSXError(path, body.get("errorCode"), body.get("errorMessage") or "unknown error")
        return body

    # ---------- accounts ----------

    def search_accounts(self, only_active: bool = True) -> list[dict[str, Any]]:
        """List trading accounts visible to this API key. Returns list of
        TradingAccountModel dicts with integer `id`, `name`, `balance`, etc."""
        body = self._post(
            "/api/Account/search", {"onlyActiveAccounts": bool(only_active)}
        )
        return body.get("accounts") or []

    # ---------- orders ----------

    def place_order(
        self,
        *,
        account_id: int,
        contract_id: str,
        order_type: int,
        side: int,
        size: int,
        local_label: str,
        limit_price: float | None = None,
        stop_price: float | None = None,
        stop_loss_ticks: int | None = None,
        take_profit_ticks: int | None = None,
    ) -> dict[str, Any]:
        """Place an order. Brackets are expressed in ticks (offset from entry).

        local_label is for LOCAL LOGGING ONLY. By design we never send a
        customTag (or any tag) to TopstepX, so do NOT add it to `payload`.
        Returns parsed response body with at minimum `orderId`.
        """
        payload: dict[str, Any] = {
            "accountId": account_id,
            "contractId": contract_id,
            "type": order_type,
            "side": side,
            "size": size,
        }
        if limit_price is not None:
            payload["limitPrice"] = limit_price
        if stop_price is not None:
            payload["stopPrice"] = stop_price
        if stop_loss_ticks is not None:
            payload["stopLossBracket"] = {"ticks": stop_loss_ticks, "type": ORDER_TYPE_STOP}
        if take_profit_ticks is not None:
            payload["takeProfitBracket"] = {"ticks": take_profit_ticks, "type": ORDER_TYPE_LIMIT}

        log.info("place: label=%s %s %s sz=%s sl=%s tp=%s",
                 local_label, "BUY" if side == SIDE_BID else "SELL", contract_id,
                 size, stop_loss_ticks, take_profit_ticks)
        return self._post("/api/Order/place", payload)

    def cancel_order(self, *, account_id: int, order_id: int) -> dict[str, Any]:
        log.info("cancel: order_id=%s", order_id)
        return self._post("/api/Order/cancel", {"accountId": account_id, "orderId": order_id})

    def search_orders(
        self, *, account_id: int, start_ts: str, end_ts: str | None = None
    ) -> list[dict[str, Any]]:
        """Query orders in [start_ts, end_ts]. ISO-8601 timestamps."""
        payload: dict[str, Any] = {"accountId": account_id, "startTimestamp": start_ts}
        if end_ts is not None:
            payload["endTimestamp"] = end_ts
        body = self._post("/api/Order/search", payload)
        return body.get("orders") or []

    def search_open_orders(self, *, account_id: int) -> list[dict[str, Any]]:
        body = self._post("/api/Order/searchOpen", {"accountId": account_id})
        return body.get("orders") or []

    # ---------- positions ----------

    def search_open_positions(self, *, account_id: int) -> list[dict[str, Any]]:
        body = self._post("/api/Position/searchOpen", {"accountId": account_id})
        return body.get("positions") or []

    def close_contract(self, *, account_id: int, contract_id: str) -> dict[str, Any]:
        log.info("close: contract=%s", contract_id)
        return self._post(
            "/api/Position/closeContract",
            {"accountId": account_id, "contractId": contract_id},
        )

    def partial_close_contract(
        self, *, account_id: int, contract_id: str, size: int
    ) -> dict[str, Any]:
        log.info("partial_close: contract=%s size=%d", contract_id, size)
        return self._post(
            "/api/Position/partialCloseContract",
            {"accountId": account_id, "contractId": contract_id, "size": int(size)},
        )


def backoff_sleep(attempt: int, base: float = 0.5, cap: float = 8.0) -> None:
    """Exponential backoff with cap. attempt is 0-indexed."""
    time.sleep(min(cap, base * (2 ** attempt)))
