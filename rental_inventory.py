"""Per-set rental status and physical inventory differences."""
import queue
import sqlite3
import threading
import webbrowser
import tkinter as tk
from datetime import date
from contextlib import closing
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from tree_sort import sortable_headings
from owned_sets import connect_collection
from rental_print import print_rental_pdf

_printer_busy = threading.Lock()


def start_form_print(parent, path):
    if not _printer_busy.acquire(blocking=False):
        messagebox.showinfo("Onderdelenformulier afdrukken", "Er wordt al een afdrukopdracht verwerkt.", parent=parent)
        return
    result = queue.Queue()

    def send():
        try:
            result.put((print_rental_pdf(path), None))
        except (OSError, ValueError, RuntimeError) as exc:
            result.put((None, str(exc)))
        finally:
            _printer_busy.release()

    def report():
        if not parent.winfo_exists():
            return
        try:
            printer, error = result.get_nowait()
        except queue.Empty:
            parent.after(150, report)
            return
        if error:
            messagebox.showerror("Onderdelenformulier afdrukken", error, parent=parent)
        else:
            messagebox.showinfo("Onderdelenformulier afdrukken",
                                f"Afdrukopdracht verzonden naar {printer}." if printer else "Afdrukopdracht verzonden.",
                                parent=parent)

    threading.Thread(target=send, daemon=True).start()
    parent.after(150, report)


def add_rental_to_history(db, set_number, schema="main"):
    table = "huurgeschiedenis.eigen_sets" if schema == "huurgeschiedenis" else "eigen_sets"
    db.execute(f"""INSERT OR IGNORE INTO {table}
                   (setnummer, naam, eigenaar, eigenaar_sleutel, aantal) VALUES (?, '', 'Superbouwers', 'superbouwers', 1)""",
               (set_number.upper(),))


def initialize_rental_history(path, downloads):
    with closing(connect_collection(path)) as history, history:
        if history.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        for rental_path in downloads.glob("*/huurregistratie.sqlite3"):
            with closing(sqlite3.connect(rental_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                if source.execute("SELECT 1 FROM sqlite_master WHERE name='huur'").fetchone():
                    if source.execute("SELECT 1 FROM huur WHERE id=1 AND gehuurd=1").fetchone():
                        add_rental_to_history(history, rental_path.parent.name)
        history.execute("PRAGMA user_version = 1")


def open_return_portal(parent, postcode=""):
    from rental_form import RETURN_PORTAL_URL

    def open_manually():
        try:
            opened = webbrowser.open(RETURN_PORTAL_URL, new=2)
        except (OSError, webbrowser.Error):
            opened = False
        if not opened:
            messagebox.showerror("Retourportaal openen",
                                 f"Open het retourportaal in je browser via\n{RETURN_PORTAL_URL}", parent=parent)

    if not postcode:
        open_manually()
        return

    from rental_return import open_prefilled_portal

    result = queue.Queue()

    def report():
        if not parent.winfo_exists():
            return
        try:
            failure = result.get_nowait()
        except queue.Empty:
            parent.after(150, report)
            return
        if failure:
            open_manually()
            messagebox.showinfo("Retourportaal", f"{failure} Vul de postcode in je browser in.", parent=parent)

    threading.Thread(target=open_prefilled_portal, args=(postcode, result.put), daemon=True).start()
    parent.after(150, report)


def form_action(parent, set_number, inventory, path, customer_name, *, box_number="", print_form=False):
    from rental_form import is_current_form, rental_form

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS afwijkingen (element TEXT PRIMARY KEY, verschil INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS formulieren (id INTEGER PRIMARY KEY CHECK(id=1), bestand TEXT NOT NULL)")
            previous = db.execute("SELECT bestand FROM formulieren WHERE id=1").fetchone()
            deltas = dict(db.execute("SELECT element, verschil FROM afwijkingen"))
        target = Path(previous[0]) if print_form and previous else None
        if target is None or not is_current_form(target, box_number=box_number):
            box = box_number
            question = "Wat is het boxnummer van de set?"
            while not box:
                answer = simpledialog.askstring("Onderdelenformulier", question, parent=parent)
                if answer is None:
                    return
                box = answer.strip()
                question = "Vul het boxnummer in om verder te gaan."
            remarks = simpledialog.askstring("Onderdelenformulier", "Welke opmerkingen wil je opnemen? (optioneel)", parent=parent)
            if remarks is None:
                return
            filename = filedialog.asksaveasfilename(parent=parent, title="Onderdelenformulier opslaan",
                        initialdir=path.parent, initialfile=f"Onderdelenformulier {set_number}.pdf",
                        defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
            if not filename:
                return
            target = Path(filename).resolve()
            rental_form(target, set_number, inventory, deltas, box_number=box.strip(),
                        customer_name=customer_name, form_date=date.today().strftime("%d-%m-%Y"),
                        remarks=remarks.strip())
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT OR REPLACE INTO formulieren VALUES (1,?)", (str(target),))
        if print_form:
            start_form_print(parent, target)
        else:
            messagebox.showinfo("Onderdelenformulier opgeslagen", str(target), parent=parent)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        messagebox.showerror("Onderdelenformulier", str(exc), parent=parent)


def show_rental(parent, set_number, inventory, path, start=None, *, history_path=None, initially_rented=False):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS huur (id INTEGER PRIMARY KEY CHECK(id=1), gehuurd INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS afwijkingen (element TEXT PRIMARY KEY, verschil INTEGER NOT NULL)")
        if initially_rented:
            db.execute("INSERT OR IGNORE INTO huur VALUES (1,1)")
        state = db.execute("SELECT gehuurd FROM huur WHERE id=1").fetchone()
        deltas = dict(db.execute("SELECT element, verschil FROM afwijkingen"))
    win = tk.Toplevel(parent)
    win.title(f"Huurregistratie Superbouwers · set {set_number}")
    win.geometry("940x500")
    win.transient(parent)
    win.grab_set()
    win.columnconfigure(0, weight=1)
    win.rowconfigure(2, weight=1)
    rented = tk.BooleanVar(win, value=bool(state and state[0]))
    search = tk.StringVar(win)
    only_differences = tk.BooleanVar(win)
    direction = tk.StringVar(win, value="Te weinig")
    amount = tk.StringVar(win, value="0")
    feedback = tk.StringVar(win)
    totals = {n:qty for n, _, _, qty in inventory}

    def save(sql, params):
        try:
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(sql, params)
            return True
        except sqlite3.Error as exc:
            messagebox.showerror("Opslaan mislukt", str(exc), parent=win)
            return False

    def toggle_rental():
        mark_rented = rented.get()
        try:
            with closing(sqlite3.connect(path)) as db, db:
                if mark_rented and history_path is not None:
                    with closing(connect_collection(history_path)):
                        pass
                    db.execute("ATTACH DATABASE ? AS huurgeschiedenis", (str(history_path),))
                    add_rental_to_history(db, set_number, "huurgeschiedenis")
                db.execute("INSERT OR REPLACE INTO huur VALUES (1,?)", (int(mark_rented),))
        except sqlite3.Error as exc:
            rented.set(not rented.get())
            messagebox.showerror("Huurregistratie opslaan mislukt", str(exc), parent=win)
        else:
            if mark_rented and history_path is not None:
                parent.event_generate("<<RentalHistoryChanged>>")
        feedback.set("")
        select()

    header = ttk.Frame(win)
    header.grid(row=0, column=0, sticky="ew", padx=12, pady=8)
    ttk.Checkbutton(header, text="Deze set is gehuurd bij Superbouwers.nl", variable=rented,
                    command=toggle_rental).pack(side="left")
    bar = ttk.Frame(win)
    bar.grid(row=1, column=0, sticky="ew", padx=12)
    ttk.Label(bar, text="Zoek op LEGO-elementnummer, omschrijving of kleur").pack(side="left")
    ttk.Entry(bar, textvariable=search).pack(side="left", fill="x", expand=True, padx=8)
    ttk.Checkbutton(bar, text="Alleen afwijkingen", variable=only_differences, command=lambda: refresh()).pack(side="right")
    frame = ttk.Frame(win)
    frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=6)
    cols = ("LEGO-element", "Omschrijving", "Kleur", "Hoofdindex", "Afwijking", "Volgens registratie")
    table = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
    for col, width in zip(cols, (100,280,120,90,80,135)):
        table.heading(col, text=col)
        table.column(col, width=width, minwidth=50)
    scroll = ttk.Scrollbar(frame, command=table.yview)
    scroll.pack(side="right", fill="y")
    table.configure(yscrollcommand=scroll.set)
    table.pack(fill="both", expand=True)
    sort = sortable_headings(table)

    editor = ttk.Frame(win)
    editor.grid(row=3, column=0, sticky="w", padx=12, pady=5)
    kind = ttk.Combobox(editor, values=("Te weinig", "Te veel"), textvariable=direction, width=10, state="readonly")
    kind.pack(side="left")
    entry = ttk.Entry(editor, textvariable=amount, width=8)
    entry.pack(side="left", padx=5)
    ttk.Label(editor, text="stuks").pack(side="left")

    def apply():
        selected = table.selection()
        if not selected or not rented.get():
            return
        number = selected[0]
        try:
            count = int(amount.get())
            if not 0 <= count <= 2**63-1:
                raise ValueError()
        except ValueError:
            feedback.set("Vul een geheel aantal van nul of meer in.")
            return
        delta = -count if direction.get() == "Te weinig" else count
        if totals[number] + delta < 0:
            feedback.set(f"Er kunnen maximaal {totals[number]} stuks ontbreken.")
            return
        if delta:
            if not save("INSERT OR REPLACE INTO afwijkingen VALUES (?,?)", (number, delta)):
                return
            deltas[number] = delta
        else:
            if not save("DELETE FROM afwijkingen WHERE element=?", (number,)):
                return
            deltas.pop(number, None)
        refresh(number)
        feedback.set(f"Element {number} opgeslagen. Volgens registratie zijn er {totals[number]+delta} stuks.")

    button = ttk.Button(editor, text="Afwijking opslaan", command=apply)
    button.pack(side="left", padx=8)
    entry.bind("<Return>", lambda _:apply())
    ttk.Label(win, textvariable=feedback).grid(row=4, column=0, sticky="w", padx=12, pady=(0,8))

    def select(event=None):
        selected = table.selection()
        enabled = bool(selected and rented.get())
        kind.configure(state="readonly" if enabled else "disabled")
        for widget in (entry, button):
            widget.configure(state="normal" if enabled else "disabled")
        delta = deltas.get(selected[0],0) if selected else 0
        direction.set("Te veel" if delta > 0 else "Te weinig")
        amount.set(str(abs(delta)))

    def refresh(preferred=None):
        previous = table.selection()
        table.delete(*table.get_children())
        words = search.get().casefold().split()
        for n, name, color, qty in inventory:
            delta = deltas.get(n,0)
            if only_differences.get() and not delta:
                continue
            if all(w in f"{n} {name} {color}".casefold() for w in words):
                table.insert("", "end", iid=n, values=(n,name,color,qty,f"{delta:+d}" if delta else "0",qty+delta))
        sort()
        visible = table.get_children()
        if visible:
            n = preferred or (previous[0] if previous else None)
            n = n if n in visible else visible[0]
            table.selection_set(n)
            table.see(n)
        select()

    table.bind("<<TreeviewSelect>>", select)
    search.trace_add("write", lambda *_: refresh())
    refresh(start)
    return win
