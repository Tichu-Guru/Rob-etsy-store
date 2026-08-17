import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
ETSY_CSV = Path(os.getenv("ETSY_CSV", BASE_DIR / "data" / "Etsy.csv"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "data" / "etsy_printify.db"))

PRINTIFY_API_TOKEN = os.getenv("PRINTIFY_API_TOKEN")
PRINTIFY_SHOP_ID = os.getenv("PRINTIFY_SHOP_ID")
PRINTIFY_BASE_URL = "https://api.printify.com/v1"
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
PAGE_SIZE = 50

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
