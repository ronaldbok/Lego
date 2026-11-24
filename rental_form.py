"""A4 rental form, based on the supplied SuperBouwer card."""
from pathlib import Path
from math import ceil

import pymupdf as pdf

ASSETS = Path(__file__).with_name("formulier_assets")
ROWS_WITH_REMARKS = 17
ROWS_WITHOUT_REMARKS = 23
ROW_HEIGHT = 4.6
FORM_VERSION = "Onderdelenformulier v3"
SURPLUS_NOTE = "Het teveel aan onderdelen zit in het reserve zakje."
RETURN_PORTAL_URL = "https://superbouwer.shipping-portal.com/rp/"
MM = 72 / 25.4
A4_WIDTH = 210 * MM
A4_HEIGHT = 297 * MM
LAYOUT_SCALE = A4_HEIGHT / (210 * MM)
LAYOUT_MM = MM * LAYOUT_SCALE
LAYOUT_LEFT = (A4_WIDTH - 148 * LAYOUT_MM) / 2


def _form_rect(x, y, w, h):
    return pdf.Rect(LAYOUT_LEFT + x * LAYOUT_MM, y * LAYOUT_MM,
                    LAYOUT_LEFT + (x + w) * LAYOUT_MM, (y + h) * LAYOUT_MM)


def is_current_form(path, *, box_number=""):
    try:
        with pdf.open(path) as doc:
            return (bool(len(doc)) and doc.metadata.get("keywords") == FORM_VERSION
                    and all(abs(page.rect.width - A4_WIDTH) < 1
                            and abs(page.rect.height - A4_HEIGHT) < 1 for page in doc)
                    and (not box_number or any(widget.field_name == "p1_Boxnummer"
                                              and widget.field_value == box_number
                                              for widget in doc[0].widgets())))
    except (OSError, ValueError, RuntimeError):
        return False


def _return_page(doc):
    page = doc.new_page(width=A4_WIDTH, height=A4_HEIGHT)

    def caption(x, y, w, h, text, size, *, bold=False, light=False):
        page.insert_textbox(_form_rect(x,y,w,h), text, fontsize=size * LAYOUT_SCALE, align=1,
                            fontname="hebo" if bold else "helv",
                            color=(1,1,1) if light else (0,0,0))

    def picture(name, x, y, w, h):
        page.insert_image(_form_rect(x,y,w,h), filename=str(ASSETS / name))

    picture("sb.png",64,12,20,13)
    caption(54,25,40,5,"S U P E R B O U W E R",5,bold=True)
    caption(8,37,132,15,"SuperBouwerBox retourneren",19,bold=True)
    caption(9,53,130,9,"Scan onderstaande QR en start je retourzending:",11,bold=True)
    picture("retour_qr.png",58,62,32,32)
    page.insert_link({"kind": pdf.LINK_URI, "from": _form_rect(58,62,32,32),
                      "uri": RETURN_PORTAL_URL})
    caption(9,95,130,11,"Retourprocedure:",12,bold=True)
    panels = (
        ("Bouwset demonteren", "Demonteer de bouwset voorzichtig en sorteer alle onderdelen weer op kleur volgens de kleurenkaart.\n\nTip: gebruik de meegeleverde tool."),
        ("Box inpakken", "Sluit alle zakjes goed af en stop ze samen met de handleiding terug in de SuperBouwerBox."),
        ("Tiewraps bevestigen", "Sluit de SuperBouwerBox af met 2 tiewraps. Bevestig deze zoals op onderstaande foto's weergegeven. Controleer of de drukknoppen goed vast zitten."),
    )
    for i, (title, body) in enumerate(panels):
        x = 10 + 43*i
        page.draw_rect(_form_rect(x,106,42,80), color=None, fill=(.07,)*3)
        caption(x+1,109,40,8,title,9,bold=True,light=True)
        caption(x+2,119,38,30,body,8,light=True)
    picture("retour_demontage.png",14,149,34,32)
    picture("retour_box.png",57,148,34,34)
    for y, title, name in ((147,"Grote SuperBouwerBox","groot"),(166,"Kleine SuperBouwerBox","klein")):
        caption(97,y,40,5,title,7,bold=True,light=True)
        picture(f"retour_{name}.png",101,y+4,32,14)


def rental_form(destination, set_number="", inventory=(), deltas=None, *,
                box_number="", customer_name="", form_date="", remarks=""):
    colors = {n: color for n, _, color, _ in inventory}
    rows = []
    for number, delta in sorted((deltas or {}).items()):
        color = colors.get(number, "")
        if delta:
            rows.append((number, color, str(-delta) if delta < 0 else "",
                         str(delta) if delta > 0 else ""))
    remarks = "\n".join(text for text in (
        SURPLUS_NOTE if any(row[3] for row in rows) else "", remarks.strip()
    ) if text)
    last_capacity = ROWS_WITH_REMARKS if remarks else ROWS_WITHOUT_REMARKS
    pages = 1 + ceil(max(0, len(rows) - last_capacity) / ROWS_WITHOUT_REMARKS)
    offset = 0
    with pdf.open() as doc:
        doc.set_metadata({"title": "Onderdelenformulier", "keywords": FORM_VERSION})
        for idx in range(pages):
            page = doc.new_page(width=A4_WIDTH, height=A4_HEIGHT)
            last_page = idx == pages - 1
            capacity = last_capacity if last_page else ROWS_WITHOUT_REMARKS
            remaining = len(rows) - offset
            used = min(capacity, remaining if last_page else remaining - 1)
            page_rows = rows[offset:offset + used]
            offset += used

            def outline(x, y, w, h):
                page.draw_rect(_form_rect(x,y,w,h), color=(.32,)*3, width=.45 * LAYOUT_SCALE)

            def label(x, y, w, text, size=9, bold=False, center=False):
                page.insert_textbox(_form_rect(x,y,w,10), text, fontsize=size * LAYOUT_SCALE,
                                    fontname="hebo" if bold else "helv", align=1 if center else 0)

            def field(name, x, y, w, h, value="", multiline=False):
                widget = pdf.Widget()
                widget.field_type = pdf.PDF_WIDGET_TYPE_TEXT
                widget.field_name = f"p{idx+1}_{name}"
                widget.field_label = name
                widget.rect = _form_rect(x+.7,y+.35,w-1.4,h-.7)
                widget.text_fontsize = 8.5 * LAYOUT_SCALE
                widget.field_value = value
                if multiline:
                    widget.field_flags = pdf.PDF_TX_FIELD_IS_MULTILINE
                page.add_widget(widget)

            page.insert_image(_form_rect(64,10,20,13), filename=str(ASSETS/"sb.png"))
            label(54,23,40,"S U P E R B O U W E R",5,True,True)
            label(9,29,130,"Onderdelenformulier",14,True,True)
            for i, (name,value) in enumerate((("Boxnummer",box_number),("Set ID",set_number),
                                              ("Naam klant",customer_name),("Datum",form_date))):
                y = 39+6.2*i
                outline(13,y,35,6.2)
                outline(48,y,87,6.2)
                label(15,y+1,32,name+":")
                field(name,48,y,87,6.2,value)
            headers = ("Nummer van het onderdeel\n(achterin de handleiding)", "Kleur", "Missend", "Teveel")
            bounds = (13,59,97,116,135)
            for col, title in enumerate(headers):
                x = bounds[col]
                width = bounds[col+1]-x
                outline(x,71,width,10)
                label(x+1,72,width-2,title,8.2,True,True)
                for j in range(capacity):
                    y = 81 + j * ROW_HEIGHT
                    outline(x, y, width, ROW_HEIGHT)
                    field(f"{title.splitlines()[0]} {j+1}", x, y, width, ROW_HEIGHT,
                          page_rows[j][col] if j < len(page_rows) else "")
            if last_page and remarks:
                outline(13,162,122,5.5)
                label(15,162.6,110,"Opmerkingen")
                outline(13,167.5,122,22)
                field("Opmerkingen",13,167.5,122,22,remarks,True)
            if pages>1:
                label(115,192,20,f"{idx+1} / {pages}",8,False,True)
        _return_page(doc)
        # Finalize all pages before replacing a previously exported form.
        from tempfile import NamedTemporaryFile
        target = Path(destination)
        with NamedTemporaryFile(dir=target.parent, suffix=".pdf", delete=False) as tmp:
            temporary = Path(tmp.name)
        try:
            doc.save(temporary, deflate=True, garbage=4)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
