"""Read printed main step numbers and their parts panels from LEGO booklets."""
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

from tree_sort import sortable_headings
from lego_pdf_text import quantity_words
from recognition_knowledge import index_explanations, learned_reviews, save_index_explanation, save_reviews

import pymupdf as pdf


CACHE_VERSION = 15
USAGE_CACHE_VERSION = 2


def _review_source(downloads, path):
    return path.relative_to(downloads).as_posix()


def reviewed_drawings(downloads, current_review, element_ids, knowledge_path=None):
    """Reuse only unambiguous decisions for an identical full-colour drawing."""
    accepted = defaultdict(set)
    rejected = defaultdict(set)
    local = set()
    decisions = {}
    for path in sorted(set(downloads.glob("*/bouwstapcontroles.sqlite3")) |
                       set(downloads.glob("bouwstapcontroles-*.sqlite3"))):
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                confirmations = (list(db.execute("SELECT beeld, element FROM bevestigd"))
                                 if "bevestigd" in tables else [])
                corrections = (list(db.execute("SELECT beeld, element, vervanging FROM correcties"))
                               if "correcties" in tables else [])
        except sqlite3.Error:
            continue
        source = _review_source(downloads, path)
        decisions.update(((source, drawing, number), number) for drawing, number in confirmations)
        decisions.update(((source, drawing, original), replacement) for drawing, original, replacement in corrections)
        if knowledge_path and (confirmations or corrections):
            save_reviews(knowledge_path, source, confirmations, corrections)
        if path == current_review:
            local.update(beeld for beeld, _ in confirmations)
            local.update(beeld for beeld, _, _ in corrections)
    if knowledge_path:
        for source, drawing, original, replacement in learned_reviews(knowledge_path):
            decisions.setdefault((source, drawing, original), replacement)
    for (_, drawing, original), replacement in decisions.items():
        if replacement != original:
            rejected[drawing].add(original)
        if replacement:
            accepted[drawing].add(replacement)
    return {drawing: next(iter(numbers)) for drawing, numbers in accepted.items()
            if drawing and drawing not in local and len(numbers) == 1
            and numbers <= element_ids and not numbers & rejected[drawing]}


def explained_difference(explanations, number, expected, found):
    explanation = explanations.get(number, ())
    return expected != found and tuple(explanation[:2]) == (expected, found)


def comparison_counts(index_totals, usage, explanations):
    opened = explained = 0
    for number, expected in index_totals.items():
        found = sum(line[5] for line in usage.get(number, {}).values())
        if explained_difference(explanations, number, expected, found):
            explained += 1
        elif found != expected:
            opened += 1
    return opened, explained


def usage_review_signature(analysis_signature, confirmed, corrections):
    return hashlib.sha256(json.dumps([
        USAGE_CACHE_VERSION, analysis_signature, sorted(confirmed), sorted(corrections)
    ]).encode()).hexdigest()


def saved_step_usage(path):
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            analysis = db.execute("SELECT versie FROM gebruiksanalyse WHERE id=1").fetchone()
            if not analysis:
                return None
            signature = usage_review_signature(
                analysis[0], db.execute("SELECT beeld, element FROM bevestigd"),
                db.execute("SELECT beeld, element, vervanging FROM correcties"),
            )
            row = db.execute("SELECT regels FROM gebruiksoverzicht WHERE id=1 AND versie=?",
                             (signature,)).fetchone()
        return json.loads(row[0]) if row else None
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None


def save_offline_usage(path, entries, read_item, index_totals, images, explanations=None):
    if not entries or not index_totals:
        return False
    totals = Counter()
    steps = []
    for item in entries:
        rows = []
        for book, row, qty, record, status in read_item(item):
            if not record or record[0] not in index_totals:
                return False
            totals[record[0]] += qty * row["repeat"]
            rows.append([book, {key: row[key] for key in ("step", "page", "repeat")}, qty, record, status])
        steps.append(rows)
    accepted = {}
    for number, expected in index_totals.items():
        found = totals[number]
        if expected != found:
            if not explained_difference(explanations or {}, number, expected, found):
                return False
            accepted[number] = explanations[number]
    data = dict(version=1, steps=steps, index=index_totals,
                explanations=accepted,
                images={n: str(images[n]) for n in totals if n in images and images[n]})
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return True


def open_offline_usage(parent, path, explanations=None):
    from PIL import Image, ImageTk

    data = json.loads(path.read_text(encoding="utf-8"))
    if data["version"] != 1 or not data["steps"] or not data["index"]:
        raise ValueError("Dit bestand bevat geen ondersteund offline overzicht.")
    steps = data["steps"]
    accepted = data.get("explanations", {})
    if explanations is not None and any(tuple(value) != explanations.get(number) for number, value in accepted.items()):
        raise ValueError("Een verklaring is gewijzigd. Open de set en bekijk de bouwstappen per onderdeel opnieuw.")
    images = data["images"]
    thumbnails = {}

    def thumbnail(record):
        number = record[0]
        if number in images and number not in thumbnails:
            try:
                with Image.open(images[number]) as source:
                    image = source.convert("RGBA")
                image.thumbnail((62, 48), Image.Resampling.LANCZOS)
                thumbnails[number] = ImageTk.PhotoImage(image, master=parent)
            except OSError:
                thumbnails[number] = ""
        return thumbnails.get(number, "")

    window = show_usage(parent, range(len(steps)), steps.__getitem__, (), thumbnail,
                        data["index"], ready=True, explanations=accepted)
    window.title(f"Bouwstappen per onderdeel · {path.parent.name} · Offline")


def page_steps(page):
    words = list(dict.fromkeys(tuple(w[:5]) for w in page.get_text("words")))
    layout = page.get_text("rawdict", flags=0)
    quantities = quantity_words(page, words=words, layout=layout)
    # LEGO sometimes prints two coincident text layers and several frame outlines.
    boxes = {tuple(d["rect"]) for d in page.get_cdrawings() if "rect" in d}
    boxes = [pdf.Rect(b) for b in boxes if b[2]-b[0] > 20 and b[3]-b[1] > 15]
    found = {}
    glyphs = {}
    for block in layout["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    if char["c"].isascii() and char["c"].isdigit() and char["bbox"][3]-char["bbox"][1] >= 23:
                        gx, gy = char["origin"]
                        glyphs[round(gx, 1), round(gy, 1)] = (char["c"], char["bbox"][2])
    for x, y, right, bottom, number in words:
        if not number.isascii() or not number.isdigit() or bottom-y < 23:
            continue
        candidates = [b for b in boxes if abs(b.x0-x) <= 5 and -4 <= y-b.y1 <= 12
                      and any(pdf.Point(q[0]+1, (q[1]+q[3])/2) in b for q in quantities)]
        if candidates:
            box = min(candidates, key=lambda b: abs(y-b.y1) + abs(x-b.x0))
            # Some parts panels have a partial border and a wider background.
            # Extend to the co-located frame when it contains more quantity labels.
            aligned = [b for b in candidates if abs(b.x0-box.x0) < 1 and abs(b.y1-box.y1) < 1
                       and (abs(b.y0-box.y0) < 1
                            or (abs(b.x1-box.x1) < 1 and 0 < box.y0-b.y0 < 30))]
            box = max(aligned, key=lambda b: (sum(pdf.Rect(q[:4]) in b for q in quantities),
                                              b.height,
                                              -abs(b.x0-box.x0)))
            key = tuple(box)
            digits = []
            last_edge = x
            for (gx, gy), (digit, edge) in sorted(glyphs.items()):
                if gx < x-.2 or not y < gy < bottom:
                    continue
                if gx > last_edge + 2:
                    break
                digits.append(digit)
                last_edge = edge
            if key in found and last_edge-x <= found[key][0]:
                continue
            value = int("".join(digits) or number)
            found[key] = (last_edge-x, dict(step=value, page=page.number+1,
                                        box=list(box), repeat=1, local=[]))
    steps = sorted((row for _, row in found.values()), key=lambda s: s["step"])
    events = []
    for x, y, right, bottom, label in quantities:
        count = int(label[:-1])
        if count < 2 or bottom-y < 15:
            continue
        midpoint = pdf.Point((x+right)/2, (y+bottom)/2)
        enclosing = [b for b in boxes if midpoint in b and b.width > right-x+8
                     and b.height > bottom-y and b.get_area() < page.rect.get_area()*.8]
        opening = [s for s in steps if any(abs(b.x0-s["box"][0]) < 5
                   and -2 <= s["box"][1]-b.y1 <= 35 for b in enclosing)]
        if opening:
            events.append(("open", opening[0]["step"], count))
        elif steps and bottom-y >= 28 and any(
                b.x1 < min(s["box"][0] for s in steps)
                and b.height > 3*(bottom-y)
                and (0 <= b.y1-bottom < 20 or (-4 <= b.y1-bottom < 0
                     and any(s["box"][1] < b.y1 and s["box"][3] > b.y0 for s in steps)))
                and 0 <= b.x1-right < 20
                for b in enclosing):
            # Some booklets put the assembly overview on the left, beside
            # the first parts panels. The closing marker still ends the range.
            events.append(("open", steps[0]["step"], count))
        elif not enclosing and bottom-y >= 28:
            before = [s for s in steps if s["box"][1] < bottom and s["box"][0] <= right]
            events.append(("close", before[-1]["step"] if before else None, count))
        elif enclosing:
            before = [s for s in steps if s["box"][1] < y and s["box"][0] <= x]
            if before and count not in before[-1]["local"]:
                before[-1]["local"].append(count)
    events = set(events)
    for row in steps:
        number = row["step"]
        box = pdf.Rect(row["box"])
        panel_counts = [int(q[4][:-1]) for q in quantities
                        if pdf.Point(q[0]+1, (q[1]+q[3])/2) in box]
        for opening in tuple(events):
            kind, start, count = opening
            closing = ("close", number, count)
            if kind != "open" or start != number or closing not in events:
                continue
            # A single-step assembly can already list the totals for all copies.
            if panel_counts and all(n % count == 0 for n in panel_counts):
                events.difference_update((opening, closing))
                if count not in row["local"]:
                    row["local"].append(count)
    return steps, events


def read_book(path):
    rows = []
    pending = None
    with pdf.open(path) as document:
        for page in document:
            steps, events = page_steps(page)
            previous_count = len(rows)
            rows.extend(steps)
            for kind, number, count in sorted(set(events), key=lambda e: (e[1] or 0, e[0] == "close")):
                if kind == "open":
                    pending = number, count
                    opening = next(r for r in steps if r["step"] == number)
                    opening_index = previous_count + steps.index(opening)
                    counts = [int(w[4][:-1]) for w in quantity_words(page, clip=pdf.Rect(opening["box"]))]
                    totals_in_panel = bool(counts) and all(q % count == 0 for q in counts)
                elif pending and pending[1] == count and rows:
                    end = number if number is not None else rows[-1]["step"]
                    if end == pending[0] and totals_in_panel:
                        if count not in opening["local"]:
                            opening["local"].append(count)
                        pending = None
                        continue
                    for row in rows[opening_index:]:
                        if pending[0] <= row["step"] <= end:
                            row["repeat"] = count
                    pending = None
    # In this edition the 4x beam shown before step 612 is installed in
    # step 618. There is no closing 4x label for the parser to pair with it.
    # Bind the verified exception to the booklet content, including renamed PDFs.
    if rows and (rows[0]["page"] in (4, 8, 11, 18)
                 or any(row["step"] in (138, 343, 418, 612, 792, 1542) for row in rows)):
        with open(path, "rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest == "97c143d19e9242899848d0a1cab7ca9a8280eaf8f1365967650f7e2cbefca062":
            for row in rows:
                if 612 <= row["step"] <= 617:
                    row["repeat"] = 4
        if digest == "3fa8fc0bfaf40b889bddf5ed82369f448855f22d51ec16b4dcbffa9e493714ad":
            # Step 418 illustrates the tyre and rim coming from outside bag 10,
            # then shows the assembled wheel four times. The parts panel lists
            # the wheel cover and other fittings only.
            for row in rows:
                if (row["step"], row["page"]) == (418, 369):
                    row["extra_parts"] = [["6284703", 4], ["6522736", 4]]
        if digest == "53ee82d5c9e33394f965e9490c4b45ebb310cb349e75980dbe7d18ba52fea9ac":
            # The blue baseplates are shown above the panels, outside the bags.
            # Steps 24 and 42 start two-copy series.
            for row in rows:
                if row["step"] in (1, 24, 42):
                    row["extra_parts"] = [["4226002", 2 if row["step"] == 1 else 1]]
        if digest == "1e6509db14aef30b40a754c65a19e3484d244fa145917d9abc4e5d82cf631159":
            # Page 7 builds the driver before numbered car step 1. It shows
            # both head options and the spanner outside a quantity panel.
            rows.insert(0, dict(step=0, page=7, box=[15, 15, 265, 120], repeat=1, local=[],
                                extra_parts=[[n, 1] for n in ("4162916", "6167576", "6218244",
                                                            "6409759", "6535184", "6540411")]))
        if digest == "537e1ba331dd2df932b7e47241a4720e59967724fb9f136abc004dd2da11e64f":
            # The bag overview begins before step 1, and step 33 shows
            # completion of the second copy. Ten base modules are in the
            # overview on page 10, five for each copy of the foundation.
            for row in rows:
                if 1 <= row["step"] <= 33:
                    row["repeat"] *= 2
                if row["step"] == 1:
                    row["extra_parts"] = [["6301768", 5]]
        if digest == "a411b9f521c1ca082fcab0ba4a86e6deb7d7ddd177b052ffe4b5357a06e049df":
            # The inset at step 367 contains its own 2x marker. The outer
            # marker on page 215 applies to the entire nacelle from step 343.
            for row in rows:
                if 343 <= row["step"] <= 368:
                    row["repeat"] = 2
        if digest == "b39288826f0aa1459f5d8cb38133011674d15e2671b16de7150337fc00bafe78":
            # Step 914 shows both completed minifigures without a parts panel.
            rows.append(dict(step=914, page=532, box=[12, 16, 728, 236], repeat=1, local=[],
                             extra_parts=[[n, 1] for n in ("6346773", "6359105", "6346770",
                                 "6051389", "6170191", "6308125", "6346772", "6360604",
                                 "6358144", "4498713")]))
        if digest == "f8d282cdab591a7c060761cddb4aff81bdc4b6019b34308ef2d68fbb86ab5c99":
            # Step 1543 builds Rex and shows the completed Admiral Yularen.
            figures = ["6065515", "6120935", "6447201", "6447202", "6447206",
                       "6447207", "6457102", "6461081", "6461160", "6461466"]
            rows.append(dict(step=1543, page=240, box=[12, 20, 730, 252], repeat=1, local=[],
                             extra_parts=[[n, 1] for n in figures] + [["6103643", 2]]))
        if digest == "0b039b4e31dca3328354cb5de8a99d846d81f1dc08566664412ae14cee29ad06":
            # These two flags and the one in booklet 3 are cut from the same
            # sheet. Booklet 3 already accounts for the single inventory item.
            flags = {
                138: "fdf05cd178ae1c1b53826ed533e7d8c07096beab378b5b7b070a6f6e761fbf09",
                139: "a9d8e083ee0ef84cdb6b810a41351ddb438645426f0122db0fb7d2e87ebfcb2d",
            }
            for row in rows:
                if row["step"] in flags:
                    row["sheet_drawings"] = {flags[row["step"]]: "6370720"}
        if digest == "44b1d9189ae8c022b266e721c79df9853c2d9ebc8d8b37ab20ee34958bb4a294":
            # The three snowtroopers are assembled on the unnumbered page
            # immediately before step 489. The officer has a different torso.
            rows.append(dict(step=489, page=103, box=[495, 10, 729, 525], repeat=1, local=[],
                             extra_parts=[["6258810", 3], ["6258909", 3], ["4498713", 3],
                                          ["6258899", 2], ["6356866", 1]]))
            rows.sort(key=lambda row: (row["page"], row["step"]))
        if digest == "412c749caa43090e877b6c526dd6983d6b8b6f5b47e30b5aba4f66972250596e":
            # General Veers and both drivers are assembled on the bag-14
            # overview, immediately before the cockpit at step 792.
            rows.insert(0, dict(step=792, page=8, box=[135, 225, 675, 526], repeat=1, local=[],
                                extra_parts=[["6120935", 1], ["6308067", 1], ["6308093", 1],
                                             ["6311110", 1], ["6308084", 2], ["6311113", 2],
                                             ["6339232", 2], ["4609050", 2]]))
    return rows


def load_steps(paths, cache, signature):
    if cache:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data["version"] == CACHE_VERSION and data["pdfs"] == signature:
                return data["books"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    books = [(str(p), read_book(p)) for p in paths]
    # Download order can differ from the printed book order.
    starts = [s[0]["step"] for _, s in books if s]
    if len(set(starts)) == len(books):
        books.sort(key=lambda b: b[1][0]["step"])
    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=cache.parent, delete=False, mode="w", encoding="utf-8") as tmp:
                json.dump(dict(version=CACHE_VERSION, pdfs=signature, books=books), tmp)
            Path(tmp.name).replace(cache)
        except OSError:
            pass
    return books


def recognize_step(page, row, matcher):
    from step_parts import panel_parts
    signatures = []
    codes = [] if matcher.printed_geometry and matcher.index_codes else None
    parts = panel_parts(page, row["box"], signatures=signatures, codes=codes)
    matches = [matcher.match(features, qty*row["repeat"], signature, codes[i] if codes is not None else None)
               for i, ((qty, features), signature) in enumerate(zip(parts, signatures))]
    matcher.refine_panel(parts, matches, row["repeat"])
    results = [(qty, match, sig) for (qty, _), match, sig in zip(parts, matches, signatures)]
    for i, (_, _, signature) in enumerate(results):
        sheet = row.get("sheet_drawings", {}).get(signature)
        if sheet in matcher.inventory:
            results[i] = (0, (matcher.inventory[sheet][0], "Al geteld bij folievel", 100), signature)
    for number, qty in row.get("extra_parts", []):
        entry = matcher.inventory.get(number)
        match = (entry[0], "Uit bouwtekening", 100) if entry else (None, "Onbekend", 0)
        results.append((qty, match, None))
    return results


def prepare_usage(books, matcher, cached, progress):
    total = sum(len(rows) for _, rows in books)
    if total == len(cached):
        return
    completed = len(cached)
    progress(completed, total)
    index = 0
    for path, rows in books:
        with pdf.open(path) as doc:
            for row in rows:
                item = str(index)
                index += 1
                if item not in cached:
                    cached[item] = recognize_step(doc[row["page"]-1], row, matcher)
                    completed += 1
                    if completed % 10 == 0 or completed == total:
                        progress(completed, total)


def show_steps(parent, books, matcher, review_path=None, inventory=(), usage_only=False, index_totals=None,
               prepared=None, save_complete=None, cached_catalog=(), offline_path=None, usage_callback=None,
               analysis_signature=None, knowledge_path=None):
    import sqlite3
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
    from PIL import Image, ImageTk
    from step_parts import panel_parts

    db = sqlite3.connect(review_path or ":memory:")
    db.execute("CREATE TABLE IF NOT EXISTS bevestigd (beeld TEXT, element TEXT, PRIMARY KEY(beeld, element))")
    confirmed = set(db.execute("SELECT beeld, element FROM bevestigd"))
    db.execute("""CREATE TABLE IF NOT EXISTS correcties
               (beeld TEXT, element TEXT, vervanging TEXT, PRIMARY KEY(beeld, element))""")
    corrections = {(s,e):v for s,e,v in db.execute("SELECT beeld,element,vervanging FROM correcties")}
    if knowledge_path and review_path:
        review_source = _review_source(knowledge_path.parent / "downloads", review_path)
        explanations = index_explanations(knowledge_path, review_source)
    else:
        explanations = {}

    def update_explanation(number, explanation):
        save_index_explanation(knowledge_path, review_source, number, explanation)
        explanations.clear()
        explanations.update(index_explanations(knowledge_path, review_source))
        parent.event_generate("<<IndexExplanationSaved>>")

    edit_explanation = update_explanation if knowledge_path and review_path else None
    catalog = cached_catalog or (matcher.refs if matcher is not None else ())
    records = {rec[0]:rec for rec,_,_ in catalog}
    images = ({rec[0]:path for rec,path,_ in cached_catalog} if matcher is None else matcher.images)

    entries = {}
    for book, (path, rows) in enumerate(books, 1):
        for row in rows:
            entries[str(len(entries))] = book, path, row
    matches = {}
    keys = {}
    prepared = {} if prepared is None else prepared

    def analyze(item, page=None):
        if item not in matches:
            row = entries[item][2]
            if item not in prepared:
                prepared[item] = recognize_step(page, row, matcher)
                if save_complete and len(prepared) == len(entries):
                    save_complete(prepared)
            matches[item] = [(qty, match) for qty, match, _ in prepared[item]]
            for index, (_, (record, _, _), signature) in enumerate(prepared[item]):
                keys[item,index] = (signature, record[0] if record else "") if signature else None
        return matches[item]

    def review_key(item, index):
        if (item, index) not in keys:
            return None
        key = keys[item, index]
        if key in confirmed or key in corrections:
            return key
        signature = prepared[item][index][2]
        # A rejection without a replacement applies only to the original proposal.
        previous = {candidate for candidate in confirmed if candidate[0] == signature}
        previous.update(candidate for candidate, replacement in corrections.items()
                        if candidate[0] == signature and replacement)
        if len(previous) == 1:
            candidate = next(iter(previous))
            if candidate in corrections or candidate[1] in records:
                return candidate
        return key

    def effective(item, index):
        qty, match = matches[item][index]
        key = review_key(item, index)
        if key in corrections:
            replacement = records.get(corrections[key])
            return qty, (replacement, "Gecorrigeerd" if replacement else "Afgekeurd", 0)
        if key in confirmed and key != keys[item, index]:
            return qty, (records[key[1]], "Bevestigd", 100)
        return qty, match

    def read_item(item):
        book, path, row = entries[item]
        if item in prepared:
            analyze(item)
        else:
            with pdf.open(path) as doc:
                analyze(item, doc[row["page"]-1])
        for i in range(len(matches[item])):
            qty, (record, status, _) = effective(item, i)
            if review_key(item, i) in confirmed:
                status = "Bevestigd"
            yield book, row, qty, record, status

    thumbnails = {}

    def thumbnail(record):
        path = images.get(record[0]) if record else None
        if not path:
            return ""
        if path not in thumbnails:
            try:
                with Image.open(path) as source:
                    image = source.convert("RGBA")
                image.thumbnail((62, 48), Image.Resampling.LANCZOS)
                thumbnails[path] = ImageTk.PhotoImage(image, master=parent)
            except OSError:
                return ""
        return thumbnails[path]

    def save_offline():
        if offline_path is None:
            return ""
        if save_offline_usage(offline_path, entries, read_item, index_totals, images, explanations):
            return " Offline opgeslagen."
        return " Offline opslaan vereist een volledig herkende analyse zonder openstaande verschillen."

    def current_review_signature():
        return usage_review_signature(
            analysis_signature, confirmed, ((*key, value) for key, value in corrections.items()))

    def collect_usage():
        usage = {}
        for item in entries:
            for book, row, qty, record, status in read_item(item):
                if record:
                    add_step_usage(usage, record[0], item, book, row, qty, status)
        return usage

    def store_usage(usage):
        grouped = {number: {item: [*line[:6], sorted(line[6])] for item, line in steps.items()}
                   for number, steps in usage.items()}
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS gebruiksoverzicht (id INTEGER PRIMARY KEY, versie TEXT, regels TEXT)")
            db.execute("INSERT OR REPLACE INTO gebruiksoverzicht VALUES (1,?,?)",
                       (current_review_signature(), json.dumps(grouped)))

    def publish_review():
        if analysis_signature and len(prepared) == len(entries):
            try:
                store_usage(collect_usage())
            except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
                parent.status.set(f"Bouwstappenoverzicht opslaan mislukt. {exc}")
        parent.event_generate("<<StepReviewSaved>>")

    if usage_callback:
        try:
            usage = None
            if analysis_signature:
                review_signature = current_review_signature()
                db.execute("CREATE TABLE IF NOT EXISTS gebruiksoverzicht (id INTEGER PRIMARY KEY, versie TEXT, regels TEXT)")
                saved = db.execute("SELECT regels FROM gebruiksoverzicht WHERE id=1 AND versie=?", (review_signature,)).fetchone()
                if saved:
                    try:
                        usage = {number: {item: [*line[:6], set(line[6])] for item, line in steps.items()}
                                 for number, steps in json.loads(saved[0]).items()}
                    except (ValueError, AttributeError, TypeError, IndexError):
                        pass
            rebuild = usage is None
            if rebuild:
                usage = collect_usage()
                if analysis_signature:
                    store_usage(usage)
            if offline_path and (rebuild or not offline_path.exists()):
                try:
                    save_offline()
                except OSError as exc:
                    parent.status.set(f"Offline opslaan mislukt. {exc}")
        finally:
            db.close()
        usage_callback(usage, None)
        return

    if usage_only:
        overview = show_usage(parent, entries, read_item, inventory or records.values(), thumbnail, index_totals,
                              ready=len(prepared) == len(entries), on_complete=save_offline,
                              explanations=explanations, edit_explanation=edit_explanation)
        def reload_reviews(event):
            confirmed.clear()
            confirmed.update(db.execute("SELECT beeld, element FROM bevestigd"))
            corrections.clear()
            corrections.update(((s,e),v) for s,e,v in db.execute("SELECT beeld,element,vervanging FROM correcties"))
            overview.event_generate("<<UsageChanged>>")
        listener = parent.bind("<<StepReviewSaved>>", reload_reviews, add="+")
        def close_overview(event):
            if event.widget == overview:
                parent.unbind("<<StepReviewSaved>>", listener)
                db.close()
        overview.bind("<Destroy>", close_overview, add="+")
        return overview

    win = tk.Toplevel(parent)
    win.title("Onderdelenkader per hoofdbouwstap")
    win.geometry("1150x650")
    win.rowconfigure(1, weight=1)
    win.columnconfigure(2, weight=1)
    cols = ("Boekje", "Stap", "Pagina", "Reeks")
    tree = ttk.Treeview(win, columns=cols, show="headings", selectmode="browse")
    for col in cols:
        tree.heading(col, text=col)
        tree.column(col, width=64, anchor="center", stretch=False)
    tree.grid(row=1, column=0, sticky="ns")
    scroll = ttk.Scrollbar(win, command=tree.yview)
    scroll.grid(row=1, column=1, sticky="ns")
    tree.configure(yscrollcommand=scroll.set)
    pane = ttk.Frame(win, padding=12)
    pane.grid(row=1, column=2, sticky="nsew")
    pane.columnconfigure(0, weight=1)
    pane.rowconfigure(0, weight=1)
    canvas = tk.Canvas(pane, highlightthickness=0)
    canvas.grid(sticky="nsew")
    caption = ttk.Label(pane, anchor="center", justify="center")
    caption.grid(row=1, column=0, sticky="ew", pady=8)
    fields = ("Aantal", "Met reeks", "LEGO-element", "Rebrickable", "Omschrijving", "Kleur", "Herkenning")
    table_frame = ttk.Frame(pane)
    table_frame.grid(row=2, column=0, sticky="ew")
    table_frame.columnconfigure(0, weight=1)
    ttk.Style(win).configure("Staponderdelen.Treeview", rowheight=52)
    parts = ttk.Treeview(table_frame, columns=fields, show="tree headings", height=4,
                         selectmode="browse", style="Staponderdelen.Treeview")
    parts.heading("#0", text="Afbeelding")
    parts.column("#0", width=92, minwidth=92, stretch=False, anchor="center")
    for field, width in zip(fields, (48, 66, 90, 90, 245, 108, 135)):
        parts.heading(field, text=field)
        parts.column(field, width=width, minwidth=width, stretch=field=="Omschrijving")
    parts.grid(row=0, column=0, sticky="ew")
    for orient, axis, pos, sticky in (("vertical", "y", (0,1), "ns"), ("horizontal", "x", (1,0), "ew")):
        bar = ttk.Scrollbar(table_frame, orient=orient, command=getattr(parts, axis+"view"))
        bar.grid(row=pos[0], column=pos[1], sticky=sticky)
        parts.configure(**{axis+"scrollcommand":bar.set})
    representatives = {}
    displayed = None
    previews = []
    review = tk.BooleanVar(win)
    review_scope = tk.StringVar(win, value="Onbekend of conflict")
    progress = ttk.Label(win)
    progress.grid(row=0, column=2, sticky="w")
    for item, (book, path, row) in entries.items():
        tree.insert("", "end", iid=item, values=(book, row["step"], row["page"], f'{row["repeat"]}×'))
    sort_steps = sortable_headings(tree)
    sort_parts = sortable_headings(parts, "LEGO-element")

    def doubtful(result):
        _, (_, status, score) = result
        if status in ("Eerder gecontroleerd", "Uit bouwtekening", "Al geteld bij folievel"):
            return False
        if status in ("Afgekeurd", "Gecorrigeerd"):
            return status == "Afgekeurd"
        if review_scope.get() == "Onbekend of conflict":
            return status in ("Onbekend", "Conflict met PDF")
        return status not in ("Ook in PDF-index", "Match via PDF-index") and (status != "Voorstel" or score < 65)

    def compare(event=None):
        item = tree.selection()[0] if tree.selection() else None
        key = review_key(item, int(parts.selection()[0])) if item and parts.selection() else None
        reject.configure(state="normal" if key else "disabled",
                         text="Correctie wijzigen…" if key in corrections else
                         "Onderdeel toewijzen…" if key and not key[1] else "Match foutief…")
        undo.configure(state="normal" if key in corrections else "disabled")
        color_menu.delete(0, "end")
        current = effective(item,int(parts.selection()[0]))[1][0] if key else None
        variants = sorted((r for r in records.values() if current and r[1] == current[1]
                           and r[3] and r[3] != current[3]), key=lambda r:(r[3],r[0]))
        for variant in variants:
            color_menu.add_command(label=f"{variant[3]} · {variant[0]}",
                                   command=lambda n=variant[0]: correct(replacement=n))
        color_button.configure(state="normal" if variants else "disabled")
        confirm.configure(state="normal" if current and key not in corrections else "disabled",
                          text="Bevestiging intrekken" if key in confirmed else "Bevestig onderdeel en kleur")
        if not parts.selection() or not tree.selection():
            return
        index = int(parts.selection()[0])
        item = tree.selection()[0]
        if item != displayed or index >= len(previews):
            return
        record = effective(item,index)[1][0]
        path = images.get(record[0]) if record else None
        reference = None
        if path:
            try:
                with Image.open(path) as im:
                    reference = im.convert("RGBA")
            except OSError:
                pass
        w, h = canvas.winfo_width(), canvas.winfo_height()
        canvas.delete("all")
        canvas.photos = []
        for x, picture, label in ((w/4, previews[index], "Bouwstap"), (3*w/4, reference, "Rebrickable")):
            canvas.create_text(x, 16, text=label)
            if picture is None:
                canvas.create_text(x, h/2, text="Geen afbeelding")
                continue
            scale = min(max(1,w/2-10)/picture.width, max(1,h-45)/picture.height, 4)
            image = picture.resize((max(1,round(picture.width*scale)), max(1,round(picture.height*scale))), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image, master=win)
            canvas.photos.append(photo)
            canvas.create_image(x, h/2+12, image=photo)

    def draw(event=None):
        nonlocal displayed
        if not tree.selection():
            return
        book, path, row = entries[tree.selection()[0]]
        box = pdf.Rect(row["box"]) + (-1, -1, 1, 1)
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if min(w, h) <= 2:
            return
        scale = min(4, (w-2)/box.width, (h-2)/box.height)
        canvas.delete("all")
        item = tree.selection()[0]
        if displayed != item:
            parts.delete(*parts.get_children())
            displayed = None
        try:
            with pdf.open(path) as doc:
                page = doc[row["page"]-1]
                pix = page.get_pixmap(matrix=pdf.Matrix(scale, scale), clip=box)
                results = analyze(item, page)
                if displayed != item:
                    previews.clear()
                    panel_parts(page, row["box"], previews)
                    for index, result in enumerate(results):
                        result = effective(item,index)
                        key = review_key(item, index)
                        if review.get() and (not doubtful(result) or key in confirmed or
                                             (key and representatives.get(key) != (item,index))):
                            continue
                        qty, (record, status, score) = result
                        parts.insert("", "end", iid=str(index), image=thumbnail(record), values=(qty, qty*row["repeat"],
                                     *(record or ("—", "—", "Onbekend", "—")),
                                     status if key in corrections else
                                     "Bevestigd" if key in confirmed else f"{score}% · {status}"))
                    sort_parts()
                    displayed = item
                    if review.get() and parts.get_children():
                        parts.selection_set(parts.get_children()[0])
            canvas.photo = tk.PhotoImage(data=pix.tobytes("png"))
            canvas.create_image(w/2, h/2, image=canvas.photo)
        except (OSError, RuntimeError, ValueError, IndexError) as exc:
            caption.configure(text=f"Onderdelenkader kon niet worden geopend. {exc}")
            return
        title = f'Boekje {book} · stap {row["step"]} · PDF-pagina {row["page"]}'
        details = f'Aantallen in het kader × {row["repeat"]}'
        if row["local"]:
            details += "\nLokale herhaling " + ", ".join(f"{n}×" for n in row["local"]) + " (al meegeteld in het kader)"
        if not records:
            details += "\nGeen lokale Rebrickable-afbeeldingen beschikbaar voor deze onderdelenlijst."
        elif not matches[item]:
            details += "\nGeen leesbare aantallen in dit onderdelenkader."
        caption.configure(text=title + "\n" + details)
        compare()

    scan_job = None
    def decide():
        nonlocal displayed
        if not tree.selection() or not parts.selection():
            return
        item = tree.selection()[0]
        index = int(parts.selection()[0])
        key = review_key(item, index)
        if not key or not key[1]:
            return
        remove = key in confirmed
        try:
            with db:
                if remove:
                    db.execute("DELETE FROM bevestigd WHERE beeld=? AND element=?", key)
                else:
                    db.execute("INSERT OR IGNORE INTO bevestigd VALUES (?,?)", key)
                if knowledge_path and review_path:
                    save_reviews(knowledge_path, review_source, confirmations=() if remove else [key],
                                 forget=key if remove else None)
        except sqlite3.Error as exc:
            messagebox.showerror("Controle opslaan", str(exc), parent=win)
            return
        if remove:
            confirmed.discard(key)
        else:
            confirmed.add(key)
        win.event_generate("<<UsageChanged>>")
        publish_review()
        displayed = None
        draw()
        if review.get() and not parts.get_children():
            tree.selection_remove(item)
            tree.detach(item)
            others = tree.get_children()
            if others:
                tree.selection_set(others[0])
            else:
                canvas.delete("all")
                caption.configure(text="")
            if scan_job is None:
                progress.configure(text=f"{len(others)} bouwstappen met openstaande controles")

    def correct(restore=False, replacement=None):
        nonlocal displayed
        if not tree.selection() or not parts.selection():
            return
        key = review_key(tree.selection()[0], int(parts.selection()[0]))
        if not key:
            return
        if replacement == key[1]:
            restore = True
        if not restore and replacement is None:
            prompt = (f"Voorgesteld LEGO-element {key[1]}.\nVul het juiste LEGO-elementnummer uit de set in, "
                      "of laat het veld leeg om alleen af te keuren." if key[1]
                      else "Vul het LEGO-elementnummer van het getoonde onderdeel in.")
            replacement = simpledialog.askstring("Onderdeel corrigeren", prompt, parent=win,
                                                 initialvalue=corrections.get(key,""))
            if replacement is None:
                return
            replacement = replacement.strip()
            if replacement and (replacement not in records or replacement == key[1]):
                messagebox.showerror("Correctie", "Kies een ander LEGO-elementnummer uit de herkende setonderdelen.", parent=win)
                return
        try:
            with db:
                if restore:
                    db.execute("DELETE FROM correcties WHERE beeld=? AND element=?", key)
                else:
                    db.execute("INSERT OR REPLACE INTO correcties VALUES (?,?,?)", (*key,replacement))
                    db.execute("DELETE FROM bevestigd WHERE beeld=? AND element=?", key)
                if knowledge_path and review_path:
                    save_reviews(knowledge_path, review_source,
                                 corrections=() if restore else [(*key, replacement)],
                                 forget=key if restore else None)
        except sqlite3.Error as exc:
            messagebox.showerror("Correctie opslaan", str(exc), parent=win)
            return
        if restore:
            corrections.pop(key,None)
        else:
            corrections[key] = replacement
            confirmed.discard(key)
        win.event_generate("<<UsageChanged>>")
        publish_review()
        displayed = None
        if review.get():
            toggle()
        else:
            draw()

    actions = ttk.Frame(pane)
    actions.grid(row=3, column=0, sticky="e", pady=4)
    undo = ttk.Button(actions, text="Correctie intrekken", command=lambda: correct(True), state="disabled")
    reject = ttk.Button(actions, text="Match foutief…", command=correct, state="disabled")
    color_button = ttk.Menubutton(actions, text="Kleur wijzigen", state="disabled")
    color_menu = tk.Menu(color_button, tearoff=False)
    color_button.configure(menu=color_menu)
    confirm = ttk.Button(actions, text="Bevestig onderdeel en kleur", command=decide, state="disabled")
    for button in (undo, reject, color_button, confirm):
        button.pack(side="left", padx=2)

    usage_window = None
    def open_usage():
        nonlocal usage_window
        if usage_window is not None and usage_window.winfo_exists():
            usage_window.lift()
            return
        usage_window = show_usage(win, entries, read_item, inventory or records.values(), thumbnail, index_totals,
                                  on_complete=save_offline, explanations=explanations, edit_explanation=edit_explanation)

    ttk.Button(actions, text="Bouwstappen per onderdeel…", command=open_usage).pack(side="left", padx=2)
    def toggle():
        nonlocal displayed, scan_job
        if scan_job is not None:
            win.after_cancel(scan_job)
            scan_job = None
        displayed = None
        representatives.clear()
        tree.selection_remove(*tree.selection())
        parts.delete(*parts.get_children())
        canvas.delete("all")
        caption.configure(text="")
        for item in entries:
            tree.detach(item)
        if not review.get():
            for item in entries:
                tree.move(item, "", "end")
            sort_steps()
            tree.selection_set(tree.get_children()[0])
            progress.configure(text="")
            return
        pending = iter(entries)
        checked = 0
        def scan():
            nonlocal scan_job, checked
            scan_job = None
            item = next(pending, None)
            if item is None:
                sort_steps()
                progress.configure(text=f"{len(tree.get_children())} bouwstappen te controleren")
                return
            _, path, row = entries[item]
            try:
                if item not in matches:
                    with pdf.open(path) as doc:
                        analyze(item, doc[row["page"]-1])
            except (OSError, RuntimeError, ValueError, IndexError) as exc:
                progress.configure(text=f"Scannen onderbroken op pagina {row['page']}. {exc}")
                return
            show = not matches[item]
            for idx,result in enumerate(matches[item]):
                result = effective(item,idx)
                key = review_key(item, idx)
                if doubtful(result) and key not in confirmed and (not key or key not in representatives):
                    if key:
                        representatives[key] = (item,idx)
                    show = True
            if show:
                tree.move(item, "", "end")
                if not tree.selection():
                    tree.selection_set(item)
            checked += 1
            progress.configure(text=f"Bouwstappen gecontroleerd  {checked} / {len(entries)}")
            scan_job = win.after(1, scan)
        scan()

    filters = ttk.Frame(win)
    filters.grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Checkbutton(filters, text="Controlelijst", variable=review, command=toggle).pack(side="left")
    scope = ttk.Combobox(filters, width=21, state="readonly", textvariable=review_scope,
                         values=("Onbekend of conflict", "Alle twijfelgevallen"))
    scope.pack(side="left")
    def scope_changed(event):
        review.set(True)
        toggle()
    scope.bind("<<ComboboxSelected>>", scope_changed)
    def closing(event):
        if event.widget == win:
            if scan_job is not None:
                win.after_cancel(scan_job)
            db.close()
    win.bind("<Destroy>", closing)
    parts.bind("<<TreeviewSelect>>", compare)

    tree.bind("<<TreeviewSelect>>", draw)
    canvas.bind("<Configure>", draw)
    first = next(iter(entries))
    tree.selection_set(first)
    tree.focus(first)
    tree.focus_set()


def add_step_usage(usage, number, item, book, row, qty, status):
    steps = usage.setdefault(number, {})
    line = steps.setdefault(item, [book, row["step"], row["page"], 0, row["repeat"], 0, set()])
    line[3] += qty
    line[5] += qty * row["repeat"]
    line[6].add(status)


def step_usage_values(steps):
    for book, step, page, qty, repeat, total, statuses in sorted(steps.values(), key=lambda line: line[:3]):
        yield book, step, page, qty, f"{repeat}×", total, ", ".join(sorted(statuses))


def show_usage(parent, entries, read_item, inventory, thumbnail, index_totals=None, ready=False, on_complete=None,
               explanations=None, edit_explanation=None):
    """Index the effective matches from the step viewer, including user corrections."""
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk

    win = tk.Toplevel(parent)
    win.title("Bouwstappen per onderdeel")
    win.geometry("1040x700")
    win.columnconfigure(0, weight=1)
    win.rowconfigure(2, weight=2)
    win.rowconfigure(4, weight=1)
    status = tk.StringVar(win)
    explanations = {} if explanations is None else explanations
    ttk.Label(win, textvariable=status).grid(row=0, column=0, sticky="w", padx=10, pady=6)
    searchbar = ttk.Frame(win)
    searchbar.grid(row=1, column=0, sticky="ew", padx=10)
    ttk.Label(searchbar, text="Zoek op nummer, omschrijving of kleur").pack(side="left")
    query = tk.StringVar(win)
    search = ttk.Entry(searchbar, textvariable=query)
    search.pack(side="left", fill="x", expand=True, padx=8)

    def table(row, columns):
        frame = ttk.Frame(win)
        frame.grid(row=row, column=0, sticky="nsew", padx=10, pady=4)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        view = ttk.Treeview(frame, columns=[c for c,_ in columns], show="headings", selectmode="browse")
        view.grid(row=0, column=0, sticky="nsew")
        for field, width in columns:
            view.heading(field, text=field)
            view.column(field, width=width, minwidth=min(55,width))
        bar = ttk.Scrollbar(frame, command=view.yview)
        bar.grid(row=0, column=1, sticky="ns")
        view.configure(yscrollcommand=bar.set)
        return view

    listing = table(2, (("LEGO-element",95),("Rebrickable",85),("Omschrijving",225),
                        ("Kleur",115),("Bouwstappen",95),("Totaal in stappen",125),
                        ("Hoofdindex",90),("Verschil",75)))
    index_totals = index_totals or {}
    listing.configure(show="tree headings", style="Gebruik.Treeview")
    ttk.Style(win).configure("Gebruik.Treeview", rowheight=52)
    listing.heading("#0", text="Afbeelding")
    listing.column("#0", width=82, minwidth=70, stretch=False)
    sort_listing = sortable_headings(listing, "LEGO-element")
    detail_title = tk.StringVar(win, value="Gebruik per bouwstap")
    ttk.Label(win, textvariable=detail_title, wraplength=850).grid(row=3, column=0, sticky="w", padx=10)
    explain_button = None
    if edit_explanation:
        explain_button = ttk.Button(searchbar, text="Verschil verklaren…", command=lambda: explain())
        explain_button.pack(side="right")
    details = table(4, (("Boekje",55),("Stap",55),("PDF-pagina",80),("In stap",65),
                        ("Reeks",55),("Met reeks",90),("Herkenning",270)))
    sort_details = sortable_headings(details)
    initial_labels = {rec[0]:tuple(rec) for rec in inventory}
    labels = initial_labels.copy()
    usage = {}
    missing = "?"
    scanned = []
    pending = iter(entries)
    job = None

    def collect(item):
        for book, row, qty, record, match_status in read_item(item):
            number = record[0] if record else missing
            if record:
                labels[number] = record
            else:
                labels[missing] = ("—", "", "Nog niet gekoppeld", "")
            add_step_usage(usage, number, item, book, row, qty, match_status)

    def select(event=None):
        details.delete(*details.get_children())
        if not listing.selection():
            detail_title.set("Gebruik per bouwstap")
            if explain_button:
                explain_button.configure(state="disabled")
            return
        number = listing.selection()[0]
        steps = usage.get(number, {})
        expected = index_totals.get(number)
        found = sum(v[5] for v in steps.values())
        accepted = explained_difference(explanations, number, expected, found)
        caption = f"{labels[number][2]} · {len(steps)} bouwstappen"
        if accepted:
            caption += f"\nVerklaard verschil ({found - expected:+d}) · {explanations[number][2]}"
        detail_title.set(caption)
        if explain_button:
            explain_button.configure(text="Verklaring intrekken" if accepted else "Verschil verklaren…",
                                     state="normal" if expected is not None and found != expected
                                     and len(scanned) == len(entries) else "disabled")
        for values in step_usage_values(steps):
            details.insert("", "end", values=values)

        sort_details()

    def explain():
        number = listing.selection()[0]
        expected = index_totals[number]
        found = sum(line[5] for line in usage.get(number, {}).values())
        if explained_difference(explanations, number, expected, found):
            explanation = None
        else:
            reason = simpledialog.askstring("Verschil verklaren",
                                            f"LEGO-element {number} heeft {expected} stuks in de hoofdindex "
                                            f"en {found} in de bouwstappen. Waarom klopt deze afwijking?",
                                            parent=win)
            if not reason or not reason.strip():
                return
            explanation = (expected, found, reason.strip())
        try:
            edit_explanation(number, explanation)
        except sqlite3.Error as exc:
            messagebox.showerror("Verklaring opslaan mislukt", str(exc), parent=win)
            return
        complete()
        refresh()

    def refresh(*_):
        previous = listing.selection()
        listing.delete(*listing.get_children())
        words = query.get().casefold().split()
        for number, rec in sorted(labels.items(), key=lambda kv:(kv[1][2],kv[1][3],kv[0])):
            if not all(word in " ".join(rec).casefold() for word in words):
                continue
            steps = usage.get(number, {})
            found = sum(v[5] for v in steps.values())
            expected = index_totals.get(number)
            difference = "—"
            if expected is not None and len(scanned) == len(entries):
                difference = ("Verklaard" if explained_difference(explanations, number, expected, found)
                              else found - expected)
            listing.insert("", "end", iid=number, image=thumbnail(rec),
                           values=(*rec, len(steps), found, expected if expected is not None else "—", difference))
        sort_listing()
        visible = listing.get_children()
        if visible:
            chosen = previous[0] if previous and listing.exists(previous[0]) else visible[0]
            listing.selection_set(chosen)
            listing.see(chosen)
        select()

    def complete():
        message = f"{len(scanned)} bouwstappen verwerkt. Verschil = totaal in stappen (inclusief reeksherhaling) min LEGO-hoofdindex."
        if on_complete:
            try:
                message += on_complete()
            except OSError as exc:
                message += f" Offline opslaan mislukt. {exc}"
        status.set(message)

    def updated(event):
        usage.clear()
        labels.clear()
        labels.update(initial_labels)
        for item in scanned:
            collect(item)
        if len(scanned) == len(entries):
            complete()
        refresh()

    def scan():
        nonlocal job
        job = None
        item = next(pending, None)
        if item is None:
            complete()
            refresh()
            return
        try:
            collect(item)
        except (OSError, RuntimeError, ValueError, IndexError) as exc:
            status.set(f"Overzicht onvolledig na {len(scanned)} bouwstappen. {exc}")
            refresh()
            return
        scanned.append(item)
        status.set(f"Overzicht wordt opgebouwd · {len(scanned)} van {len(entries)} bouwstappen")
        if len(scanned) == 1 or len(scanned) % 25 == 0:
            refresh()
        job = win.after(1, scan)

    binding = parent.bind("<<UsageChanged>>", updated, add="+")
    win.bind("<<UsageChanged>>", updated)
    def close(event):
        if event.widget == win:
            if job is not None:
                win.after_cancel(job)
            parent.unbind("<<UsageChanged>>", binding)
    win.bind("<Destroy>", close)
    listing.bind("<<TreeviewSelect>>", select)
    query.trace_add("write", refresh)
    search.focus_set()
    if ready:
        for item in pending:
            collect(item)
            scanned.append(item)
        scan()
    else:
        job = win.after_idle(scan)
    return win
