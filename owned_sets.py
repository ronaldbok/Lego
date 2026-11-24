"""Local collection of privately owned LEGO sets."""

import re
import sqlite3
import threading
import tkinter as tk
from contextlib import closing
from tkinter import messagebox, ttk

from lego_set_metadata import fetch_set_details
from tree_sort import sortable_headings


def connect_collection(path, *, rental=False):
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS eigen_sets (
            id INTEGER PRIMARY KEY, setnummer TEXT NOT NULL, naam TEXT NOT NULL,
            eigenaar TEXT NOT NULL, eigenaar_sleutel TEXT NOT NULL,
            aantal INTEGER NOT NULL CHECK(aantal > 0), UNIQUE(setnummer, eigenaar_sleutel))""")
        db.execute("""CREATE TABLE IF NOT EXISTS setgegevens (
            setnummer TEXT PRIMARY KEY, setnaam TEXT NOT NULL, onderdelen INTEGER, thema TEXT NOT NULL,
            leeftijd TEXT NOT NULL)""")
        if rental:
            db.execute("""CREATE TABLE IF NOT EXISTS huurgegevens (
                registratie_id INTEGER PRIMARY KEY REFERENCES eigen_sets(id) ON DELETE CASCADE,
                boxnummer TEXT NOT NULL, ordernummer TEXT NOT NULL)""")
    return db


def save_owned_set(db, item, number, owner, quantity, *, rental_numbers=None):
    number, owner = number.strip().upper(), owner.strip()
    if not re.fullmatch(r"[A-Z0-9]+(?:-[0-9]+)?", number) or not any(c.isdigit() for c in number):
        raise ValueError("Vul een geldig LEGO-setnummer in.")
    if not owner:
        raise ValueError("Vul in van wie de set is.")
    if not re.fullmatch(r"[1-9][0-9]{0,8}", quantity.strip()):
        raise ValueError("Vul bij aantal een positief geheel getal in.")
    values = (number, owner, owner.casefold(), int(quantity))
    with db:
        if item is None:
            result = db.execute("""INSERT INTO eigen_sets(setnummer, naam, eigenaar, eigenaar_sleutel, aantal)
                                 VALUES (?, '', ?, ?, ?)""", values)
            item = result.lastrowid
        else:
            db.execute("""UPDATE eigen_sets SET setnummer=?, eigenaar=?, eigenaar_sleutel=?, aantal=?
                          WHERE id=?""", (*values, item))
        if rental_numbers is not None:
            box, order = (value.strip() for value in rental_numbers)
            db.execute("""INSERT INTO huurgegevens (registratie_id, boxnummer, ordernummer) VALUES (?, ?, ?)
                          ON CONFLICT(registratie_id) DO UPDATE SET
                          boxnummer=excluded.boxnummer, ordernummer=excluded.ordernummer""", (item, box, order))
    return item


def collection_rows(db, rental):
    if rental:
        return db.execute("""SELECT e.id, e.setnummer, e.eigenaar, e.aantal,
                             COALESCE(h.boxnummer, ''), COALESCE(h.ordernummer, '')
                             FROM eigen_sets e LEFT JOIN huurgegevens h ON e.id=h.registratie_id
                             ORDER BY e.setnummer, e.eigenaar""").fetchall()
    return db.execute("SELECT id, setnummer, eigenaar, aantal FROM eigen_sets ORDER BY setnummer, eigenaar").fetchall()


def show_owned_sets(parent, path, initial_number, open_set, *, comparison_count, rental=False,
                    rental_action=None, return_action=None):
    with closing(connect_collection(path, rental=rental)) as db:
        rows = collection_rows(db, rental)
        metadata = {n: (title, pieces, theme, age) for n, title, pieces, theme, age in db.execute("SELECT * FROM setgegevens")}

    window = tk.Toplevel(parent)
    window.title("Gehuurde LEGO-sets · Superbouwers" if rental else "Eigen LEGO-sets")
    window.geometry("1340x480" if rental else "1120x460")
    window.minsize(790, 370)
    panel = ttk.Frame(window, padding=14)
    panel.pack(fill="both", expand=True)
    panel.rowconfigure(0, weight=1)
    panel.columnconfigure(0, weight=1)

    differences_column = "Open verschillen"
    columns = ("Setnummer", "Setnaam", "Eigenaar", "Aantal", "Aantal onderdelen", "Thema",
               "Leeftijdsindicatie", differences_column)
    identifier_columns = ("Boxnummer", "Ordernummer") if rental else ()
    columns += identifier_columns
    table = ttk.Treeview(panel, columns=columns, show="headings", selectmode="browse")
    table.grid(row=0, column=0, sticky="nsew")
    for column, width in zip(columns, (85, 240, 130, 65, 125, 105, 115, 135, 100, 120)):
        table.heading(column, text=column)
        table.column(column, width=width, minwidth=55)
    bar = ttk.Scrollbar(panel, command=table.yview)
    bar.grid(row=0, column=1, sticky="ns")
    table.configure(yscrollcommand=bar.set)
    sort_rows = sortable_headings(table)

    form = ttk.Frame(panel)
    form.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 6))
    fields = []
    for index, (label, width) in enumerate(zip(("Setnummer", "Eigenaar", "Aantal"), (13, 24, 7))):
        ttk.Label(form, text=label).grid(row=0, column=index, sticky="w", padx=(0, 8))
        field = ttk.Entry(form, width=width)
        field.grid(row=1, column=index, sticky="ew", padx=(0, 8))
        fields.append(field)
    ttk.Label(form, text="Setnaam").grid(row=0, column=3, sticky="w")
    current_name = tk.StringVar(window)
    ttk.Entry(form, textvariable=current_name, state="readonly").grid(row=1, column=3, sticky="ew")
    form.columnconfigure(3, weight=1)
    identifier_fields = []
    for index, label in enumerate(identifier_columns):
        ttk.Label(form, text=label).grid(row=2, column=index, sticky="w", pady=(6, 0))
        field = ttk.Entry(form, width=18)
        field.grid(row=3, column=index, sticky="ew", padx=(0, 8))
        identifier_fields.append(field)
    status = tk.StringVar(window)
    ttk.Label(panel, textvariable=status, wraplength=740).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
    editing = None
    pending = set()
    checked = set()
    rental_buttons = []
    difference_counts = {}

    def display_difference(number):
        if number not in difference_counts:
            difference_counts[number] = comparison_count(number)
        count = difference_counts[number]
        return count if count is not None else "Niet vergeleken"

    def values_for(number, owner, quantity, *rental_numbers):
        name, pieces, theme, age = metadata.get(number, ("", None, "", ""))
        return (number, name or "—", owner, quantity, pieces if pieces is not None else "—",
                theme or "—", age or "—", display_difference(number), *rental_numbers)

    def refresh_difference_count(_event):
        number = parent.active_set_number
        items = [item for item in table.get_children() if table.set(item, "Setnummer") == number]
        if items:
            difference_counts.pop(number, None)
            for item in items:
                table.set(item, differences_column, display_difference(number))
            sort_rows()

    def load_metadata(numbers):
        numbers = set(numbers).difference(metadata, pending, checked)
        if not numbers:
            return
        pending.update(numbers)

        def finished(number, data):
            pending.discard(number)
            checked.add(number)
            if not window.winfo_exists():
                return
            if data:
                metadata[number] = data
                for item in table.get_children():
                    if table.set(item, "Setnummer") == number:
                        details = [table.set(item, column) for column in identifier_columns]
                        table.item(item, values=values_for(number, table.set(item, "Eigenaar"),
                                                           table.set(item, "Aantal"), *details))
                sort_rows()
                if fields[0].get().strip().upper() == number:
                    current_name.set(data[0])
            elif fields[0].get().strip().upper() == number:
                current_name.set("Setgegevens niet beschikbaar")

        def fetch():
            for number in sorted(numbers):
                try:
                    data = fetch_set_details(number)
                    if data:
                        with closing(connect_collection(path)) as db, db:
                            db.execute("INSERT OR REPLACE INTO setgegevens VALUES (?,?,?,?,?)", (number, *data))
                except (OSError, sqlite3.Error):
                    data = None
                parent.after(0, finished, number, data)

        threading.Thread(target=fetch, daemon=True).start()

    def update_name(_event=None):
        number = fields[0].get().strip().upper()
        current_name.set(metadata.get(number, ("",))[0])
        if re.fullmatch(r"[A-Z0-9]+(?:-[0-9]+)?", number) and any(c.isdigit() for c in number):
            load_metadata([number])

    def display(values):
        for field, value in zip(fields + identifier_fields, values):
            field.delete(0, tk.END)
            field.insert(0, str(value))
        update_name()

    def new(number=""):
        nonlocal editing
        editing = None
        table.selection_remove(table.selection())
        display((number, "Superbouwers" if rental else "", 1, *("" for _ in identifier_fields)))
        save_button.configure(text="Toevoegen")
        open_button.configure(state="disabled")
        delete_button.configure(state="disabled")
        for button in rental_buttons:
            button.configure(state="disabled")
        status.set("")
        (fields[1] if number else fields[0]).focus_set()

    def select(_event=None):
        nonlocal editing
        if table.selection():
            editing = int(table.selection()[0])
            display([table.set(str(editing), field)
                     for field in ("Setnummer", "Eigenaar", "Aantal", *identifier_columns)])
            save_button.configure(text="Wijzigingen opslaan")
            open_button.configure(state="normal")
            delete_button.configure(state="normal")
            for button in rental_buttons:
                button.configure(state="normal")

    def refresh(data, selected=None):
        table.delete(*table.get_children())
        for row in data:
            table.insert("", "end", iid=str(row[0]), values=values_for(*row[1:]))
        sort_rows()
        if selected is not None:
            table.selection_set(str(selected))
            table.see(str(selected))
            select()

    def save():
        try:
            with closing(connect_collection(path, rental=rental)) as db:
                rental_numbers = tuple(field.get() for field in identifier_fields) if rental else None
                item = save_owned_set(db, editing, *(field.get() for field in fields), rental_numbers=rental_numbers)
                data = collection_rows(db, rental)
        except sqlite3.IntegrityError:
            status.set("Deze set staat al op naam van deze eigenaar. Selecteer die regel om het aantal te wijzigen.")
            return
        except (ValueError, sqlite3.Error) as exc:
            status.set(str(exc))
            return
        refresh(data, item)
        open_set(table.set(str(item), "Setnummer"))
        status.set("Registratie opgeslagen. De setanalyse start via het hoofdscherm.")

    def remove():
        if editing is None:
            return
        number = table.set(str(editing), "Setnummer")
        owner = table.set(str(editing), "Eigenaar")
        location = "de huurgeschiedenis" if rental else "je eigen collectie"
        if not messagebox.askyesno("Registratie verwijderen", f"Verwijder set {number} van {owner} uit {location}?",
                                   default=messagebox.NO, parent=window):
            return
        try:
            with closing(connect_collection(path)) as db, db:
                db.execute("DELETE FROM eigen_sets WHERE id=?", (editing,))
        except sqlite3.Error as exc:
            status.set(str(exc))
            return
        table.delete(str(editing))
        new()
        status.set(f"Uit {location} verwijderd.")

    def load():
        if editing is not None and open_set(table.set(str(editing), "Setnummer")):
            window.destroy()

    actions = ttk.Frame(panel)
    actions.grid(row=2, column=0, columnspan=2, sticky="ew")
    ttk.Button(actions, text="Nieuwe registratie", command=new).pack(side="left", padx=(0, 6))
    save_button = ttk.Button(actions, command=save)
    save_button.pack(side="left", padx=(0, 6))
    delete_button = ttk.Button(actions, text="Registratie verwijderen…", command=remove)
    delete_button.pack(side="right")
    open_button = ttk.Button(actions, text="Set openen", command=load)
    open_button.pack(side="right", padx=6)
    if rental and rental_action:
        rental_tools = ttk.Frame(panel)
        rental_tools.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        if return_action:
            ttk.Button(rental_tools, text="Retourzending aanmaken…",
                       command=lambda: return_action(window)).pack(side="left", padx=(0, 6))

        def run_rental_action(action):
            if editing is not None:
                rental_action(table.set(str(editing), "Setnummer"), action, box_number=table.set(str(editing), "Boxnummer"))

        for title, action in (("Onderdeelafwijkingen…", "deviations"),
                              ("Onderdelenformulier maken…", "form"),
                              ("Onderdelenformulier afdrukken…", "print")):
            button = ttk.Button(rental_tools, text=title, command=lambda a=action: run_rental_action(a))
            button.pack(side="left", padx=(0, 6))
            rental_buttons.append(button)
    table.bind("<<TreeviewSelect>>", select)
    table.bind("<Return>", lambda _event: load())
    fields[0].bind("<FocusOut>", update_name)
    bindings = {"<<SetComparisonUpdated>>": parent.bind("<<SetComparisonUpdated>>", refresh_difference_count, add="+")}
    if rental:
        def sync_history(_event):
            try:
                with closing(connect_collection(path, rental=True)) as db:
                    data = collection_rows(db, True)
            except sqlite3.Error as exc:
                status.set(str(exc))
                return
            for item, *values in data:
                if not table.exists(str(item)):
                    table.insert("", "end", iid=str(item), values=values_for(*values))
            sort_rows()
            load_metadata(row[1] for row in data)

        bindings["<<RentalHistoryChanged>>"] = parent.bind("<<RentalHistoryChanged>>", sync_history, add="+")

    def close_collection(event):
        if event.widget == window:
            for name, binding in bindings.items():
                parent.unbind(name, binding)
    window.bind("<Destroy>", close_collection)
    refresh(rows)
    new(initial_number)
    load_metadata(row[1] for row in rows)
    return window
