import requests
from typing import Any, Dict, List
from datetime import datetime, timedelta


BASE = "https://gamma-api.polymarket.com"


MONTHS = {
    1: "january",
    2: "february",
    3: "march",
    4: "april",
    5: "may",
    6: "june",
    7: "july",
    8: "august",
    9: "september",
    10: "october",
    11: "november",
    12: "december",
}


def build_date_tokens() -> List[str]:
    today = datetime.utcnow().date()
    tomorrow = today + timedelta(days=1)

    tokens = []

    for d in [today, tomorrow]:
        month = MONTHS[d.month]
        tokens.append(f"on-{month}-{d.day}-{d.year}")

    return tokens


def fetch_events(session: requests.Session, page_size: int = 100) -> List[Dict[str, Any]]:
    url = f"{BASE}/events"
    offset = 0
    out: List[Dict[str, Any]] = []

    while True:
        params = {
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": offset,
        }

        r = session.get(url, params=params, timeout=30)
        r.raise_for_status()
        batch = r.json()

        if not batch:
            break

        out.extend(batch)

        if len(batch) < page_size:
            break

        offset += page_size

    return out


def is_target_event_slug(slug: str, tokens: List[str]) -> bool:
    s = str(slug or "").lower()

    if not s.startswith("highest-temperature-"):
        return False

    for token in tokens:
        if token in s:
            return True

    return False


def main() -> int:

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    try:

        tokens = build_date_tokens()

        print("[STEP] Date tokens:", tokens)

        print("[STEP] Loading active/open events...")
        events = fetch_events(session)

        print(f"[OK] total events loaded: {len(events)}")

        filtered = [
            e for e in events
            if is_target_event_slug(e.get("slug"), tokens)
        ]

        print(f"[OK] matched events: {len(filtered)}")

        for i, e in enumerate(filtered, 1):

            print(
                f"{i:02d}. "
                f"id={e.get('id')} "
                f"slug={e.get('slug')} "
                f"title={e.get('title')}"
            )

    except Exception as e:
        print("[ERROR]", e)

    input("\nPress Enter to exit...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())