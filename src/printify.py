from __future__ import annotations

import json
import time
from typing import Any

import requests

from .config import (
    PAGE_SIZE,
    PRINTIFY_API_TOKEN,
    PRINTIFY_BASE_URL,
    PRINTIFY_SHOP_ID,
    REQUEST_TIMEOUT,
)


class PrintifyClient:
    def __init__(self, token: str | None = None):
        token = token or PRINTIFY_API_TOKEN

        if not token:
            raise RuntimeError("PRINTIFY_API_TOKEN is not set.")

        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}"}
        )

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        for attempt in range(4):
            try:
                r = self.session.get(
                    f"{PRINTIFY_BASE_URL}{path}",
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )

                if r.status_code == 429:
                    time.sleep(
                        int(r.headers.get("Retry-After", "5"))
                    )
                    continue

                r.raise_for_status()
                return r.json()

            except requests.RequestException:
                if attempt == 3:
                    raise

                time.sleep(2 ** attempt)

        raise RuntimeError("Printify request failed.")

    def get_v2(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """
        Make a request to the Printify V2 API.

        PRINTIFY_BASE_URL normally points to:
            https://api.printify.com/v1

        The V2 API uses:
            https://api.printify.com/v2
        """

        base_url = PRINTIFY_BASE_URL.rstrip("/")

        if base_url.endswith("/v1"):
            base_url = base_url[:-3] + "/v2"
        else:
            base_url = base_url.replace("/v1/", "/v2/")

        for attempt in range(4):
            try:
                r = self.session.get(
                    f"{base_url}{path}",
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )

                if r.status_code == 429:
                    time.sleep(
                        int(r.headers.get("Retry-After", "5"))
                    )
                    continue

                r.raise_for_status()
                return r.json()

            except requests.RequestException:
                if attempt == 3:
                    raise

                time.sleep(2 ** attempt)

        raise RuntimeError("Printify V2 request failed.")

    def get_shops(self):
        data = self.get("/shops.json")

        return (
            data
            if isinstance(data, list)
            else data.get("data", [])
        )

    def get_shop_id(self) -> str:
        if PRINTIFY_SHOP_ID:
            return str(PRINTIFY_SHOP_ID)

        shops = self.get_shops()

        if not shops:
            raise RuntimeError(
                "No Printify shops were returned."
            )

        return str(shops[0]["id"])

    def get_all_products(self, shop_id: str):
        products = []
        page = 1

        while True:
            data = self.get(
                f"/shops/{shop_id}/products.json",
                params={
                    "page": page,
                    "limit": PAGE_SIZE,
                },
            )

            items = (
                data.get("data", [])
                if isinstance(data, dict)
                else data
            )

            if not items:
                break

            products.extend(items)

            last_page = (
                data.get("last_page")
                if isinstance(data, dict)
                else None
            )

            if (
                last_page is not None
                and page >= int(last_page)
            ):
                break

            if len(items) < PAGE_SIZE:
                break

            page += 1

        return products

    def get_shipping_profiles(
        self,
        blueprint_id: Any,
        print_provider_id: Any,
    ):
        """
        Get U.S. standard first-item shipping costs for all
        variants of a Printify blueprint/print-provider combination.

        Printify V2 returns shipping records in this form:

        {
            "data": [
                {
                    "type": "...",
                    "id": "...",
                    "attributes": {
                        "shippingType": "standard",
                        "country": {
                            "code": "US"
                        },
                        "variantId": 123,
                        "shippingCost": {
                            "firstItem": {
                                "amount": 399,
                                "currency": "USD"
                            }
                        }
                    }
                }
            ]
        }

        Amounts are in cents, so 399 becomes $3.99.

        Returns:
            {
                "variant_id": shipping_cost_in_dollars
            }
        """

        if not blueprint_id or not print_provider_id:
            return {}

        print(
            f"SHIPPING DIAGNOSTIC: "
            f"blueprint={blueprint_id}, "
            f"provider={print_provider_id}"
        )

        try:
            data = self.get_v2(
                f"/catalog/blueprints/{blueprint_id}/"
                f"print_providers/{print_provider_id}/"
                f"shipping/standard.json"
            )

        except Exception as e:
            print(
                f"SHIPPING V2 ERROR: "
                f"blueprint={blueprint_id}, "
                f"provider={print_provider_id}, "
                f"error={e}"
            )

            return {}

        shipping_by_variant = {}

        if not isinstance(data, dict):
            return shipping_by_variant

        records = data.get("data", [])

        if not isinstance(records, list):
            return shipping_by_variant

        for record in records:
            if not isinstance(record, dict):
                continue

            attributes = record.get("attributes", {})

            if not isinstance(attributes, dict):
                continue

            country = attributes.get("country", {})

            if not isinstance(country, dict):
                continue

            country_code = country.get("code")

            # We only want U.S. shipping.
            if country_code != "US":
                continue

            variant_id = attributes.get("variantId")

            if variant_id is None:
                continue

            shipping_cost = attributes.get(
                "shippingCost",
                {},
            )

            if not isinstance(shipping_cost, dict):
                continue

            first_item = shipping_cost.get(
                "firstItem",
                {},
            )

            if not isinstance(first_item, dict):
                continue

            amount = first_item.get("amount")

            if amount is None:
                continue

            try:
                shipping_by_variant[str(variant_id)] = (
                    float(amount) / 100.0
                )

            except (TypeError, ValueError):
                continue

        print(
            f"SHIPPING PARSED: "
            f"{len(shipping_by_variant)} variants"
        )

        return shipping_by_variant

    def export_variant_rows(self, shop_id: str):
        rows = []
        shipping_cache = {}

        for product in self.get_all_products(shop_id):
            blueprint_id = product.get("blueprint_id")
            print_provider_id = product.get(
                "print_provider_id"
            )

            cache_key = (
                str(blueprint_id),
                str(print_provider_id),
            )

            if cache_key not in shipping_cache:
                shipping_cache[cache_key] = (
                    self.get_shipping_profiles(
                        blueprint_id,
                        print_provider_id,
                    )
                )

            shipping_by_variant = shipping_cache[
                cache_key
            ]

            for variant in product.get(
                "variants",
                [],
            ) or []:

                variant_id = variant.get("id")

                rows.append(
                    {
                        "printify_product_id": product.get(
                            "id"
                        ),
                        "printify_title": product.get(
                            "title"
                        ),
                        "printify_variant_id": variant_id,
                        "printify_sku": variant.get(
                            "sku"
                        ),
                        "printify_variant_title": variant.get(
                            "title"
                        ),
                        "printify_price": variant.get(
                            "price"
                        ),
                        "printify_cost": variant.get(
                            "cost"
                        ),
                        "printify_enabled": variant.get(
                            "is_enabled"
                        ),
                        "printify_available": variant.get(
                            "is_available"
                        ),
                        "print_provider_id": (
                            print_provider_id
                        ),
                        "blueprint_id": blueprint_id,
                        "printify_options_json": json_safe(
                            variant.get(
                                "options",
                                [],
                            )
                        ),
                        "printify_product_options_json": (
                            json_safe(
                                product.get(
                                    "options",
                                    [],
                                )
                            )
                        ),
                        "printify_shipping_cost": (
                            shipping_by_variant.get(
                                str(variant_id)
                            )
                        ),
                    }
                )

        return rows


def json_safe(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        default=str,
    )
