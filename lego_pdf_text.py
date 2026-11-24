"""Read quantity labels from manuals with coincident PDF text layers."""

import re


def quantity_words(page, clip=None, words=None, layout=None):
    if words is None:
        words = page.get_text("words", clip=clip)
    pattern = r"[1-9]\d*[x×]"
    quantities = {}
    for x, y, right, bottom, text, *_ in words:
        if re.fullmatch(pattern, text, re.I):
            quantities[round(x, 1), round(y, 1)] = (x, y, right, bottom, text)
            continue
        if not re.fullmatch(r"[\dx×]+", text, re.I) or not any(c.isdigit() for c in text):
            continue
        if layout is None:
            layout = page.get_text("rawdict", clip=clip, flags=0)
        glyphs = {}
        for block in layout["blocks"]:
            for line in block.get("lines", ()):
                for span in line["spans"]:
                    for char in span["chars"]:
                        cx, cy = char["origin"]
                        if x - .25 <= cx < right and y < cy <= bottom + .25:
                            glyphs[round(cx, 1), round(cy, 1)] = char["c"]
        repaired = "".join(glyphs[origin] for origin in sorted(glyphs))
        if re.fullmatch(pattern, repaired, re.I):
            quantities[round(x, 1), round(y, 1)] = (x, y, right, bottom, repaired)
    unique = []
    for label in quantities.values():
        x, y, right, _, text = label
        if any(text.lower() == other[4].lower() and abs(y - other[1]) < .5
               and min(right, other[2]) - max(x, other[0]) >= .6 * min(right - x, other[2] - other[0])
               for other in unique):
            continue
        unique.append(label)
    return unique


def index_part_labels(page):
    """Pair each element number with the quantity directly above it."""
    words = list(dict.fromkeys(tuple(word[:5]) for word in page.get_text("words")))
    quantities = quantity_words(page, words=words)
    entries = {}
    for x, y, right, bottom, number in words:
        if not re.fullmatch(r"[0-9]{6,7}[a-z]?", number, re.I):
            continue
        anchors = [(qx, qy, text) for qx, qy, _, qb, text in quantities
                   if abs(x - qx) < 1.5 and abs(y - qb) < 1.75
                   and abs((bottom - y) - (qb - qy)) < 1.2]
        if len(anchors) == 1:
            qx, qy, text = anchors[0]
            if len(number) == 6 and number.isdigit():
                # A second text layer can split off the final digit and
                # repeat the previous one at the same position.
                tails = {suffix[-1] for tx, ty, tr, tb, suffix in words
                         if len(suffix) == 2 and suffix.isdigit() and suffix[0] == number[-1]
                         and abs(ty - y) < .2 and abs(tb - bottom) < .2
                         and right - 4 < tx < right - 1 and 2 < tr - right < 5}
                if len(tails) == 1:
                    number += tails.pop()
            entries[round(x, 1), round(y, 1), number.lower()] = (
                number.lower(), int(text[:-1]), qx, qy)
    return list(entries.values())
