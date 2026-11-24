"""LEGO parts list helper for Windows. Run with: python lego_parts_app.py"""

from __future__ import annotations

import sqlite3
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from fnmatch import fnmatchcase
import json
from tempfile import NamedTemporaryFile
import hashlib
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from recognition_knowledge import index_explanations, learned_aliases, save_aliases
from tree_sort import sortable_headings

APP_DIR = Path(__file__).resolve().parent
PICTURE_DIR = APP_DIR / "onderdeelafbeeldingen"
_PICTURE_LOCKS = [threading.Lock() for _ in range(32)]
CONFIG_PATH = APP_DIR / "lego_parts_config.json"
API_ROOT = "https://rebrickable.com/api/v3/lego"
LEGO_GRAPHQL_API = "https://www.lego.com/api/graphql"
LEGO_INSTRUCTIONS_QUERY = """query getSetBuildingInstructions($setNumber: String!) {
  customerService {
    getBuildingInstructionsForSet(setNumber: $setNumber) {
      status
      data {
        buildingInstructions {
          isAdditionalInfoBooklet
          sequence { total element }
          pdf { pdfUrl }
        }
      }
    }
  }
}"""
HEADERS = {"User-Agent": "LegoPartsHelper/0.1 (desktop app)"}


PARTS_DB = APP_DIR / "onderdelen.sqlite3"
KNOWLEDGE_DB = APP_DIR / "herkenningskennis.sqlite3"
COLLECTION_DB = APP_DIR / "eigen_lego_sets.sqlite3"
RENTAL_HISTORY_DB = APP_DIR / "gehuurde_lego_sets.sqlite3"
PARTS_INDEX_CACHE_VERSION = 4
ALL_COLORS = "Alle kleuren"
UNKNOWN_COLOR = "Onbekende kleur"


def parts_connection():
    db = sqlite3.connect(PARTS_DB, timeout=8)
    db.execute("""CREATE TABLE IF NOT EXISTS onderdelen (
        elementnummer TEXT PRIMARY KEY COLLATE NOCASE, rebrickable_nummer TEXT NOT NULL,
        omschrijving TEXT NOT NULL, kleur TEXT NOT NULL, afbeelding_url TEXT NOT NULL)""")
    return db


def remember_parts(records):
    rows = []
    for record in records:
        number = str(record.get("element_id") or "").strip()
        part = record.get("part") or {}
        design = str(part.get("part_num") or "").strip()
        name = part.get("name")
        color = (record.get("color") or {}).get("name")
        if number and design and name and color and name not in ("Onbekend onderdeel", "Omschrijving niet gevonden"):
            rows.append((number, design, name, color, record.get("element_img_url") or part.get("part_img_url") or ""))
    with closing(parts_connection()) as db, db:
        db.executemany("""INSERT INTO onderdelen VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(elementnummer) DO UPDATE SET rebrickable_nummer=excluded.rebrickable_nummer,
            omschrijving=excluded.omschrijving, kleur=excluded.kleur,
            afbeelding_url=COALESCE(NULLIF(excluded.afbeelding_url,''),onderdelen.afbeelding_url)""", rows)


def local_parts(pdf_rows, aliases):
    found = {}
    with closing(parts_connection()) as db:
        for number, quantity in pdf_rows:
            for candidate in dict.fromkeys((aliases.get(number, number), number)):
                row = db.execute("SELECT rebrickable_nummer, omschrijving, kleur, afbeelding_url FROM onderdelen WHERE elementnummer=?",
                                 (candidate,)).fetchone()
                if row:
                    design, name, color, url = row
                    found[number] = dict(element_id=candidate, quantity=quantity, part=dict(part_num=design, name=name),
                                         color=dict(name=color), element_img_url=url)
                    break
    return found


BRICK_SEPARATOR_DESIGNS = {"6007", "96874", "31510", "55819"}
# Also recognise these PDF element numbers before catalog details are available.
BRICK_SEPARATOR_ELEMENTS = {"600728", "4211011", "4654448", "6000102", "6240515",
                           "6240517", "6254100", "6302093"}


def is_brick_separator(record):
    part = record.get("part") or {}
    return (str(part.get("part_num")) in BRICK_SEPARATOR_DESIGNS
            or str(record.get("element_id")) in BRICK_SEPARATOR_ELEMENTS
            or bool(re.match(r"^(?:tool\s*[,:-]?\s*)?(?:(?:wide|large|small)\s+)?(?:brick(?:\s+(?:and|&)\s+axle)?|element)\s+sep[ae]rator\b",
                             part.get("name") or "", re.I)))


def building_index_rows(rows, excluded, aliases):
    excluded = excluded | BRICK_SEPARATOR_ELEMENTS
    return [(number, qty) for number, qty in rows
            if number not in excluded and aliases.get(number) not in excluded]


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def request_json(url: str, api_key: str) -> dict:
    req = urllib.request.Request(url, headers={**HEADERS, "Authorization": f"key {api_key}"})
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def find_pdf_urls(value) -> list[str]:
    found = []
    if isinstance(value, dict):
        for child in value.values():
            found.extend(find_pdf_urls(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(find_pdf_urls(child))
    elif isinstance(value, str) and ".pdf" in value.lower():
        # LEGO CDN paths contain `.pdf` twice, e.g.
        # `/product.bi.core.pdf/4167345.pdf`; match the complete asset URL
        # instead of stopping at the category directory's first `.pdf`.
        pattern = (
            r"https?://[^\s\"'<>]+?/product\.bi\.[^/\s\"'<>]+\.pdf/"
            r"[^/\s\"'<>]+\.pdf(?:\?[^\s\"'<>]*)?"
            r"|https?://[^\s\"'<>]+/[^/\s\"'<>]+\.pdf(?:\?[^\s\"'<>]*)?"
        )
        for match in re.finditer(pattern, value, re.I):
            url = match.group(0).rstrip(",);}")
            if "lego.com" in urllib.parse.urlparse(url).netloc.lower():
                found.append(url)
    return found


def recycle_set_folder(folder: Path, window_handle: int) -> bool:
    """Recycle the set folder, or its contents if Windows holds only the folder open."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_handle = kernel.CreateFileW
    open_handle.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    open_handle.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid_handle = ctypes.c_void_p(-1).value

    def check_delete_access(path):
        delete_access = 0x00010000
        share_all = 0x00000007
        open_existing = 3
        open_directory = 0x02000000
        handle = open_handle(str(path), delete_access, share_all, None, open_existing, open_directory, None)
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            if error in (32, 33):
                return False
            raise ctypes.WinError(error)
        kernel.CloseHandle(handle)
        return True

    targets = [folder] if check_delete_access(folder) else list(folder.iterdir())
    for path in folder.rglob("*"):
        if path.resolve() != path or path.is_symlink():
            raise OSError(f"'{path.name}' verwijst naar een andere locatie.")
        if not check_delete_access(path):
            raise OSError(f"'{path.name}' is nog geopend. Sluit het bestand of de map en probeer het opnieuw.")
    if not targets:
        return True

    class ShellFileOperation(ctypes.Structure):
        _fields_ = [
            ("window", wintypes.HWND), ("operation", wintypes.UINT),
            ("source", wintypes.LPVOID), ("destination", wintypes.LPCWSTR),
            ("flags", wintypes.WORD), ("aborted", wintypes.BOOL),
            ("name_mappings", wintypes.LPVOID), ("progress_title", wintypes.LPCWSTR),
        ]

    delete_operation = 3
    allow_undo = 0x0040
    no_error_dialog = 0x0400
    paths = "\0".join(map(str, targets)) + "\0\0"
    source = ctypes.create_string_buffer(paths.encode("utf-16-le"))
    operation = ShellFileOperation(window_handle, delete_operation,
                                   ctypes.addressof(source), None, allow_undo | no_error_dialog)
    run = ctypes.windll.shell32.SHFileOperationW
    run.argtypes = [ctypes.POINTER(ShellFileOperation)]
    run.restype = ctypes.c_int
    result = run(ctypes.byref(operation))
    if operation.aborted:
        return False
    remaining = folder.exists() and (targets == [folder] or any(folder.iterdir()))
    if result or remaining:
        raise OSError(f"Windows kon de setgegevens niet verwijderen (code {result}). Sluit geopende bestanden en probeer het opnieuw.")
    return True


def download_official_instructions(set_number: str) -> list[Path]:
    target_dir = APP_DIR / "downloads" / set_number
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = target_dir / "instruction_pdfs.json"

    def valid_pdf(path: Path) -> bool:
        try:
            with path.open("rb") as pdf_file:
                return path.stat().st_size >= 10_000 and pdf_file.read(4) == b"%PDF"
        except OSError:
            return False

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cached_paths = [target_dir / name for name in manifest.get("files", [])]
        if cached_paths and all(valid_pdf(path) for path in cached_paths):
            return cached_paths
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass

    browser_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
    page_url = "https://www.lego.com/en-us/service/building-instructions/" + urllib.parse.quote(set_number)
    page_req = urllib.request.Request(page_url, headers={"User-Agent": browser_agent, "Accept": "text/html"})
    page_html = ""
    try:
        with urllib.request.urlopen(page_req, timeout=30) as response:
            page_html = response.read().decode("utf-8", errors="replace").replace("\\/", "/")
    except Exception:
        # Some networks block page requests from desktop apps; try LEGO's GraphQL lookup below.
        pass
    # The server-rendered HTML can contain only the first booklet while LEGO
    # loads the other cards through GraphQL. Always combine both sources.
    asset_path = r"/cdn/product-assets/product\.bi\.[^/\s\"'<>\\]+\.pdf/[^\s\"'<>\\]+?\.pdf(?:\?[^\s\"'<>\\]*)?"
    absolute_urls = re.findall(r"https?://(?:www\.)?lego\.com" + asset_path, page_html, re.I)
    relative_urls = re.findall(asset_path, page_html, re.I)
    html_urls = absolute_urls + ["https://www.lego.com" + url for url in relative_urls]

    # The official page uses GraphQL for the complete booklet list. Do not only
    # call it as a fallback: the HTML often contains the first booklet only.
    graphql_error = None
    try:
        # GraphQL preserves LEGO's booklet sequence, so prefer this order for
        # the downloaded paths and use HTML links as a fallback/additional source.
        urls = _graphql_instruction_urls(set_number, browser_agent) + html_urls
    except Exception as error:
        graphql_error = error
        urls = html_urls
    urls = list(dict.fromkeys(urls))
    if not urls:
        if graphql_error:
            raise ValueError(f"LEGO-instructies voor set {set_number} konden niet worden opgehaald: {graphql_error}") from graphql_error
        raise ValueError("Geen PDF-link gevonden op LEGO.com voor set " + set_number + ". De setpagina is bereikbaar, maar LEGO gaf geen instructie-PDF-link terug.")

    paths = []
    candidates_by_filename: dict[str, list[str]] = {}
    for index, url in enumerate(urls, 1):
        filename = Path(urllib.parse.urlparse(url).path).name or f"instructie-{index}.pdf"
        candidates_by_filename.setdefault(filename, []).append(url)
    for filename, candidate_urls in candidates_by_filename.items():
        path = target_dir / filename
        if valid_pdf(path):
            paths.append(path)
            continue
        failures = []
        downloaded = False
        for url in candidate_urls:
            pdf_req = urllib.request.Request(url, headers={"User-Agent": browser_agent, "Referer": page_url})
            try:
                with urllib.request.urlopen(pdf_req, timeout=180) as response:
                    content = response.read()
                if len(content) < 10_000 or not content.startswith(b"%PDF"):
                    failures.append(f"geen geldige PDF van {url}")
                    continue
                path.write_bytes(content)
                downloaded = True
                break
            except urllib.error.HTTPError as error:
                failures.append(f"HTTP {error.code} voor {url}")
        if not downloaded:
            detail = "; ".join(failures) or "geen bruikbare PDF-link gevonden"
            raise ValueError(f"Boekje {filename} kon niet worden gedownload: {detail}")
        paths.append(path)
    try:
        manifest_path.write_text(json.dumps({"version": 1, "files": [path.name for path in paths]}, indent=2), encoding="utf-8")
    except OSError:
        pass
    return paths


def _graphql_instruction_urls(set_number: str, browser_agent: str) -> list[str]:
    body = {"operationName": "getSetBuildingInstructions", "variables": {"setNumber": set_number}, "query": LEGO_INSTRUCTIONS_QUERY}
    req = urllib.request.Request(
        LEGO_GRAPHQL_API,
        data=json.dumps(body).encode("utf-8"),
        headers={"User-Agent": browser_agent, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        metadata = json.loads(response.read().decode("utf-8"))
    if metadata.get("errors"):
        details = json.dumps(metadata["errors"], ensure_ascii=False)
        raise ValueError("LEGO's instructiezoekopdracht gaf deze fout terug: " + details)
    result = metadata.get("data", {}).get("customerService", {}).get("getBuildingInstructionsForSet")
    if result is None:
        raise ValueError("LEGO antwoordde zonder instructieresultaat: " + json.dumps(metadata, ensure_ascii=False)[:1200])
    if result.get("status") and str(result["status"]).upper() not in ("SUCCESS", "OK"):
        raise ValueError(f"LEGO meldt status {result['status']}: " + json.dumps(result, ensure_ascii=False)[:1000])
    data = result.get("data", {}) if isinstance(result, dict) else {}
    instructions = data.get("buildingInstructions", []) if isinstance(data, dict) else []
    if isinstance(instructions, list):
        ordered = []
        for item in instructions:
            if not isinstance(item, dict):
                continue
            sequence = item.get("sequence", {})
            element = sequence.get("element") if isinstance(sequence, dict) else None
            try:
                order = int(element)
            except (TypeError, ValueError):
                order = len(ordered) + 1
            ordered.append((order, find_pdf_urls(item.get("pdf", {}))))
        urls = [url for _order, item_urls in sorted(ordered, key=lambda item: item[0]) for url in item_urls]
    else:
        urls = find_pdf_urls(result)
    if not urls:
        raise ValueError("LEGO-API gaf geen PDF-link terug: " + json.dumps(result, ensure_ascii=False)[:1000])
    return urls


def inventory_for_set(set_number: str, api_key: str) -> list[dict]:
    # Rebrickable expects the canonical set number (usually with -1 suffix).
    canonical = set_number if re.search(r"-\d+$", set_number) else f"{set_number}-1"
    url = f"{API_ROOT}/sets/{urllib.parse.quote(canonical, safe='')}/parts/?page_size=1000&inc_part_details=1&inc_minifig_parts=1"
    records = []
    while url:
        address = urllib.parse.urlparse(url)
        if address.scheme != "https" or address.netloc != urllib.parse.urlparse(API_ROOT).netloc:
            raise ValueError("Rebrickable gaf een onverwacht adres voor de volgende pagina.")
        page = request_json(url, api_key)
        records.extend(page.get("results", []))
        url = page.get("next")
    return records


class LegoPartsApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("LEGO onderdelenlijst")
        self.geometry("1050x690")
        self.minsize(780, 500)
        self.config_data = load_config()
        self.pdf_path: Path | None = None
        self.instruction_pdfs: list[Path] = []
        self.active_set_number = ""
        self.pdf_rows: list[tuple[str, int]] = []
        self.catalog_records: list[dict] = []
        legacy_aliases = self.config_data.pop("part_aliases", {})
        self.part_aliases: dict[str, dict[str, str]] = {}
        self._load_set_aliases(legacy_aliases if isinstance(legacy_aliases, dict) else {})
        self.element_color_cache = self.config_data.setdefault("element_color_cache", {})
        self._element_lookup_lock = threading.Lock()
        self._element_last_request = 0.0
        self._element_lookups_in_progress: set[str] = set()
        self._auto_lookup_signatures: set[tuple] = set()
        self._lookup_in_progress = False
        self._wait_count = 0
        self._collection_requests = deque()
        self._collection_request_job = None
        self._collection_request_running = False
        self.sort_directions: dict[str, bool] = {}
        self._build_ui()
        self.after(150, self._focus_set_number)

    def _focus_set_number(self) -> None:
        self.set_entry.focus_force()
        self.set_entry.selection_range(0, tk.END)

    def _refresh_status_style(self, *_args) -> None:
        if not hasattr(self, "status_label"):
            return
        message = self.status.get().casefold()
        if message.startswith(("fout:", "excel-export mislukt", "automatisch downloaden lukte niet",
                               "onderdelenindex niet automatisch gevonden")):
            background, foreground = "#FCE8E6", "#A61B1B"
        elif self._wait_count:
            background, foreground = "#FFF1CC", "#8A4B08"
        else:
            background, foreground = "#E8F2FC", "#174A73"
        self.status_label.configure(background=background, foreground=foreground)

    def _begin_wait(self) -> None:
        self._wait_count += 1
        self.configure(cursor="watch")
        self._refresh_status_style()
        self.update_idletasks()

    def _end_wait(self) -> None:
        if self._wait_count:
            self._wait_count -= 1
        if not self._wait_count:
            try:
                self.configure(cursor="")
            except tk.TclError:
                pass
        self._refresh_status_style()

    def _set_matches_path(self, set_number: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9]+(?:-[0-9]+)?", set_number):
            raise ValueError("Ongeldig setnummer voor het opslaan van koppelingen.")
        return APP_DIR / "downloads" / set_number / "visual_matches.json"

    @staticmethod
    def _pdf_signature(paths: list[Path]) -> list[dict]:
        signature = []
        for path in paths:
            stat = path.stat()
            signature.append({"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        return signature

    def _analysis_cache_path(self, filename: str) -> Path | None:
        set_number = self.active_set_number.strip()
        if not re.fullmatch(r"[A-Za-z0-9]+(?:-[0-9]+)?", set_number):
            return None
        return APP_DIR / "downloads" / set_number / filename

    def _load_parts_index_cache(self, pdf_paths: list[Path]) -> tuple[Path, list[tuple[str, int]]] | None:
        cache_path = self._analysis_cache_path("parts_index_cache.json")
        if cache_path is None:
            return None
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if cache.get("version") != PARTS_INDEX_CACHE_VERSION or cache.get("pdfs") != self._pdf_signature(pdf_paths):
                return None
            selected_path = Path(cache["selected_pdf"])
            rows = [(str(row[0]), int(row[1])) for row in cache["rows"]]
            if selected_path.exists() and rows:
                return selected_path, rows
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        return None

    def _save_parts_index_cache(self, pdf_paths: list[Path], selected_path: Path, rows: list[tuple[str, int]]) -> None:
        cache_path = self._analysis_cache_path("parts_index_cache.json")
        if cache_path is None:
            return
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": PARTS_INDEX_CACHE_VERSION, "pdfs": self._pdf_signature(pdf_paths), "selected_pdf": str(selected_path.resolve()), "rows": rows}
        try:
            cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _load_set_aliases(self, legacy_aliases: dict) -> None:
        self.part_aliases.update(learned_aliases(KNOWLEDGE_DB))
        downloads_dir = APP_DIR / "downloads"
        if downloads_dir.exists():
            for path in downloads_dir.glob("*/visual_matches.json"):
                if not re.fullmatch(r"[A-Za-z0-9]+(?:-[0-9]+)?", path.parent.name):
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        self.part_aliases[path.parent.name] = {str(key): str(value) for key, value in data.items()}
                        save_aliases(KNOWLEDGE_DB, path.parent.name, self.part_aliases[path.parent.name])
                except (OSError, json.JSONDecodeError):
                    continue
        for set_number, aliases in legacy_aliases.items():
            if not isinstance(set_number, str) or not isinstance(aliases, dict):
                continue
            if not re.fullmatch(r"[A-Za-z0-9]+(?:-[0-9]+)?", set_number):
                continue
            existing = self.part_aliases.setdefault(set_number, {})
            for pdf_id, catalog_id in aliases.items():
                existing.setdefault(str(pdf_id), str(catalog_id))
            self._save_set_aliases(set_number)
        if legacy_aliases:
            save_config(self.config_data)

    def _save_set_aliases(self, set_number: str) -> None:
        path = self._set_matches_path(set_number)
        aliases = self.part_aliases.get(set_number, {})
        if aliases:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(aliases, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        save_aliases(KNOWLEDGE_DB, set_number, aliases)

    def _recognition_aliases(self, set_number, pdf_rows, catalog_ids=None):
        local = self.part_aliases.get(set_number, {})
        numbers = {number for number, _ in pdf_rows}
        targets = {}
        for other_set, aliases in self.part_aliases.items():
            if other_set != set_number:
                for number, target in aliases.items():
                    if number in numbers:
                        targets.setdefault(number, set()).add(target)
        inherited = {}
        for number, choices in targets.items():
            if len(choices) != 1 or number in local:
                continue
            target = next(iter(choices))
            if target not in numbers and (catalog_ids is None or (target in catalog_ids and number not in catalog_ids)):
                inherited[number] = target
        target_counts = Counter(inherited.values())
        return {number: target for number, target in inherited.items()
                if target_counts[target] == 1 and target not in local.values()} | local

    def _set_element_lookup_path(self, set_number: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9]+(?:-[0-9]+)?", set_number):
            raise ValueError("Ongeldig setnummer voor het opslaan van Rebrickable-zoekresultaten.")
        return APP_DIR / "downloads" / set_number / "element_lookup_results.json"

    def _load_set_element_lookup_results(self, set_number: str, pdf_ids: set[str]) -> list[dict]:
        path = self._set_element_lookup_path(set_number)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [
            entry for entry in data
            if isinstance(entry, dict)
            and str(entry.get("element_id") or "").strip().lower() in pdf_ids
            and isinstance(entry.get("part"), dict)
        ]

    def _save_set_element_lookup_results(self, set_number: str) -> None:
        path = self._set_element_lookup_path(set_number)
        results = [entry for entry in self.catalog_records if entry.get("_element_lookup")]
        if results:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)
        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="LEGO onderdelenlijst", font=("Segoe UI", 19, "bold")).pack(side="left")
        ttk.Button(header, text="Onderdelen details…", command=self.open_management).pack(side="right")

        form = ttk.Frame(root)
        form.pack(fill="x")
        ttk.Label(form, text="Setnummer").grid(row=0, column=0, sticky="w")
        self.set_number = tk.StringVar(self)
        self.set_entry = ttk.Entry(form, textvariable=self.set_number, width=20, font=("Segoe UI", 11))
        self.set_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(4, 0))
        self.set_entry.bind("<Return>", lambda _e: self.open_instructions())
        self.download_button = ttk.Button(form, text="Set openen en analyseren", command=self.open_instructions)
        self.download_button.grid(row=1, column=1, padx=4, pady=(4, 0))
        self.view_pdfs_button = ttk.Button(form, text="Bekijk PDF’s…", command=self.open_instruction_pdfs, state="disabled")
        self.view_pdfs_button.grid(row=1, column=2, padx=4, pady=(4, 0))
        form.columnconfigure(0, weight=1)
        self._build_management()

        self.pdf_label = ttk.Label(root, text="Nog geen bouwinstructie geladen", foreground="#555")
        self.pdf_label.pack(anchor="w", pady=(12, 8))
        self.status = tk.StringVar(value="Vul een setnummer in om te beginnen.")
        self.status_label = tk.Label(root, textvariable=self.status, anchor="w", justify="left",
                                     font=("Segoe UI", 10, "bold"), padx=8, pady=5, relief="flat")
        self.status_label.pack(anchor="w", fill="x", pady=(0, 8))
        self.status.trace_add("write", self._refresh_status_style)
        self._refresh_status_style()
        columns = ("part", "rb_part", "description", "color", "quantity", "source")
        visible_columns = (("part", "LEGO-elementnr.", 130), ("description", "Omschrijving", 310),
                           ("color", "Kleur", 135), ("quantity", "Aantal", 70))
        filters = ttk.Frame(root)
        filters.pack(fill="x", pady=(0, 8))
        self._main_rows = []
        self._main_color_filter = tk.StringVar(value=ALL_COLORS)
        self._main_description_filter = tk.StringVar()
        ttk.Label(filters, text="Kleur").pack(side="left", padx=(0, 5))
        self._main_color_options = ttk.Combobox(filters, textvariable=self._main_color_filter,
                                                values=(ALL_COLORS,), state="readonly", width=22)
        self._main_color_options.pack(side="left", padx=(0, 12))
        ttk.Label(filters, text="Omschrijving (* of ?)").pack(side="left", padx=(0, 5))
        ttk.Entry(filters, textvariable=self._main_description_filter).pack(side="left", fill="x", expand=True)
        ttk.Button(filters, text="Wis filters", command=self._clear_main_filters).pack(side="left", padx=(8, 0))
        self._main_filter_count = ttk.Label(filters, width=13, anchor="e")
        self._main_filter_count.pack(side="left")
        table_frame = ttk.Frame(root)
        self._thumb_rows = {}
        self._thumb_photos = {}
        self._thumb_loading = set()
        self._thumb_job = None
        ttk.Style(self).configure("Parts.Treeview", rowheight=38)
        self.table = ttk.Treeview(table_frame, columns=columns,
                                  displaycolumns=tuple(key for key, _, _ in visible_columns),
                                  show="tree headings", style="Parts.Treeview")
        self.table.heading("#0", text="Plaatje", command=lambda: self._sort_table("part"))
        self.table.column("#0", width=58, minwidth=56, stretch=False)
        for key, title, width in visible_columns:
            self.table.heading(key, text=title, command=lambda column=key: self._sort_table(column))
            self.table.column(key, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        def on_scroll(first, last):
            scrollbar.set(first, last)
            self._schedule_thumbnails()

        self.table.configure(yscrollcommand=on_scroll)
        self.table.bind("<Configure>", lambda _: self._schedule_thumbnails())
        self.table.tag_configure("missing", foreground="#8a4b08")
        self.table.bind("<ButtonRelease-1>", self._show_main_part_details)
        self.table.bind("<Return>", self._show_main_part_details)
        self._main_color_filter.trace_add("write", self._apply_main_filters)
        self._main_description_filter.trace_add("write", self._apply_main_filters)
        self.bind("<<StepReviewSaved>>", self._invalidate_part_usage, add="+")
        self.bind("<<IndexExplanationSaved>>", self._update_set_comparison_status, add="+")
        list_actions = ttk.Frame(root)
        ttk.Button(list_actions, text="Eigen LEGO-sets…", command=self.open_owned_sets).pack(side="left", padx=(0, 8), pady=2)
        ttk.Button(list_actions, text="Gehuurde LEGO-sets…", command=self.open_rental_history).pack(side="left", pady=2)

        note = ttk.Label(root, text="PDF-uitlezing is afhankelijk van de opmaak van de instructie. Controleer de herkende regels altijd met de officiële lijst.", foreground="#555", wraplength=900)
        note.pack(side="bottom", anchor="w", pady=(9, 0))
        list_actions.pack(side="bottom", fill="x", pady=(6, 0))
        table_frame.pack(fill="both", expand=True)

    def _build_management(self):
        window = self._management_window = tk.Toplevel(self)
        window.withdraw()
        window.title("Onderdelen details")
        window.transient(self)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        window.bind("<Escape>", lambda _event: window.withdraw())
        tabs = ttk.Notebook(window)
        tabs.pack(fill="both", expand=True, padx=12, pady=12)

        sets = ttk.Frame(tabs, padding=14)
        tabs.add(sets, text="Setbeheer")
        self._managed_set_label = ttk.Label(sets)
        self._managed_set_label.pack(anchor="w", pady=(0, 10))
        self.set_number.trace_add("write", self._update_managed_set)
        self._update_managed_set()
        self.steps_button = ttk.Button(sets, text="Bouwstappen bekijken…", command=self.open_steps)
        self.usage_button = ttk.Button(sets, text="Bouwstappen per onderdeel…",
                                       command=lambda: self.open_steps(usage_only=True))
        self.lookup_button = ttk.Button(sets, text="Zoek ontbrekende onderdelen bij Rebrickable",
                                       command=self.lookup_unmatched_pdf_parts, state="disabled")
        self.compare_button = ttk.Button(sets, text="Visueel vergelijken…",
                                        command=self.open_visual_comparison, state="disabled")
        offline_button = ttk.Button(sets, text="Open offline bouwstappen…", command=self.open_saved_steps)
        export_button = ttk.Button(sets, text="Exporteer onderdelen naar excel", command=self.export_parts_to_excel)
        for button in (self.steps_button, self.usage_button, offline_button, export_button, self.lookup_button, self.compare_button,
                       ttk.Button(sets, text="Opgeslagen koppelingen beheren…", command=self.manage_saved_matches),
                       ttk.Button(sets, text="Set verwijderen…", command=self.delete_saved_set)):
            button.pack(anchor="w", fill="x", pady=3)

        settings = ttk.Frame(tabs, padding=14)
        tabs.add(settings, text="Instellingen")
        settings.columnconfigure(0, weight=1)
        ttk.Label(settings, text="Rebrickable API-sleutel").grid(row=0, column=0, sticky="w")
        self.key_entry = ttk.Entry(settings, show="•", width=40)
        self.key_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(4, 0))
        self.key_entry.insert(0, self.config_data.get("rebrickable_api_key", ""))
        ttk.Button(settings, text="Sleutel opslaan", command=self.save_key).grid(row=1, column=1, sticky="w")
        ttk.Label(settings, text="Naam klant (huurformulier)").grid(row=2, column=0, sticky="w", pady=(12, 0))
        self.customer_entry = ttk.Entry(settings)
        self.customer_entry.grid(row=3, column=0, sticky="ew", padx=(0, 8), pady=(4, 0))
        self.customer_entry.insert(0, self.config_data.get("customer_name", ""))
        ttk.Button(settings, text="Naam opslaan", command=self.save_customer_name).grid(row=3, column=1, sticky="w")
        ttk.Label(settings, text="Postcode (SuperBouwer-retourportaal)").grid(row=4, column=0, sticky="w", pady=(12, 0))
        self.return_postcode = tk.StringVar(value=self.config_data.get("return_postcode", ""))
        ttk.Entry(settings, textvariable=self.return_postcode).grid(row=5, column=0, sticky="ew", padx=(0, 8), pady=(4, 0))
        ttk.Button(settings, text="Postcode opslaan", command=self.save_return_postcode).grid(row=5, column=1, sticky="w")
        self._settings_feedback = tk.StringVar(window)
        ttk.Label(settings, textvariable=self._settings_feedback).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _update_managed_set(self, *_args):
        set_number = self.set_entry.get().strip()
        self._managed_set_label.configure(text=f"Ingevoerd setnummer: {set_number}" if set_number
                                          else "Vul een setnummer in op het hoofdscherm.")

    def open_management(self):
        self._management_window.deiconify()
        self._management_window.lift()

    def open_owned_sets(self):
        self._open_collection(COLLECTION_DB, "_owned_sets_window")

    def open_rental_history(self):
        self._open_collection(RENTAL_HISTORY_DB, "_rental_history_window", rental=True)

    def _open_collection(self, path, window_attribute, *, rental=False):
        from owned_sets import show_owned_sets

        window = getattr(self, window_attribute, None)
        if window is not None and window.winfo_exists():
            window.lift()
            return
        try:
            if rental:
                from rental_inventory import initialize_rental_history
                initialize_rental_history(path, APP_DIR / "downloads")
            window = show_owned_sets(self, path, self.set_entry.get().strip(), self._request_collection_set,
                                     comparison_count=self._collection_difference_count,
                                     rental=rental, rental_action=self._request_collection_set if rental else None,
                                     return_action=self._open_rental_return if rental else None)
            setattr(self, window_attribute, window)
        except sqlite3.Error as exc:
            messagebox.showerror("LEGO-sets", f"Het overzicht kon niet worden geopend. {exc}", parent=self)

    def _open_rental_return(self, parent):
        from tkinter import simpledialog
        from rental_inventory import open_return_portal

        postcode = self.config_data.get("return_postcode", "")
        if not postcode:
            postcode = simpledialog.askstring("Retourportaal", "Welke postcode wil je lokaal opslaan?",
                                             parent=parent)
            if postcode is None:
                return
            postcode = postcode.strip()
            if postcode:
                self.return_postcode.set(postcode)
                self.save_return_postcode()
        open_return_portal(parent, postcode)

    def _request_collection_set(self, number, action=None, *, box_number=""):
        self._collection_requests.append((number, action, box_number))
        if not self._collection_request_running and self._collection_request_job is None:
            self._collection_request_job = self.after_idle(self._run_collection_request)
        return True

    def _run_collection_request(self):
        self._collection_request_job = None
        if not self._collection_requests:
            return
        if self._wait_count or getattr(self, "_set_loading", False):
            self._collection_request_job = self.after(150, self._run_collection_request)
            return
        self._collection_request_running = True
        number, action, box_number = self._collection_requests.popleft()

        def finished(loaded):
            try:
                if loaded and action:
                    self.open_rental(action, box_number=box_number)
            finally:
                self._collection_request_running = False
                if self._collection_requests:
                    self._collection_request_job = self.after_idle(self._run_collection_request)

        if action and self.pdf_rows and number == self.active_set_number == self._inventory_set:
            finished(True)
        else:
            self.set_number.set(number)
            self.open_instructions(on_complete=finished)

    def open_rental(self, action="deviations", *, box_number=""):
        if self._wait_count or not self.pdf_rows or getattr(self, "_inventory_set", None) != self.active_set_number:
            self.status.set("Laad eerst de onderdelenlijst van de set.")
            return
        path = self._analysis_cache_path("huurregistratie.sqlite3")
        if path is None:
            self.status.set("Vul het setnummer in en laad de onderdelenlijst voor de huurregistratie.")
            return
        from rental_inventory import form_action, show_rental
        descriptions = {self.table.set(i, "part"): (self.table.set(i, "description"), self.table.set(i, "color"))
                        for i in self._main_rows}
        aliases = self._recognition_aliases(self.active_set_number, self.pdf_rows,
                                           self._catalog_element_ids(self.catalog_records) if self.catalog_records else None)
        selected = self.table.selection()
        selected_number = self.table.set(selected[0], "part") if selected else None
        inventory = [(n, *descriptions.get(aliases.get(n, n), descriptions.get(n, ("", ""))), qty)
                     for n, qty in self.pdf_rows]
        start = next((n for n, *_ in inventory if selected_number in (n, aliases.get(n,n))), None)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if action == "deviations":
                dialog = show_rental(self, self.active_set_number, inventory, path, start,
                                     history_path=RENTAL_HISTORY_DB, initially_rented=True)
                self.wait_window(dialog)
            else:
                window = getattr(self, "_rental_history_window", None)
                parent = window if window is not None and window.winfo_exists() else self
                form_action(parent, self.active_set_number, inventory, path,
                            self.customer_entry.get().strip(), box_number=box_number, print_form=action == "print")
        except (OSError, sqlite3.Error) as exc:
            messagebox.showerror("Huurregistratie", str(exc), parent=self)

    def open_saved_steps(self):
        from lego_steps import open_offline_usage

        set_number = self.set_entry.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9]+(?:-[0-9]+)?", set_number):
            messagebox.showinfo("Offline bouwstappen", "Vul eerst het LEGO-setnummer in.", parent=self)
            return
        path = APP_DIR / "downloads" / set_number / "bouwstappen_offline.json"
        try:
            explanations = index_explanations(KNOWLEDGE_DB, f"{set_number}/bouwstapcontroles.sqlite3")
            open_offline_usage(self, path, explanations)
        except FileNotFoundError:
            messagebox.showinfo("Offline bouwstappen",
                                f"Voor set {set_number} is nog geen sluitend overzicht opgeslagen.", parent=self)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            messagebox.showerror("Offline bouwstappen", f"Het overzicht kon niet worden geopend. {exc}", parent=self)

    def open_steps(self, usage_only=False, usage_callback=None):
        paths = list(self.instruction_pdfs) or ([self.pdf_path] if self.pdf_path else [])
        if not paths:
            self.status.set("Vul een setnummer in en kies ‘Set openen en analyseren’.")
            if usage_callback:
                usage_callback(None, "Geen bouwinstructie geladen.")
            return
        cache = self._analysis_cache_path("hoofdbouwstappen.json")
        offline_path = (cache.with_name("bouwstappen_offline.json")
                        if cache and getattr(self, "_inventory_set", None) == self.active_set_number else None)
        printed_geometry = self.active_set_number != "10307"
        index_pdf = self.pdf_path
        if cache:
            review_path = cache.with_name("bouwstapcontroles.sqlite3")
        else:
            digest = hashlib.sha256(str(paths[0].resolve()).encode()).hexdigest()[:20]
            review_path = APP_DIR / "downloads" / f"bouwstapcontroles-{digest}.sqlite3"
        if self._wait_count:
            messagebox.showinfo("Bouwstappen", "Wacht tot de onderdelenlijst is geladen.", parent=self)
            return
        aliases = self._recognition_aliases(self.active_set_number, self.pdf_rows,
                                           self._catalog_element_ids(self.catalog_records) if self.catalog_records else None)
        candidates = []
        picture_keys = {}
        for item in self._main_rows:
            number, design, name, color, _, source = self.table.item(item, "values")
            key = self._thumb_rows.get(item)
            if not key or not design or not source.startswith("PDF"):
                continue
            for element, total in self.pdf_rows:
                if number in (element, aliases.get(element)):
                    image = PICTURE_DIR / (key[0]+".png")
                    candidates.append(((element, design, name, color), image, total))
                    picture_keys[image] = key
        described = {record[0] for record, _, _ in candidates}
        missing = [(element, total) for element, total in self.pdf_rows if element not in described]
        known_missing = local_parts(missing, aliases) if missing else {}
        for element, total in missing:
            known = known_missing.get(element)
            if not known:
                candidates.append(((element, "", "Omschrijving onbekend", ""), None, total))
                continue
            part = known["part"]
            key = self._picture_key(known["element_id"], (known.get("element_img_url"),))
            image = PICTURE_DIR / (key[0] + ".png") if key else None
            candidates.append(((element, part["part_num"], part["name"], known["color"]["name"]), image, total))
            if image is not None:
                picture_keys[image] = key
        self.steps_button.configure(state="disabled")
        self.usage_button.configure(state="disabled")
        self.status.set("Opgeslagen bouwstapanalyse wordt gecontroleerd…" if usage_only
                        else "Onderdelenkaders en herhalingen worden gelezen…")
        self._begin_wait()

        def finished(books, matcher, error, prepared=None, save_complete=None, signature=None):
            self._end_wait()
            self.steps_button.configure(state="normal")
            self.usage_button.configure(state="normal")
            if paths != (self.instruction_pdfs or [self.pdf_path]):
                if usage_callback:
                    usage_callback(None, "De geladen bouwinstructie is gewijzigd.")
                return
            if error:
                self.status.set(f"Fout: bouwstappen lezen mislukt. {error}")
                if usage_callback:
                    usage_callback(None, error)
            elif not any(rows for _, rows in books):
                self.status.set("Geen hoofdbouwstappen met een herkenbaar onderdelenkader gevonden.")
                if usage_callback:
                    usage_callback(None, "Geen bouwstappen met een herkenbaar onderdelenkader gevonden.")
            else:
                from lego_steps import show_steps
                if matcher is None:
                    self.status.set("Opgeslagen bouwstapanalyse geladen uit de setmap.")
                elif usage_only:
                    self.status.set("Bouwstappen verwerkt en opgeslagen. Het overzicht is klaar.")
                else:
                    self.status.set(f"{sum(len(rows) for _, rows in books)} onderdelenkaders gevonden.")
                try:
                    review_path.parent.mkdir(parents=True, exist_ok=True)
                    known = {record[0]:record for record, _, _ in candidates}
                    inventory = [known.get(n, (n, "", "Omschrijving onbekend", "")) for n, _ in self.pdf_rows]
                    show_steps(self, books, matcher, review_path, inventory, usage_only=usage_only,
                               index_totals=dict(self.pdf_rows), prepared=prepared, save_complete=save_complete,
                               cached_catalog=candidates, offline_path=offline_path, usage_callback=usage_callback,
                               analysis_signature=signature, knowledge_path=KNOWLEDGE_DB)
                except (sqlite3.Error, OSError, RuntimeError, ValueError, IndexError) as exc:
                    self.status.set(f"Fout: bouwstapcontroles openen mislukt. {exc}")
                    if usage_callback:
                        usage_callback(None, str(exc))

        def worker():
            try:
                from lego_steps import load_steps, prepare_usage, reviewed_drawings
                from step_parts import PartMatcher
                pdf_signature = self._pdf_signature(paths)
                books = load_steps(paths, cache, pdf_signature)
                version = [hashlib.sha256((APP_DIR / name).read_bytes()).hexdigest()
                           for name in ("step_parts.py", "lego_pdf_text.py")]
                reviewed = reviewed_drawings(APP_DIR / "downloads", review_path,
                                             {record[0] for record, _, _ in candidates}, KNOWLEDGE_DB)

                def cache_signature():
                    inputs = [(record, total, str(image), image.stat().st_mtime_ns if image and image.is_file() else None)
                              for record, image, total in candidates]
                    return hashlib.sha256(json.dumps([pdf_signature, str(index_pdf), books, sorted(inputs),
                                                     version, sorted(reviewed.items())]).encode()).hexdigest()

                signature = cache_signature()
                review_path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(review_path)) as db:
                    db.execute("CREATE TABLE IF NOT EXISTS gebruiksanalyse (id INTEGER PRIMARY KEY, versie TEXT, resultaten TEXT)")
                    saved = db.execute("SELECT resultaten FROM gebruiksanalyse WHERE id=1 AND versie=?", (signature,)).fetchone()
                count = sum(len(rows) for _, rows in books)
                prepared = {}
                if saved:
                    try:
                        data = json.loads(saved[0])
                        if isinstance(data, dict) and set(data) == {str(i) for i in range(count)}:
                            prepared = data
                    except ValueError:
                        pass

                def save_complete(results):
                    with closing(sqlite3.connect(review_path)) as db, db:
                        db.execute("INSERT OR REPLACE INTO gebruiksanalyse VALUES (1,?,?)",
                                   (signature, json.dumps(results)))

                def report(done, total):
                    message = f"Bouwstappen worden geanalyseerd ({done}/{total})…"
                    self.after(0, self.status.set, message)

                matcher = None
                if len(prepared) != count:
                    missing_keys = [key for image, key in picture_keys.items() if not image.is_file()]
                    if missing_keys and usage_only:
                        self.after(0, self.status.set, f"Referentieafbeeldingen laden ({len(missing_keys)})…")
                        with ThreadPoolExecutor(max_workers=4) as pool:
                            list(pool.map(lambda key: self._load_part_picture(key, None) is not None, missing_keys))
                        signature = cache_signature()
                    matcher = PartMatcher(candidates, index_first=True, reviewed=reviewed,
                                          printed_geometry=printed_geometry)
                    matcher.load_index_checks(index_pdf)
                    if usage_only:
                        prepare_usage(books, matcher, prepared, report)
                        save_complete(prepared)
            except Exception as exc:
                self.after(0, finished, [], None, str(exc))
            else:
                self.after(0, finished, books, matcher, None, prepared, save_complete, signature)

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_thumbnails(self):
        if self._thumb_job is None:
            self._thumb_job = self.after_idle(self._load_visible_thumbnails)

    def _load_visible_thumbnails(self):
        self._thumb_job = None
        visible = dict.fromkeys(self.table.identify_row(y)
                                for y in range(0, self.table.winfo_height(), 18))
        for row in visible:
            key = self._thumb_rows.get(row)
            if not key:
                continue
            if key in self._thumb_photos:
                photo = self._thumb_photos[key]
                if photo is not None and not self.table.item(row, "image"):
                    self.table.item(row, image=photo)
            elif key not in self._thumb_loading and len(self._thumb_loading) < 3:
                self._thumb_loading.add(key)
                threading.Thread(target=self._fetch_thumbnail, args=(key,), daemon=True).start()

    @staticmethod
    def _load_part_picture(key, size):
        from PIL import Image
        name, urls = key
        destination = PICTURE_DIR / (name + ".png")
        with _PICTURE_LOCKS[hash(name) % len(_PICTURE_LOCKS)]:
            try:
                with Image.open(destination) as source:
                    picture = source.convert("RGBA")
            except (OSError, ValueError, Image.DecompressionBombError):
                picture = None
            if picture is None:
                for url in urls:
                    try:
                        digest = hashlib.sha256(url.encode()).hexdigest() + ".png"
                        legacy = next((APP_DIR / "downloads").glob("*/rebrickable_images/" + digest), None)
                        if legacy:
                            data = legacy.read_bytes()
                        else:
                            request = urllib.request.Request(url, headers=HEADERS)
                            with urllib.request.urlopen(request, timeout=8) as response:
                                data = response.read(8_000_001)
                            if len(data) > 8_000_000:
                                continue
                        with Image.open(BytesIO(data)) as source:
                            picture = source.convert("RGBA")
                        break
                    except (OSError, ValueError, Image.DecompressionBombError):
                        continue
                if picture is None:
                    return None
                temp = None
                try:
                    PICTURE_DIR.mkdir(parents=True, exist_ok=True)
                    with NamedTemporaryFile(dir=PICTURE_DIR, suffix=".tmp", delete=False) as stream:
                        temp = Path(stream.name)
                        picture.save(stream, format="PNG")
                    temp.replace(destination)
                except OSError:
                    pass
                finally:
                    if temp is not None:
                        temp.unlink(missing_ok=True)
        if size:
            picture.thumbnail(size, Image.Resampling.LANCZOS)
        return picture

    def _prefetch_pictures(self):
        if hasattr(self, "_prefetch_stop"):
            self._prefetch_stop.set()
        stop = self._prefetch_stop = threading.Event()
        keys = tuple(dict.fromkeys(self._thumb_rows.values()))
        def populate():
            for key in keys:
                if stop.is_set():
                    break
                if not (PICTURE_DIR / (key[0] + ".png")).is_file():
                    self._load_part_picture(key, None)
        threading.Thread(target=populate, daemon=True).start()

    def _fetch_thumbnail(self, key):
        thumbnail = self._load_part_picture(key, (46, 34))
        self.after(0, self._thumbnail_ready, key, thumbnail)

    def _thumbnail_ready(self, key, thumbnail):
        from PIL import ImageTk
        self._thumb_loading.discard(key)
        if key in self._thumb_rows.values():
            self._thumb_photos[key] = ImageTk.PhotoImage(thumbnail, master=self) if thumbnail is not None else None
        self._schedule_thumbnails()

    @staticmethod
    def _picture_key(number, urls=(), design_color=None):
        candidates = list(dict.fromkeys(url for url in urls if url))
        if str(number).isdigit() and design_color is None:
            fallback = f"https://cdn.rebrickable.com/media/parts/elements/{number}.jpg"
            if fallback not in candidates:
                candidates.append(fallback)
        if not re.fullmatch(r"[0-9a-zA-Z_-]{1,80}", str(number)) or not candidates:
            return None
        if design_color is None and str(number).isdigit():
            name = str(number)
        else:
            color = hashlib.sha256(str(design_color or "").encode()).hexdigest()[:12]
            name = f"part-{number}-{color}"
        return name, tuple(candidates)

    def _register_thumbnail(self, row, number, urls=(), design_color=None):
        key = self._picture_key(number, urls, design_color)
        if key:
            self._thumb_rows[row] = key

    def _invalidate_part_usage(self, _event=None):
        self._part_usage = None
        self.event_generate("<<SetComparisonUpdated>>")
        refresh = getattr(self, "_refresh_open_part_usage", None)
        if refresh:
            refresh()

    def _show_main_part_details(self, event=None) -> None:
        from lego_steps import step_usage_values

        mouse = event is not None and event.type == tk.EventType.ButtonRelease
        if mouse and self.table.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        row_id = self.table.identify_row(event.y) if mouse else self.table.focus()
        if not row_id:
            return
        values = self.table.item(row_id, "values")
        if not values:
            return
        element_id, part_number = str(values[0]).strip(), str(values[1]).strip()
        color_name = str(values[3]).strip()
        key = self._thumb_rows.get(row_id) or self._picture_key(element_id)
        api_key = self.key_entry.get().strip()

        try:
            from PIL import Image, ImageTk
        except ImportError:
            messagebox.showerror("Module ontbreekt", "Installeer Pillow met: pip install -r requirements.txt", parent=self)
            return

        pdf_rows = self.pdf_rows
        previous = getattr(self, "_part_detail_viewer", None)
        if previous is not None and previous.winfo_exists():
            if previous._part_row == row_id and previous._part_inventory is pdf_rows:
                previous.lift()
                return
            previous.destroy()
        viewer = self._part_detail_viewer = tk.Toplevel(self)
        viewer._part_row = row_id
        viewer._part_inventory = pdf_rows
        viewer.title(f"Onderdeel {element_id}")
        viewer.geometry("820x540")
        viewer.minsize(640, 400)
        header = ttk.Frame(viewer, padding=16)
        header.pack(fill="x")
        picture = ttk.Frame(header, width=190, height=150)
        picture.pack(side="left", padx=(0, 20))
        picture.pack_propagate(False)
        image_label = ttk.Label(picture, text="Afbeelding laden…", anchor="center", wraplength=170)
        image_label.pack(fill="both", expand=True)
        info = ttk.Frame(header)
        info.pack(side="left", fill="x", expand=True)
        ttk.Label(info, text=values[2], font=("Segoe UI", 14, "bold"), wraplength=400).pack(anchor="w")
        ttk.Label(info, text=f"LEGO-element {element_id}" + (f" · Rebrickable {part_number}" if part_number else "")).pack(anchor="w", pady=(5, 10))
        ttk.Label(info, text=f"Kleur: {color_name or 'Onbekend'}").pack(anchor="w")
        aliases = self._recognition_aliases(self.active_set_number, pdf_rows,
                                           self._catalog_element_ids(self.catalog_records) if self.catalog_records else None)
        quantities = {number: qty for number, qty in pdf_rows if element_id in (number, aliases.get(number))}
        total = sum(quantities.values()) if quantities else values[4]
        ttk.Label(info, text=f"Totaal aantal: {total}").pack(anchor="w", pady=(4, 0))

        frame = ttk.LabelFrame(viewer, text="Bouwstappen", padding=8)
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        status = tk.StringVar(viewer, value="Bouwstappen laden…")
        ttk.Label(frame, textvariable=status, wraplength=590).pack(anchor="w", pady=(0, 6))
        columns = ("Boekje", "Stap", "PDF-pagina", "In stap", "Reeks", "Met reeks", "Herkenning")
        details = ttk.Treeview(frame, columns=columns, show="headings")
        for column, width in zip(columns, (55, 55, 85, 65, 55, 74, 195)):
            details.heading(column, text=column)
            details.column(column, width=width, minwidth=45)
        bar = ttk.Scrollbar(frame, command=details.yview)
        bar.pack(side="right", fill="y")
        details.configure(yscrollcommand=bar.set)
        details.pack(fill="both", expand=True)
        sort_details = sortable_headings(details)

        def render_usage(usage, error):
            if not viewer.winfo_exists() or pdf_rows is not self.pdf_rows:
                return
            details.delete(*details.get_children())
            if error:
                status.set(f"Bouwstappen niet beschikbaar. {error}")
                return
            steps = {}
            for number in quantities:
                for item, line in usage.get(number, {}).items():
                    if item in steps:
                        steps[item][3] += line[3]
                        steps[item][5] += line[5]
                        steps[item][6].update(line[6])
                    else:
                        steps[item] = [*line[:6], line[6].copy()]
            for row in step_usage_values(steps):
                details.insert("", "end", values=row)
            sort_details()
            status.set(f"{len(steps)} bouwstappen" if steps else "Dit onderdeel is in geen bouwstap herkend.")

        def load_usage():
            if not viewer.winfo_exists() or pdf_rows is not self.pdf_rows:
                return
            cached = getattr(self, "_part_usage", None)
            if cached and cached[0] is pdf_rows:
                render_usage(cached[1], None)
            elif not quantities:
                render_usage({}, None)
            elif self._wait_count:
                self.after(150, load_usage)
            else:
                status.set("Bouwstapanalyse wordt geladen of berekend…")
                def ready(usage, error):
                    if error is None and self.pdf_rows is pdf_rows:
                        self._part_usage = (pdf_rows, usage)
                        self.event_generate("<<SetComparisonUpdated>>")
                    render_usage(usage, error)
                self.open_steps(usage_only=True, usage_callback=ready)

        self._refresh_open_part_usage = load_usage
        viewer.after_idle(load_usage)

        def load_image() -> None:
            image = self._load_part_picture(key, None) if key else None
            last_error = None
            if image is None and api_key:
                try:
                    with self._element_lookup_lock:
                        delay = 1.1 - (time.monotonic() - self._element_last_request)
                        if delay > 0:
                            time.sleep(delay)
                        self._element_last_request = time.monotonic()
                        if element_id:
                            detail_url = f"{API_ROOT}/elements/{urllib.parse.quote(element_id, safe='')}/"
                        else:
                            detail_url = f"{API_ROOT}/parts/{urllib.parse.quote(part_number, safe='')}/"
                        details = request_json(detail_url, api_key)
                    part = details.get("part") or {}
                    detail_urls = [details.get("element_img_url"), part.get("part_img_url") if isinstance(part, dict) else None, details.get("part_img_url")]
                    extra = tuple(url for url in detail_urls if url)
                    if extra and key:
                        image = self._load_part_picture((key[0], extra), None)
                except Exception as error:
                    last_error = error
            if image is not None:
                self.after(0, self._display_main_part_image, viewer, image_label, image, Image, ImageTk, (184, 144))
                return
            message = "Geen Rebrickable-afbeelding gevonden voor dit onderdeel."
            if last_error:
                message += f" ({type(last_error).__name__})"
            self.after(0, self._show_main_part_image_error, viewer, image_label, message)

        threading.Thread(target=load_image, daemon=True).start()

    @staticmethod
    def _display_main_part_image(viewer, image_label, image, image_module, image_tk, size) -> None:
        if not viewer.winfo_exists():
            return
        thumbnail = image.copy()
        thumbnail.thumbnail(size, image_module.Resampling.LANCZOS)
        photo = image_tk.PhotoImage(thumbnail, master=viewer)
        viewer._part_photo = photo
        image_label.configure(image=photo, text="")
        if size == (184, 144):
            def zoom(_event):
                window = tk.Toplevel(viewer)
                window.title(f"Afbeelding · {viewer.title()}")
                label = ttk.Label(window)
                label.pack(padx=12, pady=12)
                LegoPartsApp._display_main_part_image(window, label, image, image_module, image_tk, (580, 500))
            image_label.configure(cursor="hand2")
            image_label.bind("<Button-1>", zoom)

    @staticmethod
    def _show_main_part_image_error(viewer, image_label, message: str) -> None:
        if viewer.winfo_exists():
            image_label.configure(text=message)

    def manage_saved_matches(self) -> None:
        set_number = self.set_entry.get().strip()
        aliases = self.part_aliases.get(set_number, {})
        if not aliases:
            messagebox.showinfo("Geen opgeslagen koppelingen", f"Voor set {set_number or '(geen setnummer)'} zijn geen opgeslagen koppelingen gevonden.", parent=self)
            return

        dialog = tk.Toplevel(self)
        dialog.title(f"Koppelingen beheren – set {set_number}")
        dialog.geometry("560x360")
        dialog.transient(self)
        ttk.Label(dialog, text="Zoek een verkeerde koppeling en verwijder deze. De onderdelen worden daarna weer als ongekoppeld getoond.", wraplength=520).pack(anchor="w", padx=12, pady=(12, 6))
        search_var = tk.StringVar()
        ttk.Label(dialog, text="Zoeken op LEGO- of Rebrickable-nummer").pack(anchor="w", padx=12)
        search_entry = ttk.Entry(dialog, textvariable=search_var)
        search_entry.pack(fill="x", padx=12, pady=(3, 8))
        matches_tree = ttk.Treeview(dialog, columns=("pdf", "catalog"), show="headings", height=9, selectmode="browse")
        matches_tree.heading("pdf", text="LEGO-elementnummer")
        matches_tree.heading("catalog", text="Rebrickable-nummer")
        matches_tree.column("pdf", width=220)
        matches_tree.column("catalog", width=220)
        matches_tree.pack(fill="both", expand=True, padx=12)
        sort_matches = sortable_headings(matches_tree)

        def refresh_matches(*_args):
            query = search_var.get().strip().casefold()
            for item_id in matches_tree.get_children(""):
                matches_tree.delete(item_id)
            for pdf_id, catalog_id in aliases.items():
                if not query or query in pdf_id.casefold() or query in str(catalog_id).casefold():
                    matches_tree.insert("", "end", iid=pdf_id, values=(pdf_id, catalog_id))
            sort_matches()

        search_var.trace_add("write", refresh_matches)
        refresh_matches()

        def remove_selected():
            selected = matches_tree.selection()
            if not selected:
                messagebox.showinfo("Selectie ontbreekt", "Selecteer eerst de koppeling die je wilt verwijderen.", parent=dialog)
                return
            pdf_id = selected[0]
            catalog_id = aliases.pop(pdf_id, None)
            if catalog_id is None:
                return
            if not aliases:
                self.part_aliases.pop(set_number, None)
            self._save_set_aliases(set_number)
            matches_tree.delete(pdf_id)
            if self.catalog_records and self.pdf_rows:
                self._show_catalog_results(self.catalog_records, self.pdf_rows, auto_compare=False)
            self.status.set(f"Koppeling verwijderd: LEGO {pdf_id} ↔ Rebrickable {catalog_id}.")
            if not matches_tree.get_children(""):
                dialog.destroy()

        ttk.Button(dialog, text="Geselecteerde koppeling verwijderen", command=remove_selected).pack(anchor="e", padx=12, pady=12)

    def export_parts_to_excel(self) -> None:
        rows = [(self.table.item(i, "values"), self._thumb_rows.get(i)) for i in self._main_rows]
        if not rows:
            messagebox.showinfo("Geen onderdelen", "Lees eerst een onderdelenlijst in voordat je exporteert.", parent=self)
            return
        set_number = self.set_entry.get().strip()
        initial_dir = APP_DIR / "downloads" / set_number if re.fullmatch(r"[A-Za-z0-9]+(?:-[0-9]+)?", set_number) else APP_DIR
        initial_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{set_number}_onderdelen.xlsx" if set_number else "lego_onderdelen.xlsx"
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="Onderdelenlijst exporteren",
            initialdir=str(initial_dir),
            initialfile=filename,
            defaultextension=".xlsx",
            filetypes=[("Excel-werkmap", "*.xlsx")],
        )
        if not destination:
            return

        self._begin_wait()
        self.status.set("Excel-export wordt samengesteld met onderdeelafbeeldingen…")

        def export():
            try:
                count = self._write_parts_workbook(destination, rows)
            except Exception as error:
                self.after(0, self._finish_excel_export, destination, len(rows), 0, str(error))
            else:
                self.after(0, self._finish_excel_export, destination, len(rows), count, None)

        threading.Thread(target=export, daemon=True).start()

    def _finish_excel_export(self, destination, total, count, error):
        self._end_wait()
        if error:
            self.status.set("Excel-export mislukt.")
            messagebox.showerror("Export mislukt", error, parent=self)
        else:
            self.status.set(f"Excel-export opgeslagen met {count} afbeeldingen voor {total} onderdelen: {destination}")

    @staticmethod
    def _write_parts_workbook(destination, rows):
        from concurrent.futures import ThreadPoolExecutor
        from tempfile import NamedTemporaryFile
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as ExcelImage
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Onderdelen"
        headers = ("LEGO-elementnummer", "Rebrickable-nummer", "Omschrijving", "Kleur", "Aantal", "Bron", "Afbeelding")
        sheet.append(headers)
        for row, _ in rows:
            values = list(row)
            try:
                values[4] = int(values[4])
            except (ValueError, TypeError):
                pass
            sheet.append(values)
        header_fill = PatternFill("solid", fgColor="1F4E78")
        for cell in sheet[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for column_index, header in enumerate(headers, 1):
            column_values = [str(sheet.cell(row=row_index, column=column_index).value or "") for row_index in range(1, sheet.max_row + 1)]
            sheet.column_dimensions[get_column_letter(column_index)].width = min(60, max(12, max(map(len, column_values)) + 2))
        def picture_bytes(key):
            picture = LegoPartsApp._load_part_picture(key, (88, 60))
            if picture is None:
                return None
            buffer = BytesIO()
            picture.save(buffer, format="PNG")
            return buffer.getvalue()

        keys = list(dict.fromkeys(key for _, key in rows if key))
        with ThreadPoolExecutor(max_workers=4) as pool:
            pictures = dict(zip(keys, pool.map(picture_bytes, keys)))
        count = 0
        sheet.column_dimensions["G"].width = 15
        for index, (_, key) in enumerate(rows, 2):
            data = pictures.get(key)
            if data:
                sheet.add_image(ExcelImage(BytesIO(data)), f"G{index}")
                sheet.row_dimensions[index].height = 48
                count += 1
        with NamedTemporaryFile(dir=Path(destination).parent, suffix=".xlsx", delete=False) as temp:
            temp_path = Path(temp.name)
        try:
            workbook.save(temp_path)
            temp_path.replace(destination)
        finally:
            temp_path.unlink(missing_ok=True)
            workbook.close()
        return count

    def lookup_unmatched_pdf_parts(self) -> None:
        if getattr(self, "_set_loading", False):
            return
        if not self.pdf_rows or not self.catalog_records:
            messagebox.showinfo("Onderdelenlijst ontbreekt", "Lees eerst een PDF en laad de Rebrickable-setinventaris.", parent=self)
            return
        api_key = self.key_entry.get().strip()
        if not api_key:
            messagebox.showinfo("API-sleutel ontbreekt", "Vul eerst je Rebrickable API-sleutel in en sla deze op.", parent=self)
            return
        known_ids = {str(entry.get("element_id") or "").strip().lower() for entry in self.catalog_records}
        aliases = self.part_aliases.get(self.set_entry.get().strip(), {})
        matched_pdf_ids = known_ids | {str(pdf_id).lower() for pdf_id in aliases}
        unmatched = [(str(number).strip(), quantity) for number, quantity in self.pdf_rows if str(number).strip().lower() not in matched_pdf_ids]
        if not unmatched:
            self.status.set("Alle PDF-elementnummers zijn al gekoppeld aan Rebrickable.")
            return
        self.lookup_button.configure(state="disabled")
        self.status.set(f"Rebrickable zoekt {len(unmatched)} ontbrekende PDF-elementnummers op…")
        self._lookup_in_progress = True
        self._begin_wait()
        threading.Thread(target=self._lookup_unmatched_pdf_parts_worker, args=(unmatched, api_key), daemon=True).start()

    def _lookup_unmatched_pdf_parts_worker(self, unmatched: list[tuple[str, int]], api_key: str) -> None:
        found = []
        failed = 0
        cached = local_parts(unmatched, {})
        for index, (element_id, quantity) in enumerate(unmatched, 1):
            if element_id in cached:
                found.append(dict(cached[element_id], _element_lookup=True))
                continue
            try:
                with self._element_lookup_lock:
                    delay = 1.1 - (time.monotonic() - self._element_last_request)
                    if delay > 0:
                        time.sleep(delay)
                    self._element_last_request = time.monotonic()
                    url = f"{API_ROOT}/elements/{urllib.parse.quote(element_id, safe='')}/"
                    details = request_json(url, api_key)
                part = details.get("part") or {}
                color = details.get("color") or {}
                if not isinstance(part, dict):
                    part = {"part_num": str(part)}
                if not isinstance(color, dict):
                    color = {"name": str(color)}
                part_number = str(part.get("part_num") or details.get("part_num") or "").strip()
                if not part_number:
                    failed += 1
                    continue
                part.setdefault("part_num", part_number)
                part.setdefault("name", details.get("part_name") or "Onbekend onderdeel")
                part.setdefault("part_img_url", details.get("part_img_url") or "")
                found.append({
                    "element_id": element_id,
                    "part": part,
                    "color": color,
                    "quantity": quantity,
                    "element_img_url": details.get("element_img_url") or "",
                    "_element_lookup": True,
                })
            except Exception:
                failed += 1
            self.after(0, self.status.set, f"Rebrickable-zoekopdracht: {index}/{len(unmatched)} elementnummers gecontroleerd…")
        try:
            remember_parts(found)
        finally:
            self.after(0, self._finish_unmatched_pdf_lookup, found, failed)

    def _finish_unmatched_pdf_lookup(self, found: list[dict], failed: int) -> None:
        self._end_wait()
        self._lookup_in_progress = False
        self.lookup_button.configure(state="normal")
        existing = {str(entry.get("element_id") or "").strip().lower() for entry in self.catalog_records}
        set_number = self.set_entry.get().strip()
        aliases = self.part_aliases.get(set_number, {})
        added = []
        matched_by_part_number = 0
        for entry in found:
            element_id = str(entry["element_id"]).strip()
            if element_id.lower() in existing:
                continue
            part = entry.get("part") or {}
            color = entry.get("color") or {}
            part_number = str(part.get("part_num") or "").strip().casefold()
            color_id = str(color.get("id") or "").strip()
            color_name = str(color.get("name") or "").strip().casefold()
            same_part_and_color = []
            if part_number and (color_id or color_name):
                for candidate in [*self.catalog_records, *added]:
                    candidate_part = candidate.get("part") or {}
                    candidate_color = candidate.get("color") or {}
                    if str(candidate_part.get("part_num") or "").strip().casefold() != part_number:
                        continue
                    candidate_color_id = str(candidate_color.get("id") or "").strip()
                    candidate_color_name = str(candidate_color.get("name") or "").strip().casefold()
                    if (color_id and candidate_color_id == color_id) or (color_name and candidate_color_name == color_name):
                        same_part_and_color.append(candidate)
            if same_part_and_color:
                candidate = same_part_and_color[0]
                target_id = str(candidate.get("element_id") or (candidate.get("part") or {}).get("part_num") or "").strip()
                if target_id:
                    aliases[element_id] = target_id
                    matched_by_part_number += 1
                    if candidate.get("_element_lookup"):
                        candidate["quantity"] = int(candidate.get("quantity") or 0) + int(entry.get("quantity") or 0)
                    continue
            added.append(entry)
        if added:
            self.catalog_records.extend(added)
        if added or matched_by_part_number:
            self._save_set_element_lookup_results(set_number)
        if matched_by_part_number:
            self.part_aliases[set_number] = aliases
            self._save_set_aliases(set_number)
        self._show_catalog_results(self.catalog_records, self.pdf_rows, auto_compare=False)
        has_unmatched = self._has_unmatched_parts()
        if getattr(self, "_set_loading", False):
            self._analyze_loaded_set()
            return
        summary = f"Rebrickable-zoeking klaar. {matched_by_part_number} bestaande onderdelen gekoppeld op onderdeelnummer en kleur."
        if added:
            summary += f" {len(added)} onderdelen toegevoegd met het PDF-aantal."
        if failed:
            summary += f" Voor {failed} elementnummer(s) gaf de zoekactie geen resultaat."
        if has_unmatched:
            self.status.set(summary + " Resterende verschillen staan in de visuele vergelijking.")
            self.after(150, self.open_visual_comparison)
        else:
            self.status.set(summary + " Alle PDF-onderdelen zijn gekoppeld.")

    @staticmethod
    def _catalog_element_ids(records):
        return {
            str(entry.get("element_id") or entry.get("part", {}).get("part_num") or "").strip().lower()
            for entry in records
        }

    def _has_unmatched_parts(self) -> bool:
        pdf_ids = {str(number).strip().lower() for number, _quantity in self.pdf_rows}
        catalog_ids = self._catalog_element_ids(self.catalog_records)
        aliases = self._recognition_aliases(self.set_entry.get().strip(), self.pdf_rows, catalog_ids)
        linked_pdf = pdf_ids.intersection(catalog_ids)
        linked_pdf.update(str(pdf_id).lower() for pdf_id, target in aliases.items() if str(target).lower() in catalog_ids)
        return bool(pdf_ids - linked_pdf)

    def _clear_main_filters(self) -> None:
        self._main_color_filter.set(ALL_COLORS)
        self._main_description_filter.set("")

    def _refresh_main_color_options(self) -> None:
        colors = {self.table.set(row, "color") or UNKNOWN_COLOR for row in self._main_rows}
        options = (ALL_COLORS, *sorted(colors, key=str.casefold))
        self._main_color_options.configure(values=options)
        if self._main_color_filter.get() not in options:
            self._main_color_filter.set(ALL_COLORS)
        else:
            self._apply_main_filters()

    def _apply_main_filters(self, *_args) -> None:
        color = self._main_color_filter.get()
        description = self._main_description_filter.get().strip().casefold()
        wildcard = "*" in description or "?" in description
        visible = []
        for row in self._main_rows:
            if color != ALL_COLORS and (self.table.set(row, "color") or UNKNOWN_COLOR) != color:
                continue
            name = self.table.set(row, "description").casefold()
            matches_description = fnmatchcase(name, description) if wildcard else description in name
            if matches_description:
                visible.append(row)

        matching = set(visible)
        hidden = [row for row in self.table.get_children() if row not in matching]
        if hidden:
            self.table.selection_remove(*(row for row in self.table.selection() if row not in matching))
            self.table.detach(*hidden)
            if self.table.focus() in hidden:
                self.table.focus("")
        for row in visible:
            self.table.move(row, "", "end")
        self._main_filter_count.configure(text=f"{len(visible)} / {len(self._main_rows)}" if self._main_rows else "")
        self._schedule_thumbnails()

    def _clear_main_table(self) -> None:
        self.table.delete(*self._main_rows)
        self._main_rows.clear()
        self._thumb_rows.clear()
        self._thumb_photos.clear()

    def _sort_table(self, column: str) -> None:
        reverse = self.sort_directions.get(column, False)
        rows = [(self.table.set(item_id, column), item_id) for item_id in self._main_rows]

        def sort_key(row):
            value = row[0].strip()
            if column == "quantity":
                try:
                    return (0, int(value))
                except ValueError:
                    return (1, value.casefold())
            if column in ("part", "rb_part"):
                return tuple((0, int(piece)) if piece.isdigit() else (1, piece.casefold()) for piece in re.split(r"(\d+)", value))
            return value.casefold()

        self._main_rows[:] = [item_id for _, item_id in sorted(rows, key=sort_key, reverse=reverse)]
        self.sort_directions[column] = not reverse
        self._apply_main_filters()

    def delete_saved_set(self) -> None:
        if self._wait_count or getattr(self, "_set_loading", False):
            messagebox.showinfo("Set verwijderen", "Wacht tot de huidige verwerking klaar is.", parent=self)
            return
        set_number = self.set_entry.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9]+(?:-[0-9]+)?", set_number):
            messagebox.showinfo("Set verwijderen", "Vul eerst het LEGO-setnummer in.", parent=self)
            return
        root = APP_DIR / "downloads"
        folder = root / set_number
        try:
            if root.resolve() != root or folder.resolve() != folder or folder.is_symlink():
                raise OSError("De setmap verwijst naar een andere locatie.")
            if not folder.is_dir() or not any(folder.iterdir()):
                messagebox.showinfo("Set verwijderen", f"Voor set {set_number} zijn geen lokale gegevens opgeslagen.", parent=self)
                return
            independent_windows = (getattr(self, "_management_window", None),
                                   getattr(self, "_owned_sets_window", None),
                                   getattr(self, "_rental_history_window", None))
            if any(isinstance(widget, tk.Toplevel) and widget not in independent_windows
                   for widget in self.winfo_children()):
                messagebox.showinfo("Set verwijderen", "Sluit eerst de geopende detail-, controle- en registratievensters.", parent=self)
                return
            if not messagebox.askyesno(
                "Setgegevens verwijderen",
                f"Wil je de lokale gegevens van set {set_number} verwijderen?\n\n"
                "De setgegevens gaan naar de Windows-prullenbak, inclusief bouwinstructies, "
                "index, analyses, huurregistratie en bestanden die in de setmap zijn opgeslagen. "
                "Geleerde herkenningen, koppelingen, andere sets en back-ups blijven beschikbaar.",
                default=messagebox.NO, parent=self,
            ):
                return
            from lego_steps import reviewed_drawings
            reviewed_drawings(root, folder / "bouwstapcontroles.sqlite3", set(), KNOWLEDGE_DB)
            if (folder / "huurregistratie.sqlite3").is_file():
                from rental_inventory import initialize_rental_history
                initialize_rental_history(RENTAL_HISTORY_DB, root)
            if not recycle_set_folder(folder, self.winfo_id()):
                return
        except (OSError, sqlite3.Error) as exc:
            messagebox.showerror("Set verwijderen", str(exc), parent=self)
            return
        self._auto_lookup_signatures = {key for key in self._auto_lookup_signatures if key[0] != set_number}
        if self.active_set_number == set_number:
            self.active_set_number = ""
            self._clear_loaded_set()
            self.pdf_label.configure(text="Nog geen bouwinstructie geladen")
        result = f"De lokale gegevens van set {set_number} zijn naar de prullenbak verplaatst."
        if folder.exists():
            result += " Windows houdt de lege setmap nog open."
        self.status.set(result)

    def _clear_loaded_set(self):
        self._part_usage = None
        self._refresh_open_part_usage = None
        if hasattr(self, "_prefetch_stop"):
            self._prefetch_stop.set()
        viewer = getattr(self, "_part_detail_viewer", None)
        if viewer is not None and viewer.winfo_exists():
            viewer.destroy()
        chooser = getattr(self, "_pdf_chooser", None)
        if chooser is not None and chooser.winfo_exists():
            chooser.destroy()
        self.view_pdfs_button.configure(state="disabled")
        self.instruction_pdfs = []
        self.pdf_path = None
        self.pdf_rows = []
        self.catalog_records = []
        self._inventory_set = None
        self._clear_main_table()
        self._main_color_options.configure(values=(ALL_COLORS,))
        self._clear_main_filters()
        self.compare_button.configure(state="disabled")
        self.lookup_button.configure(state="disabled")

    def open_instructions(self, *, on_complete=None) -> None:
        if self._wait_count or getattr(self, "_set_loading", False):
            if on_complete:
                on_complete(False)
            return
        set_number = self.set_entry.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9]+(?:-[0-9]+)?", set_number):
            messagebox.showerror("Setnummer ontbreekt", "Vul een geldig LEGO-setnummer in, bijvoorbeeld 42115.")
            if on_complete:
                on_complete(False)
            return
        self.active_set_number = set_number
        self._set_loading = True
        self._set_load_callback = on_complete
        self._clear_loaded_set()
        self.download_button.configure(state="disabled")
        self.set_entry.configure(state="disabled")
        self.pdf_label.configure(text="Bouwinstructie laden…")
        self.status.set(f"Set {set_number}: bouwinstructie laden…")
        self._begin_wait()
        threading.Thread(target=self._download_instructions, args=(set_number,), daemon=True).start()

    def _download_instructions(self, set_number: str) -> None:
        try:
            paths = download_official_instructions(set_number)
            self.after(0, self._downloads_ready, paths)
        except Exception as error:
            detail = f"{type(error).__name__}: {error!r}"
            self.after(0, self._download_failed, detail)

    def _unlock_set_entry(self, *, index_available=False):
        self._set_loading = False
        self.download_button.configure(state="normal")
        self.set_entry.configure(state="normal")
        callback = getattr(self, "_set_load_callback", None)
        self._set_load_callback = None
        if callback:
            callback(index_available)

    def _analyze_loaded_set(self):
        self.status.set("Hoofdindex gecontroleerd. Bouwstappen worden geladen…")
        pdf_rows = self.pdf_rows

        def finished(usage, error):
            if error:
                self.status.set(f"Fout: de onderdelenlijst is geladen. Bouwstappen konden niet worden verwerkt. {error}")
            else:
                self._part_usage = (pdf_rows, usage)
                self._update_set_comparison_status()
            self._unlock_set_entry(index_available=True)

        self.open_steps(usage_only=True, usage_callback=finished)

    def _collection_difference_count(self, set_number):
        from lego_steps import comparison_counts, saved_step_usage

        if not re.fullmatch(r"[A-Za-z0-9]+(?:-[0-9]+)?", set_number):
            return None
        folder = APP_DIR / "downloads" / set_number
        review_name = "bouwstapcontroles.sqlite3"
        try:
            cached = getattr(self, "_part_usage", None)
            if set_number == self.active_set_number and cached and cached[0] is self.pdf_rows:
                index_rows, usage = cached
            else:
                usage = saved_step_usage(folder / review_name)
                if usage is None:
                    return None
                index = json.loads((folder / "parts_index_cache.json").read_text(encoding="utf-8"))
                files = [Path(item["path"]) for item in index["pdfs"]]
                if not files or index["pdfs"] != self._pdf_signature(files):
                    return None
                rows = [(str(number), int(quantity)) for number, quantity in index["rows"]]
                aliases = self._recognition_aliases(set_number, rows)
                excluded = {number for number, record in local_parts(rows, aliases).items()
                            if is_brick_separator(record)}
                index_rows = building_index_rows(rows, excluded, aliases)
            explanations = index_explanations(KNOWLEDGE_DB, f"{set_number}/{review_name}")
            return comparison_counts(dict(index_rows), usage, explanations)[0] if index_rows else None
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, AttributeError):
            return None

    def _update_set_comparison_status(self, _event=None):
        from lego_steps import comparison_counts

        cached = getattr(self, "_part_usage", None)
        self.event_generate("<<SetComparisonUpdated>>")
        if not cached or cached[0] is not self.pdf_rows:
            return
        usage = cached[1]
        source = f"{self.active_set_number}/bouwstapcontroles.sqlite3"
        explanations = index_explanations(KNOWLEDGE_DB, source)
        open_count, explained_count = comparison_counts(dict(self.pdf_rows), usage, explanations)
        message = f"Set {self.active_set_number} geladen. Hoofdindex en bouwstappen per onderdeel zijn lokaal beschikbaar."
        if open_count:
            message += f" Openstaande verschillen: {open_count}."
        if explained_count:
            message += f" Verklaarde verschillen: {explained_count}."
        self.status.set(message)

    def _bring_to_front(self) -> None:
        # Request the foreground once without pinning the app above other windows.
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
        except tk.TclError:
            pass

    def _download_failed(self, detail: str) -> None:
        self._end_wait()
        self.status.set(f"Automatisch downloaden lukte niet: {detail}")
        self._bring_to_front()
        messagebox.showerror("PDF downloaden mislukt", f"De bouwinstructie kon niet worden geladen.\n\n{detail}", parent=self)
        self._unlock_set_entry()

    def _downloads_ready(self, paths: list[Path]) -> None:
        self.instruction_pdfs = list(paths)
        self.view_pdfs_button.configure(state="normal" if paths else "disabled")
        self.status.set(f"{len(paths)} instructie-PDF('s) beschikbaar. PDF's worden doorzocht op de onderdelenindex…")
        self._bring_to_front()
        threading.Thread(target=self._find_index_pdf, args=(paths,), daemon=True).start()

    def open_instruction_pdfs(self) -> None:
        booklets = [(number, path) for number, path in enumerate(self.instruction_pdfs, 1) if path.is_file()]
        if not booklets:
            messagebox.showinfo("Bouwinstructies", "Er zijn geen PDF’s beschikbaar voor deze set.", parent=self)
            return
        if len(booklets) == 1:
            self._view_instruction_pdf(booklets[0][1], self)
            return

        chooser = getattr(self, "_pdf_chooser", None)
        if chooser is not None and chooser.winfo_exists():
            chooser.lift()
            return
        chooser = self._pdf_chooser = tk.Toplevel(self)
        chooser.title("Bouwinstructies")
        chooser.transient(self)
        chooser.bind("<Escape>", lambda _event: chooser.destroy())
        panel = ttk.Frame(chooser, padding=16)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text=f"Kies een boekje van set {self.active_set_number}.").pack(anchor="w")
        selection = ttk.Combobox(panel, state="readonly", width=44,
                                 values=[f"Boekje {number} · {path.name}" for number, path in booklets])
        selection.pack(fill="x", pady=(6, 12))
        selection.current(0)

        def open_selected(_event=None):
            index = selection.current()
            if index >= 0:
                self._view_instruction_pdf(booklets[index][1], chooser)

        selection.bind("<Return>", open_selected)
        selection.focus_set()
        ttk.Button(panel, text="Open PDF", command=open_selected).pack(anchor="e")

    @staticmethod
    def _view_instruction_pdf(path: Path, parent) -> None:
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror("PDF openen mislukt", f"{path.name} kon niet worden geopend.\n\n{exc}", parent=parent)

    def _find_index_pdf(self, paths: list[Path]) -> None:
        """Choose the booklet whose contents yield the most index part rows."""
        try:
            cached = self._load_parts_index_cache(paths)
            if cached is not None:
                selected_path, cached_rows = cached
                self.after(0, self._index_pdf_found, selected_path, len(cached_rows), cached_rows)
                return
            candidates = [(self._read_parts_index(path), path) for path in paths]
            if not candidates:
                raise ValueError("LEGO heeft geen instructie-PDF's teruggegeven.")
            rows, selected = max(candidates, key=lambda candidate: len(candidate[0]))
            if not rows:
                names = ", ".join(path.name for path in paths)
                raise ValueError(
                    "Ik heb alle gedownloade PDF's doorzocht, maar kon in geen ervan "
                    "tekstuele onderdelenregels herkennen. Mogelijk is de index als afbeelding opgenomen. "
                    f"Doorzochte bestanden: {names}."
                )
            self._save_parts_index_cache(paths, selected, rows)
            self.after(0, self._index_pdf_found, selected, len(rows), rows)
        except Exception as error:
            detail = str(error)
            self.after(0, self._index_pdf_failed, detail)

    def _index_pdf_found(self, path: Path, count: int, cached_rows: list[tuple[str, int]] | None = None) -> None:
        self.pdf_path = path
        self.pdf_label.configure(text=f"PDF met onderdelenindex: {path}")
        if cached_rows is not None:
            self.status.set(f"Opgeslagen onderdelenindex geladen uit {path.name} ({count} regels).")
        else:
            self.status.set(f"Onderdelenindex gevonden in {path.name} ({count} regels). Onderdelen worden ingelezen…")
        threading.Thread(target=self._extract_and_enrich, args=(cached_rows,), daemon=True).start()

    def _index_pdf_failed(self, detail: str) -> None:
        self._end_wait()
        self.status.set("Onderdelenindex niet automatisch gevonden.")
        self._bring_to_front()
        messagebox.showerror("Onderdelenindex niet gevonden", detail, parent=self)
        self._unlock_set_entry()

    def save_customer_name(self) -> None:
        self.config_data["customer_name"] = self.customer_entry.get().strip()
        save_config(self.config_data)
        self._settings_feedback.set("Klantnaam lokaal opgeslagen.")

    def save_return_postcode(self) -> None:
        postcode = self.return_postcode.get().strip()
        self.config_data["return_postcode"] = postcode
        save_config(self.config_data)
        self._settings_feedback.set("Postcode lokaal opgeslagen." if postcode else "Opgeslagen postcode gewist.")

    def save_key(self) -> None:
        key = self.key_entry.get().strip()
        self.config_data["rebrickable_api_key"] = key
        save_config(self.config_data)
        self._settings_feedback.set("API-sleutel lokaal opgeslagen.")

    def _extract_and_enrich(self, cached_extracted: list[tuple[str, int]] | None = None) -> None:
        try:
            extracted = cached_extracted
            if extracted is None:
                extracted = self._read_parts_index(self.pdf_path)
                if extracted:
                    pdf_paths = [path for path in self.instruction_pdfs if path.exists()] or [self.pdf_path]
                    self._save_parts_index_cache(pdf_paths, self.pdf_path, extracted)
            if not extracted:
                raise ValueError("Geen onderdeelregels als tekst gevonden. Deze PDF heeft mogelijk een afbeelding als onderdelenlijst; OCR-ondersteuning volgt later.")
            key = self.key_entry.get().strip()
            set_number = self.active_set_number
            catalog_cache = self._analysis_cache_path("rebrickable_inventory.json")
            if catalog_cache:
                try:
                    records = json.loads(catalog_cache.read_text(encoding="utf-8"))
                    if isinstance(records, list) and records and all(isinstance(record, dict) for record in records):
                        self.after(0, self._show_catalog_results, records, extracted)
                        return
                except (OSError, ValueError):
                    pass
            aliases = self._recognition_aliases(set_number, extracted)
            known = local_parts(extracted, self.part_aliases.get(set_number, {}))
            known.update(local_parts([(n, qty) for n, qty in extracted if n not in known], aliases))
            if key and len(known) < len(extracted):
                try:
                    records = inventory_for_set(set_number, key)
                    if records:
                        remember_parts(records)
                        if catalog_cache:
                            try:
                                catalog_cache.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
                            except OSError:
                                pass
                        self.after(0, self._show_catalog_results, records, extracted)
                        return
                except (OSError, ValueError):
                    pass
            self.after(0, self._show_results, extracted, known)
        except Exception as error:
            self.after(0, self.status.set, f"Fout: {error}")
            error_message = str(error)
            self.after(0, self._parts_read_failed, error_message)

    def _parts_read_failed(self, detail: str) -> None:
        self._end_wait()
        messagebox.showerror("Onderdelenlijst lezen", detail, parent=self)
        self._unlock_set_entry()

    @staticmethod
    def _read_parts_index(path: Path) -> list[tuple[str, int]]:
        import pymupdf
        from lego_pdf_text import index_part_labels

        totals = Counter()
        with pymupdf.open(path) as document:
            for page in document:
                fallback = dict(LegoPartsApp._parse_lines(page.get_text().splitlines()))
                if not fallback:
                    continue
                positioned = Counter()
                for number, quantity, _, _ in index_part_labels(page):
                    positioned[number] += quantity
                # Coordinate matching deduplicates coincident text layers. Keep
                # line parsing for layouts with a different label arrangement.
                missing = fallback.keys() - positioned.keys()
                if len(positioned) >= 2 and all(
                        len(number) == 6 and sum(positioned[number + d] == fallback[number]
                                                 for d in "0123456789") == 1
                        for number in missing):
                    totals.update(positioned)
                else:
                    totals.update(fallback)
        if totals and "4184108" not in totals:
            # LEGO Pick a Brick and the five drawings for set 77243 agree.
            # The printed index in this edition omits this dark blue 1 x 1 plate.
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == (
                        "1e6509db14aef30b40a754c65a19e3484d244fa145917d9abc4e5d82cf631159"):
                    totals["4184108"] = 5
        return list(totals.items())

    @staticmethod
    def _parse_lines(lines: list[str]) -> list[tuple[str, int]]:
        # LEGO's inventory table puts quantity before the element number, often on
        # separate lines (e.g. "3x" followed by "6130007"). Some PDF extractors
        # also put both values on one line. Never infer quantity from the line
        # after an ID: that is often the printed page number at column boundaries.
        output: dict[str, int] = {}
        normalized = [" ".join(raw.split()) for raw in lines]
        for index, line in enumerate(normalized):
            if not line or line == "---PAGE BREAK---":
                continue
            # Inventory element IDs in LEGO PDFs are at least six digits.
            # Short values such as 1111 can be adjacent step-number text, not
            # a truncated ID (the actual element may be 4211111 elsewhere).
            match = re.search(r"\b(\d{1,3})\s*[x×]\s*(\d{6,7}[a-z]?)\b", line, re.I)
            if match:
                count, part_num = int(match.group(1)), match.group(2)
            else:
                # Also accept the reversed order in other instruction layouts.
                reversed_match = re.search(r"\b(\d{6,7}[a-z]?)\s*[x×]\s*(\d{1,3})\b", line, re.I)
                if reversed_match:
                    part_num, count = reversed_match.group(1), int(reversed_match.group(2))
                else:
                    quantity_match = re.fullmatch(r"(\d{1,3})\s*[x×]", line, re.I)
                    if not quantity_match:
                        continue
                    next_index = index + 1
                    while next_index < len(normalized) and not normalized[next_index]:
                        next_index += 1
                    if next_index >= len(normalized) or normalized[next_index] == "---PAGE BREAK---":
                        continue
                    part_match = re.fullmatch(r"(\d{6,7}[a-z]?)", normalized[next_index], re.I)
                    if not part_match:
                        continue
                    count, part_num = int(quantity_match.group(1)), part_match.group(1)
            normalized_id = part_num.lower()
            output[normalized_id] = output.get(normalized_id, 0) + count
        return list(output.items())

    def _show_results(self, extracted: list[tuple[str, int]], descriptions: dict) -> None:
        self._end_wait()
        self._clear_main_table()
        excluded = {number for number, record in descriptions.items() if is_brick_separator(record)}
        extracted = building_index_rows(extracted, excluded, self.part_aliases.get(self.active_set_number, {}))
        self.pdf_rows = extracted
        self._inventory_set = self.active_set_number
        self.catalog_records = []
        self.compare_button.configure(state="disabled")
        self.lookup_button.configure(state="disabled")
        for number, count in extracted:
            record = descriptions.get(number, {})
            part = record.get("part", {})
            name = part.get("name", "Omschrijving niet gevonden")
            color = record.get("color", {}).get("name", "")
            row = self.table.insert("", "end", values=(number, part.get("part_num", ""), name, color, count,
                                    "PDF + lokale database" if record else "PDF"), tags=() if record else ("missing",))
            self._main_rows.append(row)
            self._register_thumbnail(row, record.get("element_id", number), (record.get("element_img_url"),))
        self._refresh_main_color_options()
        self._prefetch_pictures()
        self.status.set(f"{len(extracted)} onderdelen gelezen. {sum(n in descriptions for n, _ in extracted)} omschrijvingen uit de lokale database.")
        self._bring_to_front()
        if getattr(self, "_set_loading", False):
            self._analyze_loaded_set()

    def _show_catalog_results(self, records: list[dict], pdf_rows: list[tuple[str, int]], auto_compare: bool = True) -> None:
        """Use the set inventory as the complete, structured list when available."""
        records = list(records)
        self._inventory_set = self.active_set_number
        pdf_ids_for_cache = {str(number).strip().lower() for number, _quantity in pdf_rows}
        current_element_ids = {str(entry.get("element_id") or "").strip().lower() for entry in records}
        for cached in self._load_set_element_lookup_results(self.set_entry.get().strip(), pdf_ids_for_cache):
            cached_id = str(cached.get("element_id") or "").strip().lower()
            if cached_id not in current_element_ids:
                records.append(cached)
                current_element_ids.add(cached_id)
        set_aliases = self._recognition_aliases(self.set_entry.get().strip(), pdf_rows,
                                               self._catalog_element_ids(records))
        excluded = {str(entry.get("element_id") or entry["part"].get("part_num")) for entry in records
                    if is_brick_separator(entry)}
        excluded.update(number for number, record in local_parts(pdf_rows, set_aliases).items()
                        if is_brick_separator(record))
        pdf_rows = building_index_rows(pdf_rows, excluded, set_aliases)
        self.pdf_rows = pdf_rows
        records = [entry for entry in records if not is_brick_separator(entry)]
        self.catalog_records = records
        self.compare_button.configure(state="normal")
        self.lookup_button.configure(state="normal")
        self._clear_main_table()
        pdf_ids = {str(number).strip().lower() for number, _quantity in pdf_rows}
        element_lookup_ids = {str(entry.get("element_id") or "").strip().lower() for entry in records if entry.get("_element_lookup")}
        group_urls = {}
        design_colors = {}
        grouped = {}
        for entry in records:
            part = entry.get("part", {})
            color = entry.get("color", {})
            element_id = str(entry.get("element_id") or "").strip()
            part_number = str(part.get("part_num") or "").strip()
            display_number = element_id or part_number or "Onbekend"
            name = part.get("name", "Onbekend onderdeel")
            color_name = color.get("name", "")
            group_key = (display_number, part_number, name, color_name)
            design_colors[group_key] = None if element_id else color_name
            group_urls.setdefault(group_key, []).extend((entry.get("element_img_url"), part.get("part_img_url")))
            if group_key not in grouped:
                grouped[group_key] = 0
            grouped[group_key] += int(entry.get("quantity") or 0)
        catalog_ids = {key[0].lower() for key in grouped}
        matched_pdf_ids = pdf_ids.intersection(catalog_ids) | {pdf_id.lower() for pdf_id, target in set_aliases.items() if target.lower() in catalog_ids}
        pdf_match_count = sum(1 for number, _qty in pdf_rows if number.strip().lower() in matched_pdf_ids)
        pdf_only = [(number, quantity) for number, quantity in pdf_rows if number.strip().lower() not in matched_pdf_ids]
        linked_catalog_ids = pdf_ids.intersection(catalog_ids) | {target.lower() for pdf_id, target in set_aliases.items() if pdf_id.lower() in pdf_ids and target.lower() in catalog_ids}
        catalog_only_count = len(catalog_ids - linked_catalog_ids)
        verified = bool(pdf_ids) and not pdf_only
        self.compare_button.configure(state="disabled" if verified else "normal")
        visible_groups = {key: qty for key, qty in grouped.items()
                          if not verified or key[0].lower() in linked_catalog_ids}
        for (number, part_number, name, color), quantity in sorted(visible_groups.items(), key=lambda row: (row[0][3], row[0][2], row[0][0])):
            source = "Rebrickable"
            if number.lower() in linked_catalog_ids:
                source = "PDF + Rebrickable (elementzoeking)" if number.lower() in element_lookup_ids else "PDF + Rebrickable"
            row = self.table.insert("", "end", values=(number, part_number, name, color, quantity, source))
            self._main_rows.append(row)
            self._register_thumbnail(row, number, group_urls[(number, part_number, name, color)], design_colors[(number, part_number, name, color)])
        for number, quantity in pdf_only:
            row = self.table.insert("", "end", values=(number, "", "Geen match in Rebrickable", "", quantity, "Alleen in PDF"), tags=("missing",))
            self._main_rows.append(row)
            self._register_thumbnail(row, number)
        self._refresh_main_color_options()
        self._prefetch_pictures()
        total_pieces = sum(visible_groups.values())
        self.status.set(
            f"Onderdelenlijst: {len(visible_groups)} verschillende onderdelen, {total_pieces} stuks. "
            f"PDF: {len(pdf_rows)} unieke elementnummers; {pdf_match_count} gekoppeld, {len(pdf_only)} alleen in PDF, {catalog_only_count} alleen in Rebrickable"
            + (" (weggelaten)." if verified and catalog_only_count else ".")
        )
        self._bring_to_front()
        if auto_compare and pdf_only:
            api_key = self.key_entry.get().strip()
            signature = (self.set_entry.get().strip(), tuple(sorted((str(number).strip(), int(quantity)) for number, quantity in pdf_rows)))
            if pdf_only and api_key and signature not in self._auto_lookup_signatures and not self._lookup_in_progress:
                self._auto_lookup_signatures.add(signature)
                self._lookup_in_progress = True
                self.lookup_button.configure(state="disabled")
                self.status.set(f"Lijsten vergeleken. Rebrickable zoekt nu automatisch {len(pdf_only)} ontbrekende PDF-elementnummers op vóór de visuele controle…")
                threading.Thread(target=self._lookup_unmatched_pdf_parts_worker, args=(pdf_only, api_key), daemon=True).start()
                return
            if getattr(self, "_set_loading", False):
                self._end_wait()
                self._analyze_loaded_set()
            else:
                self.after(150, self.open_visual_comparison)
            return
        if auto_compare:
            self._end_wait()
            if getattr(self, "_set_loading", False):
                self._analyze_loaded_set()

    def _lookup_pdf_element_color(self, element_id: str, tree: ttk.Treeview, item_id: str, label: ttk.Label, win: tk.Toplevel) -> None:
        cached = self.element_color_cache.get(element_id)
        if cached is not None:
            tree.set(item_id, "color", cached)
            if win._selected_pdf and win._selected_pdf["id"] == element_id:
                label.configure(text=f"LEGO-elementkleur (Rebrickable): {cached}")
            return
        api_key = self.key_entry.get().strip()
        if not api_key:
            tree.set(item_id, "color", "API-sleutel nodig")
            if win._selected_pdf and win._selected_pdf["id"] == element_id:
                label.configure(text="LEGO-elementkleur: vul de Rebrickable API-sleutel in")
            return
        if element_id in self._element_lookups_in_progress:
            return
        self._element_lookups_in_progress.add(element_id)
        win._pending_color_lookups = getattr(win, "_pending_color_lookups", 0) + 1
        win.configure(cursor="watch")
        tree.set(item_id, "color", "Kleur ophalen…")
        if win._selected_pdf and win._selected_pdf["id"] == element_id:
            label.configure(text="LEGO-elementkleur wordt bij Rebrickable opgezocht…")

        def lookup() -> None:
            try:
                # Rebrickable allows about one request per second on average.
                with self._element_lookup_lock:
                    delay = 1.1 - (time.monotonic() - self._element_last_request)
                    if delay > 0:
                        time.sleep(delay)
                    self._element_last_request = time.monotonic()
                    url = f"{API_ROOT}/elements/{urllib.parse.quote(element_id, safe='')}/"
                    details = request_json(url, api_key)
                color = details.get("color", {}).get("name") or "Kleur niet beschikbaar"
            except urllib.error.HTTPError as error:
                color = "Onbekend (geen Rebrickable-match)" if error.code == 404 else f"Rebrickable HTTP {error.code}"
            except Exception as error:
                color = f"Kleur niet opgehaald ({type(error).__name__})"
            self.after(0, self._finish_element_color_lookup, element_id, item_id, color, tree, label, win)

        threading.Thread(target=lookup, daemon=True).start()

    def _finish_element_color_lookup(self, element_id: str, item_id: str, color: str, tree: ttk.Treeview, label: ttk.Label, win: tk.Toplevel) -> None:
        self._element_lookups_in_progress.discard(element_id)
        if not color.startswith(("Rebrickable HTTP", "Kleur niet opgehaald")):
            self.element_color_cache[element_id] = color
            save_config(self.config_data)
        if not win.winfo_exists():
            return
        win._pending_color_lookups = max(0, getattr(win, "_pending_color_lookups", 1) - 1)
        if not win._pending_color_lookups:
            win.configure(cursor="")
        try:
            tree.set(item_id, "color", color)
        except tk.TclError:
            pass
        if win._selected_pdf and win._selected_pdf["id"] == element_id:
            label.configure(text=f"LEGO-elementkleur (Rebrickable): {color}")

    def open_visual_comparison(self) -> None:
        if getattr(self, "_set_loading", False):
            return
        self._end_wait()
        if not self.pdf_path or not self.pdf_rows or not self.catalog_records:
            messagebox.showinfo("Visuele controle", "Lees eerst een instructie-PDF en laad de Rebrickable-inventaris.", parent=self)
            return
        try:
            import pymupdf as fitz
            from PIL import Image, ImageTk
        except ImportError:
            messagebox.showerror("Modules ontbreken", "Installeer de vereisten met: pip install -r requirements.txt", parent=self)
            return

        win = tk.Toplevel(self)
        win.title("Onderdeel visueel vergelijken")
        win.geometry("1040x700")
        win.transient(self)
        comparison_header = ttk.Label(win, text="Selecteer links een LEGO-elementnummer en rechts een Rebrickable-onderdeel.", wraplength=980)
        comparison_header.pack(anchor="w", padx=14, pady=(14, 8))
        panes = ttk.Frame(win, padding=(14, 0, 14, 8))
        panes.pack(fill="both", expand=True)
        left = ttk.LabelFrame(panes, text="LEGO PDF")
        right = ttk.LabelFrame(panes, text="Rebrickable")
        left.pack(side="left", fill="both", expand=True, padx=(0, 7))
        right.pack(side="left", fill="both", expand=True, padx=(7, 0))

        set_aliases = self._recognition_aliases(self.set_entry.get().strip(), self.pdf_rows,
                                               self._catalog_element_ids(self.catalog_records))
        pdf_ids = {str(number).strip().lower() for number, _quantity in self.pdf_rows}
        catalog_parts = {}
        for entry in self.catalog_records:
            part = entry.get("part", {})
            color = entry.get("color", {})
            element_id = str(entry.get("element_id") or "").strip()
            part_number = str(part.get("part_num") or "").strip()
            identifier = element_id or part_number or "Onbekend"
            record = catalog_parts.setdefault(identifier, {
                "id": identifier, "part_number": part_number, "element_id": element_id,
                "name": part.get("name", "Onbekend onderdeel"), "color": color.get("name", ""),
                "quantity": 0, "image_url": entry.get("element_img_url") or part.get("part_img_url") or "",
            })
            record["quantity"] += int(entry.get("quantity") or 0)
        all_catalog_parts = list(catalog_parts.values())
        catalog_ids = {identifier.lower() for identifier in catalog_parts}
        linked_pdf_ids = pdf_ids.intersection(catalog_ids)
        linked_pdf_ids.update(pdf_id.lower() for pdf_id, target in set_aliases.items() if target.lower() in catalog_ids)
        linked_catalog_ids = pdf_ids.intersection(catalog_ids)
        linked_catalog_ids.update(target.lower() for pdf_id, target in set_aliases.items() if pdf_id.lower() in pdf_ids and target.lower() in catalog_ids)
        pdf_parts = [
            {"id": number, "quantity": quantity}
            for number, quantity in self.pdf_rows
            if number.strip().lower() not in linked_pdf_ids
        ]
        catalog_parts = [item for item in catalog_parts.values() if item["id"].lower() not in linked_catalog_ids]
        comparison_header.configure(text=f"Nog niet gekoppeld: {len(pdf_parts)} LEGO-elementen en {len(catalog_parts)} Rebrickable-onderdelen. Vergelijk vorm én kleur.")

        def update_comparison_header() -> None:
            comparison_header.configure(text=f"Nog niet gekoppeld: {len(pdf_tree.get_children(''))} LEGO-elementen en {len(catalog_tree.get_children(''))} Rebrickable-onderdelen. Vergelijk vorm én kleur.")

        pdf_list_frame = ttk.Frame(left)
        pdf_list_frame.pack(fill="x", padx=8, pady=8)
        pdf_tree = ttk.Treeview(pdf_list_frame, columns=("id", "qty", "color"), show="headings", height=11, selectmode="browse")
        pdf_tree.heading("id", text="LEGO-elementnummer")
        pdf_tree.heading("qty", text="Aantal")
        pdf_tree.heading("color", text="Rebrickable-kleur/status")
        pdf_tree.column("id", width=155)
        pdf_tree.column("qty", width=60, anchor="center")
        pdf_tree.column("color", width=150)
        pdf_tree.pack(side="left", fill="x", expand=True)
        pdf_scroll = ttk.Scrollbar(pdf_list_frame, orient="vertical", command=pdf_tree.yview)
        pdf_scroll.pack(side="right", fill="y")
        pdf_tree.configure(yscrollcommand=pdf_scroll.set)
        for index, item in enumerate(pdf_parts):
            initial_color = self.element_color_cache.get(item["id"], "Kleur ophalen…")
            pdf_tree.insert("", "end", iid=str(index), values=(item["id"], item["quantity"], initial_color))

        catalog_list_frame = ttk.Frame(right)
        catalog_list_frame.pack(fill="x", padx=8, pady=8)
        catalog_tree = ttk.Treeview(catalog_list_frame, columns=("id", "rb_number", "name", "color", "qty"), show="headings", height=11, selectmode="browse")
        for key, title, width in (("id", "LEGO-elementnr.", 125), ("rb_number", "Rebrickable-nr.", 115), ("name", "Omschrijving", 210), ("color", "Kleur", 100), ("qty", "Aantal", 55)):
            catalog_tree.heading(key, text=title, command=lambda column=key: sort_compare_tree(catalog_tree, column))
            catalog_tree.column(key, width=width, anchor="w")
        catalog_tree.pack(side="left", fill="x", expand=True)
        catalog_scroll = ttk.Scrollbar(catalog_list_frame, orient="vertical", command=catalog_tree.yview)
        catalog_scroll.pack(side="right", fill="y")
        catalog_tree.configure(yscrollcommand=catalog_scroll.set)
        catalog_horizontal = ttk.Scrollbar(right, orient="horizontal", command=catalog_tree.xview)
        catalog_horizontal.pack(fill="x", padx=8)
        catalog_tree.configure(xscrollcommand=catalog_horizontal.set)
        for index, item in enumerate(catalog_parts):
            catalog_tree.insert("", "end", iid=str(index), values=(item["element_id"], item["part_number"], item["name"], item["color"], item["quantity"]))

        compare_sort_directions = {"pdf": {}, "catalog": {}}

        def sort_compare_tree(tree: ttk.Treeview, column: str) -> None:
            tree_name = "pdf" if tree is pdf_tree else "catalog"
            reverse = compare_sort_directions[tree_name].get(column, False)
            rows = [(tree.set(item_id, column), item_id) for item_id in tree.get_children("")]

            def sort_key(row):
                value = row[0].strip()
                if column == "qty":
                    try:
                        return (0, int(value))
                    except ValueError:
                        return (1, value.casefold())
                if column in ("id", "rb_number"):
                    return tuple((0, int(piece)) if piece.isdigit() else (1, piece.casefold()) for piece in re.split(r"(\d+)", value))
                return value.casefold()

            for index, (_value, item_id) in enumerate(sorted(rows, key=sort_key, reverse=reverse)):
                tree.move(item_id, "", index)
            compare_sort_directions[tree_name][column] = not reverse

        pdf_tree.heading("id", command=lambda: sort_compare_tree(pdf_tree, "id"))
        pdf_tree.heading("qty", command=lambda: sort_compare_tree(pdf_tree, "qty"))
        pdf_tree.heading("color", command=lambda: sort_compare_tree(pdf_tree, "color"))

        pdf_preview = ttk.Label(left, text="Kies een PDF-onderdeel" if pdf_parts else "Geen ongekoppelde PDF-onderdelen", anchor="center")
        pdf_preview.pack(fill="both", expand=True, padx=10, pady=8)
        pdf_color_label = ttk.Label(left, text="PDF-kleurindicatie: —", anchor="center")
        pdf_color_label.pack(fill="x", padx=10, pady=(0, 8))
        catalog_preview = ttk.Label(right, text="Kies een Rebrickable-onderdeel" if catalog_parts else "Geen ongekoppelde Rebrickable-onderdelen", anchor="center")
        catalog_preview.pack(fill="both", expand=True, padx=10, pady=8)
        ttk.Button(left, text="Zoom PDF-afbeelding…", command=lambda: open_zoom("pdf", "PDF-onderdeel")).pack(anchor="e", padx=10, pady=(0, 8))
        ttk.Button(right, text="Zoom Rebrickable-afbeelding…", command=lambda: open_zoom("catalog", "Rebrickable-onderdeel")).pack(anchor="e", padx=10, pady=(0, 8))
        win._image_refs = {}
        win._source_images = {}
        win._selected_pdf = None
        win._selected_catalog = None
        image_boxes_cache = {}

        def pdf_part_crop(identifier: str, doc) -> bytes | None:
            for page_index in range(max(0, len(doc) - 12), len(doc)):
                page = doc[page_index]
                matches = page.search_for(identifier)
                if not matches:
                    continue
                rect = matches[0]
                if page_index not in image_boxes_cache:
                    image_boxes_cache[page_index] = [fitz.Rect(*info["bbox"]) for info in page.get_image_info(xrefs=True) if info.get("bbox")]
                candidates = []
                text_center_x = (rect.x0 + rect.x1) / 2
                for image_box in image_boxes_cache[page_index]:
                    vertical_gap = rect.y0 - image_box.y1
                    horizontal_gap = max(image_box.x0 - text_center_x, 0, text_center_x - image_box.x1)
                    if -2 <= vertical_gap <= 70 and horizontal_gap <= 45:
                        candidates.append((vertical_gap + horizontal_gap * 1.5, image_box))
                if not candidates:
                    continue
                _score, image_box = min(candidates, key=lambda candidate: candidate[0])
                clip = fitz.Rect(max(0, image_box.x0 - 3), max(0, image_box.y0 - 3), min(page.rect.width, image_box.x1 + 3), min(page.rect.height, image_box.y1 + 3))
                return page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False).tobytes("png")
            return None

        def show_pdf(_event=None):
            selected = pdf_tree.selection()
            if not selected:
                return
            row = pdf_parts[int(selected[0])]
            win._selected_pdf = row
            pdf_preview.configure(text="PDF-afbeelding laden…", image="")
            pdf_color_label.configure(text=f"LEGO-elementkleur (Rebrickable): {pdf_tree.set(selected[0], 'color')}")
            self._lookup_pdf_element_color(row["id"], pdf_tree, selected[0], pdf_color_label, win)
            try:
                with fitz.open(self.pdf_path) as doc:
                    crop_bytes = pdf_part_crop(row["id"], doc)
                if crop_bytes is None:
                    pdf_preview.configure(text=f"Geen afbeelding gevonden voor {row['id']}", image="")
                    return
                image = Image.open(BytesIO(crop_bytes)).convert("RGB")
                win._source_images["pdf"] = image.copy()
                display_image = image.copy()
                scale = min(3.5, 300 / max(display_image.width, 1), 260 / max(display_image.height, 1))
                if scale != 1:
                    display_image = display_image.resize((max(1, int(display_image.width * scale)), max(1, int(display_image.height * scale))), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(display_image)
                win._image_refs["pdf"] = photo
                pdf_preview.configure(text=f"LEGO {row['id']}  ·  {row['quantity']}x", image=photo, compound="top")
            except Exception as error:
                pdf_preview.configure(text=f"PDF-afbeelding niet beschikbaar: {error}", image="")

        def show_catalog(_event=None):
            selected = catalog_tree.selection()
            if not selected:
                return
            row = catalog_parts[int(selected[0])]
            win._selected_catalog = row
            catalog_preview.configure(text="Rebrickable-afbeelding laden…", image="")
            try:
                url = row["image_url"]
                if not url and row["element_id"]:
                    url = f"https://cdn.rebrickable.com/media/parts/elements/{row['element_id']}.jpg"
                if not url:
                    raise ValueError("Geen catalogusafbeelding beschikbaar")
                key = self._picture_key(row["id"], [url], None if row["element_id"] else row["color"])
                image = self._load_part_picture(key, None) if key else None
                if image is None:
                    raise ValueError("Geen catalogusafbeelding beschikbaar")
                win._source_images["catalog"] = image.copy()
                display_image = image.copy()
                scale = min(3.5, 300 / max(display_image.width, 1), 260 / max(display_image.height, 1))
                if scale != 1:
                    display_image = display_image.resize((max(1, int(display_image.width * scale)), max(1, int(display_image.height * scale))), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(display_image)
                win._image_refs["catalog"] = photo
                catalog_preview.configure(text=f"LEGO {row['element_id']}  ·  Rebrickable {row['part_number']}  ·  Aantal: {row['quantity']}\n{row['name']}  ·  {row['color']}", image=photo, compound="top")
            except Exception as error:
                catalog_preview.configure(text=f"Catalogusafbeelding niet beschikbaar: {error}", image="")

        def open_zoom(side: str, title: str) -> None:
            source = win._source_images.get(side)
            if source is None:
                messagebox.showinfo("Afbeelding ontbreekt", "Selecteer eerst een onderdeel zodat de afbeelding geladen wordt.", parent=win)
                return
            zoom_window = tk.Toplevel(win)
            zoom_window.title(f"Zoom: {title}")
            zoom_window.geometry("760x680")
            zoom_window.transient(win)
            canvas_frame = ttk.Frame(zoom_window)
            canvas_frame.pack(fill="both", expand=True, padx=10, pady=10)
            canvas = tk.Canvas(canvas_frame, background="#eeeeee", highlightthickness=0)
            horizontal = ttk.Scrollbar(canvas_frame, orient="horizontal", command=canvas.xview)
            vertical = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
            canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
            canvas.grid(row=0, column=0, sticky="nsew")
            vertical.grid(row=0, column=1, sticky="ns")
            horizontal.grid(row=1, column=0, sticky="ew")
            canvas_frame.rowconfigure(0, weight=1)
            canvas_frame.columnconfigure(0, weight=1)
            max_dimension = max(source.width, source.height, 1)
            zoom = {"value": max(1.0, min(4.0, 520 / max_dimension))}

            def render_zoom() -> None:
                factor = zoom["value"]
                size = (max(1, int(source.width * factor)), max(1, int(source.height * factor)))
                enlarged = source.resize(size, Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(enlarged)
                canvas.delete("all")
                canvas.create_image(0, 0, anchor="nw", image=photo)
                canvas.image = photo
                canvas.configure(scrollregion=(0, 0, size[0], size[1]))
                zoom_text.configure(text=f"{factor * 100:.0f}%")

            controls = ttk.Frame(zoom_window, padding=(10, 0, 10, 10))
            controls.pack(fill="x")
            ttk.Button(controls, text="−", width=4, command=lambda: set_zoom(zoom["value"] / 1.25)).pack(side="left")
            zoom_text = ttk.Label(controls, text="")
            zoom_text.pack(side="left", padx=10)
            ttk.Button(controls, text="+", width=4, command=lambda: set_zoom(zoom["value"] * 1.25)).pack(side="left")
            ttk.Button(controls, text="Passend", command=lambda: set_zoom(max(1.0, min(4.0, 520 / max_dimension)))).pack(side="left", padx=8)

            def set_zoom(value: float) -> None:
                zoom["value"] = max(0.5, min(10.0, value))
                render_zoom()

            render_zoom()

            # Activate only after Tk has mapped the fully-built popup. Activating
            # a Toplevel before it is visible can make Windows briefly focus it,
            # then return focus to its owner (the comparison window).
            def activate_zoom() -> None:
                if not zoom_window.winfo_exists():
                    return
                try:
                    zoom_window.lift()
                    zoom_window.focus_force()
                except tk.TclError:
                    pass

            zoom_window.after(100, activate_zoom)

        def confirm_same():
            pdf_row = win._selected_pdf
            catalog_row = win._selected_catalog
            if not pdf_row or not catalog_row:
                messagebox.showinfo("Selectie ontbreekt", "Selecteer eerst aan beide kanten een onderdeel.", parent=win)
                return
            set_aliases = self.part_aliases.setdefault(self.set_entry.get().strip(), {})
            set_aliases[pdf_row["id"]] = catalog_row["id"]
            self._save_set_aliases(self.set_entry.get().strip())
            pdf_tree.delete(str(pdf_parts.index(pdf_row)))
            catalog_tree.delete(str(catalog_parts.index(catalog_row)))
            win._selected_pdf = None
            win._selected_catalog = None
            win._image_refs.pop("pdf", None)
            win._image_refs.pop("catalog", None)
            win._source_images.pop("pdf", None)
            win._source_images.pop("catalog", None)
            pdf_preview.configure(text="Geen ongekoppelde PDF-onderdelen" if not pdf_tree.get_children("") else "Selecteer een PDF-onderdeel", image="")
            catalog_preview.configure(text="Geen ongekoppelde Rebrickable-onderdelen" if not catalog_tree.get_children("") else "Selecteer een Rebrickable-onderdeel", image="")
            comparison_header.configure(text=f"Nog niet gekoppeld: {len(pdf_tree.get_children(''))} LEGO-elementen en {len(catalog_tree.get_children(''))} Rebrickable-onderdelen. Vergelijk vorm én kleur.")
            self._show_catalog_results(self.catalog_records, self.pdf_rows, auto_compare=False)
            if not self._has_unmatched_parts():
                win.destroy()
                return
            self.status.set(f"Visuele match opgeslagen: LEGO {pdf_row['id']} = Rebrickable {catalog_row['id']}.")

        def correct_saved_match():
            current_aliases = self.part_aliases.get(self.set_entry.get().strip(), {})
            if not current_aliases:
                messagebox.showinfo("Geen opgeslagen koppelingen", "Er zijn voor deze set geen opgeslagen koppelingen om te corrigeren.", parent=win)
                return

            correction = tk.Toplevel(win)
            correction.title("Opgeslagen koppelingen corrigeren")
            correction.geometry("560x330")
            correction.transient(win)
            ttk.Label(correction, text="Selecteer een koppeling om ongedaan te maken. Beide onderdelen komen terug in de vergelijking.", wraplength=520).pack(anchor="w", padx=12, pady=12)
            search_var = tk.StringVar()
            ttk.Label(correction, text="Zoeken op onderdeelnummer").pack(anchor="w", padx=12)
            search_entry = ttk.Entry(correction, textvariable=search_var)
            search_entry.pack(fill="x", padx=12, pady=(0, 8))
            mapping_tree = ttk.Treeview(correction, columns=("pdf", "catalog"), show="headings", height=9, selectmode="browse")
            mapping_tree.heading("pdf", text="LEGO-elementnummer", command=lambda: sort_saved_matches("pdf"))
            mapping_tree.heading("catalog", text="Rebrickable-nummer", command=lambda: sort_saved_matches("catalog"))
            mapping_tree.column("pdf", width=220)
            mapping_tree.column("catalog", width=220)
            mapping_tree.pack(fill="both", expand=True, padx=12)

            def refresh_saved_matches(*_args):
                query = search_var.get().strip().casefold()
                for item_id in mapping_tree.get_children(""):
                    mapping_tree.delete(item_id)
                for pdf_id, catalog_id in current_aliases.items():
                    if query and query not in pdf_id.casefold() and query not in str(catalog_id).casefold():
                        continue
                    mapping_tree.insert("", "end", iid=pdf_id, values=(pdf_id, catalog_id))

            saved_match_sort_directions = {}

            def sort_saved_matches(column: str):
                rows = [(mapping_tree.set(item_id, column), item_id) for item_id in mapping_tree.get_children("")]
                reverse = saved_match_sort_directions.get(column, False)
                for index, (_value, item_id) in enumerate(sorted(rows, key=lambda row: row[0].casefold(), reverse=reverse)):
                    mapping_tree.move(item_id, "", index)
                saved_match_sort_directions[column] = not reverse

            search_var.trace_add("write", refresh_saved_matches)
            refresh_saved_matches()

            def undo_selected_match():
                selected = mapping_tree.selection()
                if not selected:
                    messagebox.showinfo("Selectie ontbreekt", "Selecteer eerst de koppeling die je wilt corrigeren.", parent=correction)
                    return
                pdf_id = selected[0]
                catalog_id = current_aliases.pop(pdf_id, None)
                if catalog_id is None:
                    return
                if not current_aliases:
                    self.part_aliases.pop(self.set_entry.get().strip(), None)
                self._save_set_aliases(self.set_entry.get().strip())
                pdf_item = next(({"id": number, "quantity": qty} for number, qty in self.pdf_rows if number.strip().lower() == pdf_id.lower()), None)
                catalog_item = next((item for item in all_catalog_parts if item["id"].lower() == str(catalog_id).lower()), None)
                if pdf_item and all(item["id"].lower() != pdf_item["id"].lower() for item in pdf_parts):
                    pdf_parts.append(pdf_item)
                    new_iid = str(len(pdf_parts) - 1)
                    pdf_tree.insert("", "end", iid=new_iid, values=(pdf_item["id"], pdf_item["quantity"], self.element_color_cache.get(pdf_item["id"], "Kleur ophalen…")))
                    self._lookup_pdf_element_color(pdf_item["id"], pdf_tree, new_iid, pdf_color_label, win)
                if catalog_item and all(item["id"].lower() != catalog_item["id"].lower() for item in catalog_parts):
                    catalog_parts.append(catalog_item)
                    new_iid = str(len(catalog_parts) - 1)
                    catalog_tree.insert("", "end", iid=new_iid, values=(catalog_item["element_id"], catalog_item["part_number"], catalog_item["name"], catalog_item["color"], catalog_item["quantity"]))
                mapping_tree.delete(pdf_id)
                update_comparison_header()
                self._show_catalog_results(self.catalog_records, self.pdf_rows, auto_compare=False)
                self.status.set(f"Koppeling ongedaan gemaakt: LEGO {pdf_id} ↔ Rebrickable {catalog_id}. Koppel de onderdelen opnieuw.")
                if not mapping_tree.get_children(""):
                    correction.destroy()

            ttk.Button(correction, text="Koppeling ongedaan maken", command=undo_selected_match).pack(anchor="e", padx=12, pady=12)

        pdf_tree.bind("<<TreeviewSelect>>", show_pdf)
        catalog_tree.bind("<<TreeviewSelect>>", show_catalog)
        # Resolve every PDF element automatically as soon as the comparison opens.
        # Uncached lookups are serialized to stay within Rebrickable's API rate limit.
        for index, row in enumerate(pdf_parts):
            self._lookup_pdf_element_color(row["id"], pdf_tree, str(index), pdf_color_label, win)
        controls = ttk.Frame(win, padding=(14, 0, 14, 14))
        controls.pack(fill="x")
        ttk.Label(controls, text="Een bevestigde match wordt voor deze set bewaard.").pack(side="left")
        ttk.Button(controls, text="Verkeerde koppeling corrigeren…", command=correct_saved_match).pack(side="right", padx=(0, 8))
        ttk.Button(controls, text="Bevestig: hetzelfde onderdeel", command=confirm_same).pack(side="right")
        # Do not force focus to the main window: this comparison is a normal,
        # non-modal window and the user must be able to switch to other apps.


if __name__ == "__main__":
    LegoPartsApp().mainloop()

