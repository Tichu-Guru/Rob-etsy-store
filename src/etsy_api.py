cat src/etsy_api.py
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests


BASE_URL = "https://api.etsy.com/v3/application"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
DEFAULT_TOKEN_FILE = Path(".etsy_tokens.json")


class EtsyApiClient:
    """Small Etsy Open API v3 client used for listing inventory prices."""

    def __init__(
        self,
        api_key: str,
        shared_secret: str,
        access_token: str,
        refresh_token: str | None = None,
        token_file: Path = DEFAULT_TOKEN_FILE,
    ) -> None:
        self.api_key = api_key.strip()
        self.shared_secret = shared_secret.strip()
        self.access_token = access_token.strip()
        self.refresh_token = (
            refresh_token.strip()
            if refresh_token
            else None
        )
        self.token_file = token_file

        if not self.api_key:
            raise RuntimeError("ETSY_API_KEY is not set.")
        if not self.shared_secret:
            raise RuntimeError(
                "ETSY_API_SHARED_SECRET is not set."
            )
        if not self.access_token:
            raise RuntimeError(
                "No Etsy OAuth access token is available."
            )

    @classmethod
    def from_environment(cls) -> "EtsyApiClient":
        token_file = Path(
            os.getenv(
                "ETSY_TOKEN_FILE",
                str(DEFAULT_TOKEN_FILE),
            )
        )

        token_data: dict[str, Any] = {}
        if token_file.exists():
            try:
                token_data = json.loads(
                    token_file.read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Could not read Etsy token file: {exc}"
                ) from exc

        api_key = (
            os.getenv("ETSY_API_KEY")
            or token_data.get("api_key")
            or ""
        )

        shared_secret = os.getenv(
            "ETSY_API_SHARED_SECRET",
            "",
        )

        access_token = (
            os.getenv("ETSY_ACCESS_TOKEN")
            or token_data.get("access_token")
            or ""
        )

        refresh_token = (
            os.getenv("ETSY_REFRESH_TOKEN")
            or token_data.get("refresh_token")
            or None
        )

        return cls(
            api_key=api_key,
            shared_secret=shared_secret,
            access_token=access_token,
            refresh_token=refresh_token,
            token_file=token_file,
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "x-api-key": f"{self.api_key}:{self.shared_secret}",
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    def _save_tokens(self, token_data: dict[str, Any]) -> None:
        """Persist a refreshed token set when a local token file is used."""
        if not self.token_file:
            return

        try:
            existing: dict[str, Any] = {}
            if self.token_file.exists():
                existing = json.loads(
                    self.token_file.read_text(
                        encoding="utf-8"
                    )
                )

            existing.update(token_data)
            existing["api_key"] = self.api_key

            self.token_file.write_text(
                json.dumps(
                    existing,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            # A CI environment may intentionally make the token file
            # read-only or omit it. The current access token can still be
            # used for the rest of this run.
            pass

    def refresh_access_token(self) -> None:
        if not self.refresh_token:
            raise RuntimeError(
                "Etsy access token is invalid/expired and no refresh "
                "token is available."
            )

        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self.api_key,
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )

        if not response.ok:
            raise RuntimeError(
                "Etsy OAuth refresh failed: "
                f"HTTP {response.status_code} {response.text}"
            )

        token_data = response.json()
        new_access_token = token_data.get("access_token")
        new_refresh_token = token_data.get("refresh_token")

        if not new_access_token:
            raise RuntimeError(
                "Etsy OAuth refresh response did not contain "
                "an access_token."
            )

        self.access_token = str(new_access_token)

        if new_refresh_token:
            self.refresh_token = str(new_refresh_token)

        self._save_tokens(token_data)

    def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        url = (
            path
            if path.startswith("http")
            else f"{BASE_URL}{path}"
        )

        response = requests.request(
            method,
            url,
            headers=self.headers,
            timeout=60,
            **kwargs,
        )

        # Etsy access tokens are short-lived. If the token is rejected and
        # a refresh token is available, refresh once and retry the request.
        if response.status_code == 401 and self.refresh_token:
            self.refresh_access_token()
            response = requests.request(
                method,
                url,
                headers=self.headers,
                timeout=60,
                **kwargs,
            )

        if not response.ok:
            raise RuntimeError(
                f"Etsy API request failed: {method} {url} "
                f"HTTP {response.status_code}: {response.text}"
            )

        if not response.content:
            return {}

        return response.json()

    def get_shop_id(self) -> int:
        user_id = self.access_token.split(".", 1)[0]
        if not user_id.isdigit():
            raise RuntimeError(
                "Could not determine the Etsy user ID from the OAuth token."
            )

        data = self.request(
            "GET",
            f"/users/{user_id}/shops",
        )

        shop_id = data.get("shop_id")
        if not shop_id:
            raise RuntimeError(
                "Etsy did not return a shop_id for the authenticated user."
            )

        return int(shop_id)

    def get_all_listings(self, shop_id: int) -> list[dict[str, Any]]:
        listings: list[dict[str, Any]] = []

        # The Etsy CSV can contain listings in states other than active.
        # Fetch all states so pricing does not silently disappear.
        for state in (
            "active",
            "inactive",
            "sold_out",
            "draft",
            "expired",
        ):
            offset = 0

            while True:
                data = self.request(
                    "GET",
                    f"/shops/{shop_id}/listings",
                    params={
                        "state": state,
                        "limit": 100,
                        "offset": offset,
                    },
                )

                results = data.get("results", [])
                listings.extend(results)

                if len(results) < 100:
                    break

                offset += len(results)

        return listings

    def get_listing_inventory(
        self,
        listing_id: int,
    ) -> dict[str, Any]:
        return self.request(
            "GET",
            f"/listings/{listing_id}/inventory",
        )

    @staticmethod
    def _money_to_float(value: Any) -> float | None:
        if not isinstance(value, dict):
            return None

        amount = value.get("amount")
        divisor = value.get("divisor")

        try:
            amount = float(amount)
            divisor = float(divisor)
        except (TypeError, ValueError):
            return None

        if divisor == 0:
            return None

        return amount / divisor

    def get_listing_inventory_rows(self) -> list[dict[str, Any]]:
        shop_id = self.get_shop_id()
        listings = self.get_all_listings(shop_id)

        rows: list[dict[str, Any]] = []

        for listing in listings:
            listing_id = listing.get("listing_id")
            title = listing.get("title", "")

            if not listing_id:
                continue

            try:
                inventory = self.get_listing_inventory(
                    int(listing_id)
                )
            except RuntimeError as exc:
                # Some old/non-inventory listings may not have an inventory
                # record. Skip those instead of failing the entire sync.
                print(
                    f"Etsy inventory skipped for listing "
                    f"{listing_id}: {exc}"
                )
                continue

            for product in inventory.get("products", []):
                sku = str(
                    product.get("sku") or ""
                ).strip()

                if not sku:
                    continue

                offerings = product.get("offerings") or []
                enabled_prices: list[float] = []

                for offering in offerings:
                    if offering.get("is_deleted"):
                        continue
                    if offering.get("is_enabled") is False:
                        continue

                    price = self._money_to_float(
                        offering.get("price")
                    )
                    if price is not None:
                        enabled_prices.append(price)

                if not enabled_prices:
                    # If no enabled offering is present, keep a price-less
                    # row so the listing/SKU can still be identified.
                    price = None
                else:
                    price = min(enabled_prices)

                rows.append(
                    {
                        "etsy_api_listing_id": str(listing_id),
                        "etsy_api_title": title,
                        "etsy_api_sku": sku,
                        "etsy_api_price": price,
                    }
                )

        return rows
