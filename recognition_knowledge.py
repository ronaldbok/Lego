"""Store reviewed drawing decisions and element links independently of set files."""

import sqlite3
from contextlib import closing


def save_reviews(path, source, confirmations=(), corrections=(), forget=None):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS tekeningen (
            bron TEXT, beeld TEXT, element TEXT, vervanging TEXT,
            PRIMARY KEY (bron, beeld, element))""")
        if forget:
            db.execute("DELETE FROM tekeningen WHERE bron=? AND beeld=? AND element=?", (source, *forget))
        db.executemany("INSERT OR REPLACE INTO tekeningen VALUES (?,?,?,?)",
                       ((source, drawing, element, element) for drawing, element in confirmations))
        db.executemany("INSERT OR REPLACE INTO tekeningen VALUES (?,?,?,?)",
                       ((source, drawing, element, replacement) for drawing, element, replacement in corrections))


def learned_reviews(path):
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tekeningen'").fetchone():
            return []
        return db.execute("SELECT bron, beeld, element, vervanging FROM tekeningen").fetchall()


def save_aliases(path, source, aliases):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS koppelingen (
            bron TEXT, element TEXT, catalogus TEXT, PRIMARY KEY (bron, element))""")
        db.execute("DELETE FROM koppelingen WHERE bron=?", (source,))
        db.executemany("INSERT INTO koppelingen VALUES (?,?,?)",
                       ((source, element, catalog) for element, catalog in aliases.items()))


def learned_aliases(path):
    aliases = {}
    if path.is_file():
        with closing(sqlite3.connect(path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='koppelingen'").fetchone():
                for source, element, catalog in db.execute("SELECT bron, element, catalogus FROM koppelingen"):
                    aliases.setdefault(source, {})[element] = catalog
    return aliases


def index_explanations(path, source):
    if not path.is_file():
        return {}
    with closing(sqlite3.connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='indexafwijkingen'").fetchone():
            return {}
        return {element: (expected, found, reason) for element, expected, found, reason in db.execute(
            "SELECT element, hoofdindex, bouwstappen, reden FROM indexafwijkingen WHERE bron=?", (source,))}


def save_index_explanation(path, source, element, explanation):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS indexafwijkingen (
            bron TEXT, element TEXT, hoofdindex INTEGER, bouwstappen INTEGER, reden TEXT,
            PRIMARY KEY (bron, element))""")
        if explanation is None:
            db.execute("DELETE FROM indexafwijkingen WHERE bron=? AND element=?", (source, element))
        else:
            db.execute("INSERT OR REPLACE INTO indexafwijkingen VALUES (?,?,?,?,?)", (source, element, *explanation))
