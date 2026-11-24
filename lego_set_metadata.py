"""Set details published by LEGO on its building instructions pages."""

import json
import re
import urllib.parse
import urllib.request


def parse_set_details(html, set_number):
    script = re.search(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
    if not script:
        return None
    try:
        props = json.loads(script[1])["props"]["pageProps"]["__APOLLO_STATE__"]["ROOT_QUERY"]["customerService"]
        for key, result in props.items():
            if not key.startswith("getBuildingInstructionsForSet("):
                continue
            data = result.get("data") or {}
            if str(data.get("setNumber")) != set_number:
                continue
            name = (data.get("name") or "").strip()
            pieces = str(data.get("setPieceCount") or "").strip()
            theme = (data.get("theme") or {}).get("themeName") or ""
            theme = re.sub(r"^LEGO[®™]?\s+", "", theme.strip())
            age = str(data.get("ageRating") or "").strip()
            if name:
                return name, int(pieces) if pieces.isdecimal() else None, theme, age
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return None


def fetch_set_details(number):
    # The suffix in Rebrickable-style set numbers denotes the inventory edition.
    # LEGO serves the instruction page under the base set number.
    set_number = number.split("-", 1)[0]
    details = None
    for locale in ("nl-nl", "en-us"):
        url = f"https://www.lego.com/{locale}/service/building-instructions/" + urllib.parse.quote(set_number, safe="")
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
            "Accept": "text/html",
        })
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                found = parse_set_details(response.read().decode("utf-8", errors="replace"), set_number)
        except OSError:
            continue
        if found:
            details = found if details is None else tuple(primary or fallback for primary, fallback in zip(details, found))
            if all(details):
                break
    return details
