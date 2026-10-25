#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETS2 Tuner - lokales Tuning-Tool fuer Euro Truck Simulator 2
-------------------------------------------------------------
Startet einen kleinen Webserver auf 127.0.0.1 und oeffnet die Oberflaeche im Browser.
Benoetigt nur Python 3.8+ (keine Zusatzpakete).

Ablauf:
  1. Spielordner + Extractor finden (automatisch oder per Auswahl)
  2. /def/vehicle/truck aus allen .scs-Archiven entpacken (Cache-Ordner)
  3. Truck waehlen, Motoren/Getriebe bearbeiten
  4. Export als .scs-Mod (neue Variante oder Original ueberschreiben)
"""
import base64
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_VERSION = "1.3.1-beta"
HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
CACHE_DIR = os.path.join(HERE, "cache")
CONFIG_FILE = os.path.join(HERE, "config.json")
PROJECT_FILE = os.path.join(HERE, "projekt.json")
PORT = 8765

LOCALE_PATHS = ["/locale/de_de", "/locale/en_us"]
EXTRACT_PATHS = ["/def/vehicle/truck"] + LOCALE_PATHS

# --------------------------------------------------------------------------------------
# Konfiguration / Pfaderkennung
# --------------------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def steam_libraries():
    libs = []
    steam = None
    if sys.platform.startswith("win"):
        try:
            import winreg
            for hive, key in [(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")]:
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for name in ("SteamPath", "InstallPath"):
                            try:
                                steam = winreg.QueryValueEx(k, name)[0]
                                break
                            except OSError:
                                pass
                    if steam:
                        break
                except OSError:
                    pass
        except ImportError:
            pass
        if not steam:
            steam = r"C:\Program Files (x86)\Steam"
    else:
        steam = os.path.expanduser("~/.steam/steam")
    if steam:
        steam = os.path.normpath(steam)
        libs.append(steam)
        vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    p = os.path.normpath(m.group(1).replace("\\\\", "\\"))
                    if p not in libs:
                        libs.append(p)
        except OSError:
            pass
    return libs


def detect_game_dir():
    for lib in steam_libraries():
        p = os.path.join(lib, "steamapps", "common", "Euro Truck Simulator 2")
        if os.path.isfile(os.path.join(p, "def.scs")):
            return p
    return ""


def documents_dir():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes
            import uuid
            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
            u = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
            g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(ptr)) == 0:
                path = ptr.value
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                return path
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def detect_mod_dir():
    cands = [os.path.join(documents_dir(), "Euro Truck Simulator 2", "mod"),
             os.path.join(os.path.expanduser("~"), "OneDrive", "Dokumente", "Euro Truck Simulator 2", "mod"),
             os.path.join(os.path.expanduser("~"), "OneDrive", "Documents", "Euro Truck Simulator 2", "mod")]
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0]


def detect_extractor():
    names = ["extractor.exe", "scs_extractor.exe", "extractor", "scs_extractor"]
    for d in [HERE, os.path.join(HERE, "extractor")]:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return ""


def get_config():
    cfg = load_json(CONFIG_FILE, {})
    changed = False
    if not cfg.get("game_dir") or not os.path.isdir(cfg.get("game_dir", "")):
        cfg["game_dir"] = detect_game_dir()
        changed = True
    if not cfg.get("mod_dir"):
        cfg["mod_dir"] = detect_mod_dir()
        changed = True
    if not cfg.get("extractor") or not os.path.isfile(cfg.get("extractor", "")):
        cfg["extractor"] = detect_extractor()
        changed = True
    cfg.setdefault("language", "de_de")
    if changed:
        try:
            save_json(CONFIG_FILE, cfg)
        except OSError:
            pass
    return cfg


def config_status(cfg):
    gd = cfg.get("game_dir", "")
    ex = cfg.get("extractor", "")
    st = {
        "game_ok": bool(gd) and os.path.isfile(os.path.join(gd, "def.scs")),
        "extractor_ok": bool(ex) and os.path.isfile(ex),
        "extractor_kind": extractor_kind(ex) if ex else "",
        "mod_dir_ok": os.path.isdir(cfg.get("mod_dir", "")),
        "cache_ok": cache_has_data(),
        "cache_info": load_json(os.path.join(CACHE_DIR, "_info.json"), {}),
    }
    return st


def extractor_kind(path):
    n = os.path.basename(path).lower()
    return "scs" if n.startswith("scs_extractor") else "skzk"


def is_en():
    try:
        return load_json(CONFIG_FILE, {}).get("language") == "en_us"
    except Exception:
        return False


def M(de, en):
    """Meldung in der eingestellten Sprache / message in the configured language"""
    return en if is_en() else de


# --------------------------------------------------------------------------------------
# Extraktion
# --------------------------------------------------------------------------------------

EXTRACT_STATE = {"running": False, "log": [], "done": 0, "total": 0, "error": "", "finished": False}
EXTRACT_LOCK = threading.Lock()


def xlog(msg):
    with EXTRACT_LOCK:
        EXTRACT_STATE["log"].append(msg)
        if len(EXTRACT_STATE["log"]) > 400:
            EXTRACT_STATE["log"] = EXTRACT_STATE["log"][-400:]


def archive_order(name):
    n = name.lower()
    # Basisarchive zuerst, DLCs danach (spaetere ueberschreiben fruehere)
    if n.startswith("dlc_"):
        return (2, n)
    if n in ("def.scs",):
        return (0, "0")
    return (1, n)


def list_archives(game_dir):
    out = []
    for f in os.listdir(game_dir):
        if f.lower().endswith(".scs"):
            out.append(f)
    out.sort(key=archive_order)
    return out


def run_extraction(cfg):
    try:
        game_dir = cfg["game_dir"]
        exe = cfg["extractor"]
        kind = extractor_kind(exe)
        archives = list_archives(game_dir)
        if kind == "scs":
            # Offizieller SCS Extractor kann (vermutlich) nicht gezielt entpacken -> nur kleine Archive
            small = []
            for a in archives:
                size = os.path.getsize(os.path.join(game_dir, a))
                if a.lower() in ("def.scs", "locale.scs") or (a.lower().startswith("dlc_") and size < 250 * 1024 * 1024):
                    small.append(a)
            archives = small
            xlog(M("Offizieller SCS Extractor erkannt: entpacke komplette Archive (nur def.scs, locale.scs und kleine DLCs).", "Official SCS extractor detected: extracting complete archives (only def.scs, locale.scs and small DLCs)."))
        if os.path.isdir(CACHE_DIR):
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
        os.makedirs(CACHE_DIR, exist_ok=True)
        EXTRACT_STATE["total"] = len(archives)
        used = []
        creationflags = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW
        for i, a in enumerate(archives):
            src = os.path.join(game_dir, a)
            dest = os.path.join(CACHE_DIR, "%03d_%s" % (i, os.path.splitext(a)[0]))
            os.makedirs(dest, exist_ok=True)
            if kind == "scs":
                cmd = [exe, src, dest]
            else:
                cmd = [exe, src, "--partial=" + ",".join(EXTRACT_PATHS), "-d", dest, "-q"]
            xlog("[%d/%d] %s" % (i + 1, len(archives), a))
            try:
                p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900,
                                   stdin=subprocess.DEVNULL, creationflags=creationflags)
                outp = p.stdout.decode("utf-8", "ignore").strip()
                if p.returncode != 0 and outp:
                    xlog(M("   Hinweis: ", "   Note: ") + outp.splitlines()[-1][:200])
            except subprocess.TimeoutExpired:
                xlog(M("   Zeitlimit ueberschritten, uebersprungen", "   Timeout, skipped"))
            # Leere Ordner entfernen
            has = any(files for _, _, files in os.walk(dest))
            if has:
                # Bei vollstaendiger Entpackung alles ausser Trucks/Locale wegwerfen
                if kind == "scs":
                    prune_dir(dest)
                used.append(a)
            else:
                shutil.rmtree(dest, ignore_errors=True)
            EXTRACT_STATE["done"] = i + 1
        save_json(os.path.join(CACHE_DIR, "_info.json"),
                  {"time": time.strftime("%d.%m.%Y %H:%M"), "archives": used, "game_dir": game_dir})
        VFS.reset()
        n = len(VFS.truck_ids())
        xlog(M("Fertig: %d Archive mit Truckdaten, %d Trucks gefunden.", "Done: %d archives with truck data, %d trucks found.") % (len(used), n))
        if n == 0:
            EXTRACT_STATE["error"] = M("Keine Truckdaten gefunden. Bitte Extractor pruefen (siehe Protokoll).", "No truck data found. Please check the extractor (see log).")
    except Exception as e:
        EXTRACT_STATE["error"] = str(e)
        xlog(M("FEHLER: ", "ERROR: ") + str(e))
        traceback.print_exc()
    finally:
        EXTRACT_STATE["running"] = False
        EXTRACT_STATE["finished"] = True


def prune_dir(dest):
    keep = [os.path.normpath(os.path.join(dest, *p.strip("/").split("/"))) for p in EXTRACT_PATHS]
    for root, dirs, files in os.walk(dest, topdown=False):
        for f in files:
            fp = os.path.normpath(os.path.join(root, f))
            if not any(fp.startswith(k + os.sep) for k in keep):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        if not os.listdir(root) and root != dest:
            try:
                os.rmdir(root)
            except OSError:
                pass


def cache_has_data():
    if not os.path.isdir(CACHE_DIR):
        return False
    for d in os.listdir(CACHE_DIR):
        if os.path.isdir(os.path.join(CACHE_DIR, d, "def", "vehicle", "truck")):
            return True
    return False


# --------------------------------------------------------------------------------------
# Virtuelles Dateisystem ueber alle entpackten Archive
# --------------------------------------------------------------------------------------

class VirtualFS:
    def __init__(self):
        self.lock = threading.Lock()
        self.index = None   # "/def/vehicle/truck/x/engine/a.sii" -> (abs, layer)
        self.locale = None

    def reset(self):
        with self.lock:
            self.index = None
            self.locale = None

    def _build(self):
        idx = {}
        if os.path.isdir(CACHE_DIR):
            for layer in sorted(os.listdir(CACHE_DIR)):
                base = os.path.join(CACHE_DIR, layer)
                if not os.path.isdir(base):
                    continue
                for root, _, files in os.walk(base):
                    for f in files:
                        ab = os.path.join(root, f)
                        rel = "/" + os.path.relpath(ab, base).replace(os.sep, "/")
                        idx[rel.lower()] = (ab, layer, rel)
        self.index = idx

    def ensure(self):
        with self.lock:
            if self.index is None:
                self._build()

    def get(self, path):
        self.ensure()
        return self.index.get(path.lower())

    def read(self, path):
        e = self.get(path)
        if not e:
            return None
        return read_text(e[0])

    def listdir_files(self, prefix):
        self.ensure()
        p = prefix.lower().rstrip("/") + "/"
        return [v[2] for k, v in self.index.items() if k.startswith(p)]

    def truck_ids(self):
        self.ensure()
        ids = set()
        for k, v in self.index.items():
            if k.startswith("/def/vehicle/truck/"):
                parts = v[2].split("/")
                if len(parts) > 5 and parts[5].lower() in ("engine", "transmission"):
                    ids.add(parts[4])
        return sorted(ids)

    def get_locale(self, lang):
        with self.lock:
            if self.locale is not None:
                return self.locale
        loc = {}
        for lp in [LOCALE_PATHS[1], "/locale/" + lang]:  # en zuerst, dann gewuenschte Sprache darueber
            for f in sorted(self.listdir_files(lp)):
                if not f.lower().endswith(".sii"):
                    continue
                txt = self.read(f)
                if not txt or "localization_db" not in txt:
                    continue
                keys = re.findall(r'^\s*key\[\]\s*:\s*"((?:[^"\\]|\\.)*)"', txt, re.M)
                vals = re.findall(r'^\s*val\[\]\s*:\s*"((?:[^"\\]|\\.)*)"', txt, re.M)
                for k, v in zip(keys, vals):
                    loc[k] = v
        with self.lock:
            self.locale = loc
        return loc


VFS = VirtualFS()


def read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] in (b"ScsC", b"BSII"):
        return None  # verschluesselt/binaer
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# --------------------------------------------------------------------------------------
# SII-Parser
# --------------------------------------------------------------------------------------

LINE_RE = re.compile(r'^\s*([A-Za-z0-9_]+)\s*(\[\s*\d*\s*\])?\s*:\s*(.*?)\s*$')
INCLUDE_RE = re.compile(r'^\s*@include\s+"([^"]+)"')


def strip_comment(line):
    out = []
    inq = False
    i = 0
    while i < len(line):
        c = line[i]
        if c == '"' and (i == 0 or line[i - 1] != "\\"):
            inq = not inq
        if not inq:
            if c == "#":
                break
            if line.startswith("//", i):
                break
        out.append(c)
        i += 1
    return "".join(out).rstrip()


def remove_block_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def parse_number(tok):
    tok = tok.strip()
    if tok.startswith("&"):
        try:
            import struct
            return struct.unpack(">f", bytes.fromhex(tok[1:]))[0]
        except Exception:
            return tok
    try:
        if re.fullmatch(r"[-+]?\d+", tok):
            return int(tok)
        return float(tok)
    except ValueError:
        return None


def parse_value(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        m = re.match(r'"((?:[^"\\]|\\.)*)"', raw)
        return m.group(1) if m else raw.strip('"')
    if raw.startswith("("):
        inner = raw.strip()[1:].rsplit(")", 1)[0]
        parts = [p.strip() for p in inner.split(",")]
        vals = [parse_number(p) for p in parts]
        if all(v is not None for v in vals):
            return vals
        return raw
    if raw in ("true", "false"):
        return raw == "true"
    n = parse_number(raw)
    if n is not None:
        return n
    return raw


def find_units(text):
    """Liefert [(typ, name, body_text)]"""
    text = remove_block_comments(text)
    units = []
    for m in re.finditer(r'([a-z0-9_]+)\s*:\s*([A-Za-z0-9_.]+)\s*\{', text):
        if m.group(1) == "SiiNunit":
            continue
        start = m.end()
        depth = 1
        i = start
        inq = False
        while i < len(text) and depth > 0:
            c = text[i]
            if c == '"':
                inq = not inq
            elif not inq:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
            i += 1
        units.append((m.group(1), m.group(2), text[start:i - 1]))
    return units


def expand_includes(body, file_path, depth=0):
    """Ersetzt @include-Zeilen durch den Inhalt (rekursiv). file_path = virtueller Pfad"""
    out = []
    base_dir = posixpath.dirname(file_path)
    for line in body.splitlines():
        m = INCLUDE_RE.match(line)
        if m and depth < 8:
            inc = m.group(1)
            inc_path = inc if inc.startswith("/") else posixpath.normpath(posixpath.join(base_dir, inc))
            txt = VFS.read(inc_path)
            if txt is None:
                out.append(line)  # nicht aufloesbar -> unveraendert lassen
                continue
            out.append("# --- begin include %s ---" % inc)
            out.extend(expand_includes(remove_block_comments(txt), inc_path, depth + 1))
            out.append("# --- end include %s ---" % inc)
        else:
            out.append(line)
    return out


def parse_lines(lines):
    """-> (entries, values). entries: [{raw, key, arr}], values: {key: value or [values]}"""
    entries = []
    arrays = set()
    for raw in lines:
        clean = strip_comment(raw)
        m = LINE_RE.match(clean)
        if not m:
            entries.append({"raw": raw, "key": None, "arr": False})
            continue
        key, arr, val = m.group(1), m.group(2), m.group(3)
        if arr:
            arrays.add(key)
        entries.append({"raw": raw, "key": key, "arr": bool(arr), "val": val})
    values = {}
    for e in entries:
        k = e["key"]
        if not k:
            continue
        if k in arrays:
            if not e["arr"]:
                continue  # Anzahl-Zeile
            values.setdefault(k, []).append(parse_value(e["val"]))
        else:
            values[k] = parse_value(e["val"])
    return entries, values, arrays


def fmt_num(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if abs(v - round(v)) < 1e-9 and abs(v) < 1e9:
            return str(int(round(v)))
        s = ("%.4f" % v).rstrip("0").rstrip(".")
        return s
    return str(v)


def fmt_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return fmt_num(v)
    if isinstance(v, (list, tuple)):
        return "(" + ", ".join(fmt_num(x) for x in v) + ")"
    if isinstance(v, str):
        if v.startswith("RAW:"):
            return v[4:]
        return '"' + v.replace('"', '\\"') + '"'
    return str(v)


def apply_changes(lines, changes):
    """changes: {key: value | list (array) | None (entfernen)}"""
    entries, values, arrays = parse_lines(lines)
    done = set()
    out = []
    for e in entries:
        k = e["key"]
        if k and k in changes:
            if k in done:
                continue
            done.add(k)
            out.extend(render_attr(k, changes[k], k in arrays))
        else:
            out.append(e["raw"])
    rest = [k for k in changes if k not in done and changes[k] is not None]
    if rest:
        # vor dem ersten include-Block bzw. am Ende einfuegen
        add = []
        for k in rest:
            add.extend(render_attr(k, changes[k], k in ARRAY_KEYS))
        out = out + [""] + add
    return out


TUPLE_SCALARS = {"rpm_range_low_gear", "rpm_range_high_gear", "rpm_range_power", "rpm_range_power_boost",
                 "rpm_range_engine_brake"}


def render_attr(key, value, is_array):
    if value is None:
        return []
    if is_array or key in ARRAY_KEYS:
        return ["\t%s[]: %s" % (key, fmt_value(v)) for v in value]
    return ["\t%s: %s" % (key, fmt_value(value))]


ARRAY_KEYS = {"torque_curve", "ratios_forward", "ratios_reverse", "info", "secondary_torque_curve", "overrides",
              "defaults", "suitable_for", "conflict_with", "sounds"}


# --------------------------------------------------------------------------------------
# Komponenten laden
# --------------------------------------------------------------------------------------

KIND_UNITS = {"engine": "accessory_engine_data", "transmission": "accessory_transmission_data"}


def localize(s, loc):
    if not isinstance(s, str):
        return s
    return re.sub(r"@@([A-Za-z0-9_\.]+)@@", lambda m: loc.get(m.group(1), m.group(0)), s)


def pretty_truck(tid, loc):
    # Versuche Name aus accessory_truck_data
    for f in VFS.listdir_files("/def/vehicle/truck/" + tid):
        if f.count("/") != 5 or not f.lower().endswith(".sii"):
            continue
        txt = VFS.read(f)
        if txt and "accessory_truck_data" in txt:
            for typ, name, body in find_units(txt):
                if typ == "accessory_truck_data":
                    _, vals, _ = parse_lines(body.splitlines())
                    n = vals.get("name")
                    if isinstance(n, str) and n:
                        brand = tid.split(".")[0].replace("_", " ").title()
                        nm = localize(n, loc)
                        return nm if brand.lower() in nm.lower() else brand + " " + nm
    brand, _, model = tid.partition(".")
    return (brand.replace("_", " ").title() + " " + model.replace("_", " ").upper()).strip()


def load_component(file_path, kind):
    txt = VFS.read(file_path)
    if not txt:
        return []
    out = []
    for typ, unit, body in find_units(txt):
        if typ != KIND_UNITS[kind]:
            continue
        lines = expand_includes(body, file_path)
        _, values, arrays = parse_lines(lines)
        out.append({"unit": unit, "file": file_path, "values": values, "lines": lines})
    return out


def truck_components(tid):
    cfg = get_config()
    loc = VFS.get_locale(cfg.get("language", "de_de"))
    res = {"id": tid, "name": pretty_truck(tid, loc), "engine": [], "transmission": []}
    for kind in ("engine", "transmission"):
        files = sorted(f for f in VFS.listdir_files("/def/vehicle/truck/%s/%s" % (tid, kind))
                       if f.lower().endswith(".sii"))
        for f in files:
            for c in load_component(f, kind):
                v = c["values"]
                e = VFS.get(f)
                res[kind].append({
                    "key": "%s|%s|%s" % (tid, kind, c["unit"]),
                    "unit": c["unit"], "file": f, "layer": e[1] if e else "",
                    "display": localize(v.get("name", c["unit"]), loc),
                    "info": [localize(i, loc) for i in v.get("info", [])] if isinstance(v.get("info"), list) else [],
                    "values": v,
                })
    if res["engine"]:
        res["engine"].sort(key=lambda c: (c["values"].get("torque", 0) if isinstance(c["values"].get("torque"), (int, float)) else 0))
    if res["transmission"]:
        res["transmission"].sort(key=lambda c: (c["display"], c["values"].get("differential_ratio", 0) or 0))
    return res


# --------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------

ICON_B64 = "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"


def fmt_dg(n):
    n = int(round(n))
    s = str(abs(n))
    groups = []
    while len(s) > 3:
        groups.insert(0, s[-3:])
        s = s[:-3]
    groups.insert(0, s)
    return ("-" if n < 0 else "") + "@@dg@@".join(groups)


def interp(curve, r):
    if not curve:
        return 0
    if r <= curve[0][0]:
        return curve[0][1]
    for (a, fa), (b, fb) in zip(curve, curve[1:]):
        if a <= r <= b:
            return fa if b == a else fa + (fb - fa) * (r - a) / (b - a)
    return curve[-1][1]


def engine_stats(values):
    t = values.get("torque") or 0
    curve = values.get("torque_curve") or []
    curve = sorted([(float(p[0]), float(p[1])) for p in curve if isinstance(p, list) and len(p) >= 2])
    lim = values.get("rpm_limit") or 2500
    best_kw, best_r = 0, 0
    r = 400
    while r <= lim:
        kw = t * interp(curve, r) * r / 9549.3
        if kw > best_kw:
            best_kw, best_r = kw, r
        r += 10
    plateau = [p[0] for p in curve if p[1] >= 0.995]
    return {"kw": best_kw, "hp": best_kw * 1.35962, "rpm_peak": best_r,
            "plat": (min(plateau), max(plateau)) if plateau else None}


def regen_engine_info(values):
    st = engine_stats(values)
    info = ['%d @@hp@@ (%d@@kw@@)' % (round(st["hp"]), round(st["kw"])),
            '%s @@nm@@' % fmt_dg(values.get("torque", 0))]
    if st["plat"]:
        a, b = st["plat"]
        if b - a >= 50:
            info.append('%s-%s @@rpm@@' % (fmt_dg(a), fmt_dg(b)))
        else:
            info.append('%s @@rpm@@' % fmt_dg(a))
    return info


def make_unit_name(base, label, n):
    h = "%08x" % (zlib.crc32(("%s|%s|%d" % (base, label, n)).encode()) & 0xffffffff)
    return "tn" + h[:8]  # max 12 Zeichen erlaubt


def build_mod(payload):
    items = payload.get("items", [])
    if not items:
        raise ValueError(M("Keine Aenderungen zum Exportieren.", "No changes to export."))
    files = {}
    summary = []
    used_names = set()
    over_keys = set()
    for n, it in enumerate(items):
        tid, kind, unit = it["truck"], it["kind"], it["unit"]
        src_file = it["file"]
        comps = [c for c in load_component(src_file, kind) if c["unit"] == unit]
        if not comps:
            raise ValueError(M("Original nicht gefunden: %s", "Original not found: %s") % src_file)
        comp = comps[0]
        changes = dict(it.get("changes", {}))
        # Werte zusammenfuehren
        merged = dict(comp["values"])
        merged.update({k: v for k, v in changes.items() if v is not None})
        mode = it.get("mode", "new")
        label = (it.get("label") or "Tuning").strip()
        if kind == "engine" and any(k in changes for k in ("torque", "torque_curve")) and "info" not in changes \
                and it.get("auto_info", True):
            changes["info"] = regen_engine_info(merged)
        suffix_unit = comp["unit"].split(".", 1)
        if mode == "new":
            if "name" not in changes:
                changes["name"] = "%s %s" % (comp["values"].get("name", unit.split(".")[0]), label)
            short = make_unit_name(comp["unit"], label, n)
            while short in used_names:
                short = make_unit_name(comp["unit"], label + "x", n + len(used_names))
            used_names.add(short)
            new_unit = short + "." + suffix_unit[1] if len(suffix_unit) > 1 else short
            out_path = posixpath.join(posixpath.dirname(src_file), short + ".sii")
            if "unlock" not in changes:
                changes["unlock"] = 0
        else:
            if comp["unit"] in over_keys:
                raise ValueError(M("%s wird mehrfach ueberschrieben - nur eine Version darf 'Original ueberschreiben' nutzen.", "%s is overwritten more than once - only one version may use 'Overwrite original'.") % comp["unit"])
            over_keys.add(comp["unit"])
            new_unit = comp["unit"]
            out_path = src_file
        lines = apply_changes(comp["lines"], changes)
        text = "SiiNunit\n{\n%s : %s\n{\n%s\n}\n}\n" % (KIND_UNITS[kind], new_unit, "\n".join(lines))
        files[out_path.lstrip("/")] = text
        summary.append("%s: %s (%s)" % (M("Motor", "Engine") if kind == "engine" else M("Getriebe", "Transmission"),
                                         label if mode == "new" else comp["unit"],
                                         M("neue Variante", "new variant") if mode == "new" else M("Original ueberschrieben", "original overwritten")))
    mod_name = (payload.get("mod_name") or "ETS2 Tuning").strip()
    manifest = ('SiiNunit\n{\nmod_package : .package_name\n{\n\tpackage_version: "%s"\n\tdisplay_name: "%s"\n'
                '\tauthor: "ETS2 Tuner"\n\tcategory[]: "tuning_parts"\n\tcategory[]: "physics"\n'
                '\tdescription_file: "description.txt"\n\ticon: "icon.jpg"\n}\n}\n') % (
        time.strftime("%Y.%m.%d-%H%M"), mod_name.replace('"', "'"))
    desc = M("%s\n\nErstellt mit ETS2 Tuner (Beta) am %s\n\n%s\n", "%s\n\nCreated with ETS2 Tuner (beta) on %s\n\n%s\n") % (mod_name, time.strftime("%d.%m.%Y %H:%M"), "\n".join(summary))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.sii", manifest)
        z.writestr("description.txt", desc)
        z.writestr("icon.jpg", base64.b64decode(ICON_B64))
        for p, t in files.items():
            z.writestr(p, t)
    return buf.getvalue(), files


def safe_filename(name):
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_").lower()
    return (s or "ets2_tuning") + ".scs"


# --------------------------------------------------------------------------------------
# Datei-/Ordnerauswahl (natives Fenster ueber tkinter in eigenem Prozess)
# --------------------------------------------------------------------------------------

def native_dialog(kind, initial=""):
    code = (
        "import tkinter as tk\nfrom tkinter import filedialog\nr=tk.Tk();r.withdraw();r.attributes('-topmost',True)\n"
        "k=%r;i=%r\n"
        "p=filedialog.askdirectory(initialdir=i or None) if k=='dir' else "
        "filedialog.askopenfilename(initialdir=i or None,filetypes=[('Programm','*.exe'),('Alle','*.*')])\n"
        "print(p or '')\n") % (kind, initial)
    try:
        p = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE, timeout=600)
        return os.path.normpath(p.stdout.decode("utf-8", "ignore").strip()) if p.stdout.strip() else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------------------
# HTTP-Server
# --------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ETS2Tuner/" + APP_VERSION

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _check_origin(self):
        # Schutz: nur Anfragen von der eigenen Oberflaeche zulassen
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin and not re.match(r"^http://(127\.0\.0\.1|localhost):%d$" % PORT, origin):
            return False
        return True

    def do_GET(self):
        if not self._check_origin():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(UI_DIR, "index.html"), "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/config":
                cfg = get_config()
                self._json({"config": cfg, "status": config_status(cfg), "version": APP_VERSION})
            elif u.path == "/api/extract/status":
                with EXTRACT_LOCK:
                    self._json(dict(EXTRACT_STATE))
            elif u.path == "/api/trucks":
                cfg = get_config()
                loc = VFS.get_locale(cfg.get("language", "de_de"))
                self._json({"trucks": [{"id": t, "name": pretty_truck(t, loc)} for t in VFS.truck_ids()]})
            elif u.path == "/api/truck":
                tid = parse_qs(u.query).get("id", [""])[0]
                self._json(truck_components(tid))
            elif u.path == "/api/project":
                self._json(load_json(PROJECT_FILE, {}))
            elif u.path == "/api/browse":
                q = parse_qs(u.query)
                self._json({"path": native_dialog(q.get("kind", ["dir"])[0], q.get("initial", [""])[0])})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        if not self._check_origin():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        try:
            body = self._body()
            if u.path == "/api/config":
                cfg = get_config()
                for k in ("game_dir", "mod_dir", "extractor", "language"):
                    if k in body:
                        cfg[k] = body[k]
                save_json(CONFIG_FILE, cfg)
                VFS.reset()
                self._json({"config": cfg, "status": config_status(cfg)})
            elif u.path == "/api/extract":
                cfg = get_config()
                st = config_status(cfg)
                if not st["game_ok"]:
                    return self._json({"error": M("Spielordner nicht gefunden (def.scs fehlt).", "Game folder not found (def.scs missing).")}, 400)
                if not st["extractor_ok"]:
                    return self._json({"error": M("Extractor nicht gefunden.", "Extractor not found.")}, 400)
                with EXTRACT_LOCK:
                    if EXTRACT_STATE["running"]:
                        return self._json({"ok": True})
                    EXTRACT_STATE.update({"running": True, "log": [], "done": 0, "total": 0, "error": "", "finished": False})
                threading.Thread(target=run_extraction, args=(cfg,), daemon=True).start()
                self._json({"ok": True})
            elif u.path == "/api/project":
                save_json(PROJECT_FILE, body)
                self._json({"ok": True})
            elif u.path == "/api/export":
                data, files = build_mod(body)
                fname = safe_filename(body.get("mod_name") or "ets2_tuning")
                if body.get("target") == "moddir":
                    cfg = get_config()
                    md = cfg.get("mod_dir")
                    os.makedirs(md, exist_ok=True)
                    path = os.path.join(md, fname)
                    with open(path, "wb") as f:
                        f.write(data)
                    self._json({"ok": True, "path": path, "files": list(files.keys())})
                elif body.get("target") == "preview":
                    self._json({"ok": True, "files": files})
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", 'attachment; filename="%s"' % fname)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)


def main():
    global PORT
    if "--port" in sys.argv:
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print("Port %d belegt / in use - ETS2 Tuner laeuft schon? / already running? -> http://127.0.0.1:%d" % (PORT, PORT))
        webbrowser.open("http://127.0.0.1:%d" % PORT)
        return
    url = "http://127.0.0.1:%d" % PORT
    print("ETS2 Tuner %s (BETA) - %s" % (APP_VERSION, url))
    print("Dieses Fenster offen lassen. Beenden mit Strg+C oder Fenster schliessen.")
    print("Keep this window open. Quit with Ctrl+C or close the window.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
