"""Open the SuperBouwer return portal with its postcode field filled in."""

import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from rental_form import RETURN_PORTAL_URL

_portal_open = threading.Lock()


def fill_postcode(page, postcode):
    field = page.get_by_label("Uw postcode", exact=True)
    field.wait_for(state="visible", timeout=12000)
    # The portal's cookie dialog makes the form inactive until the user chooses.
    field.click(trial=True, timeout=180000)
    portal = urlsplit(RETURN_PORTAL_URL)
    current = urlsplit(page.url)
    if (current.scheme, current.netloc) != (portal.scheme, portal.netloc):
        raise ValueError("Het retourportaal is naar een andere website doorgestuurd.")
    field.fill(postcode, timeout=5000)
    if "".join(field.input_value().split()).casefold() != "".join(postcode.split()).casefold():
        raise ValueError("Het retourportaal heeft de postcode niet overgenomen.")


def open_prefilled_portal(postcode, report):
    dependencies = Path(__file__).with_name(".return-portal-deps")
    if dependencies.is_dir() and str(dependencies) not in sys.path:
        sys.path.append(str(dependencies))
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        report("Automatisch invullen is niet beschikbaar.")
        return

    if not _portal_open.acquire(blocking=False):
        report(None)
        return
    opened = False
    try:
        with sync_playwright() as playwright:
            profile = Path(__file__).with_name(".return-portal-browser")
            context = playwright.chromium.launch_persistent_context(
                profile, channel="msedge", headless=False, locale="nl-NL", timeout=15000)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(RETURN_PORTAL_URL, wait_until="domcontentloaded", timeout=20000)
            fill_postcode(page, postcode)
            opened = True
            report(None)
            page.wait_for_event("close", timeout=0)
    except (Error, OSError, ValueError):
        if not opened:
            report("Automatisch invullen van de postcode is mislukt.")
    finally:
        _portal_open.release()
