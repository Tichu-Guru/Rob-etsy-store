from __future__ import annotations
import time
from typing import Any
import requests
from .config import PAGE_SIZE, PRINTIFY_API_TOKEN, PRINTIFY_BASE_URL, PRINTIFY_SHOP_ID, REQUEST_TIMEOUT

class PrintifyClient:
    def __init__(self, token: str | None = None):
        token = token or PRINTIFY_API_TOKEN
        if not token:
            raise RuntimeError("PRINTIFY_API_TOKEN is not set.")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(4):
            try:
                r = self.session.get(f"{PRINTIFY_BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After","5")))
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("Printify request failed.")

    def get_shops(self):
        data = self.get("/shops.json")
        return data if isinstance(data, list) else data.get("data", [])

    def get_shop_id(self) -> str:
        if PRINTIFY_SHOP_ID:
            return str(PRINTIFY_SHOP_ID)
        shops = self.get_shops()
        if not shops:
            raise RuntimeError("No Printify shops were returned.")
        return str(shops[0]["id"])

    def get_all_products(self, shop_id: str):
        products = []
        page = 1
        while True:
            data = self.get(f"/shops/{shop_id}/products.json", params={"page": page, "limit": PAGE_SIZE})
            items = data.get("data", []) if isinstance(data, dict) else data
            if not items:
                break
            products.extend(items)
            last_page = data.get("last_page") if isinstance(data, dict) else None
            if last_page is not None and page >= int(last_page):
                break
            if len(items) < PAGE_SIZE:
                break
            page += 1
        return products

    def export_variant_rows(self, shop_id: str):
        rows = []
        for product in self.get_all_products(shop_id):
            for variant in product.get("variants", []) or []:
                rows.append({
                    "printify_product_id": product.get("id"),
                    "printify_title": product.get("title"),
                    "printify_variant_id": variant.get("id"),
                    "printify_sku": variant.get("sku"),
                    "printify_variant_title": variant.get("title"),
                    "printify_price": variant.get("price"),
                    "printify_cost": variant.get("cost"),
                    "printify_enabled": variant.get("is_enabled"),
                    "printify_available": variant.get("is_available"),
                    "print_provider_id": product.get("print_provider_id"),
                    "blueprint_id": product.get("blueprint_id"),
                    "printify_options_json": json_safe(variant.get("options", [])),
                    "printify_product_options_json": json_safe(product.get("options", [])),
                })
        return rows

def json_safe(value):
    import json
    return json.dumps(value, ensure_ascii=False, default=str)
