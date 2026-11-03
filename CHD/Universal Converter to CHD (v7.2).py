#!/usr/bin/env python3
"""
Universal CHD Converter v8.1
Wandelt PS2 ISO, PSP ISO, PS1/Saturn/Dreamcast Cue+Bin (und Dreamcast GDI) in CHD um.

Hinweise zur technischen Umsetzung:
  - PS2 und PSP nutzen denselben chdman-Befehl (createdvd), da beide als DVD-
    Images behandelt werden (PS2: DVD-ROM, PSP: UMD = optisches Medium).
  - PS1, Saturn und Dreamcast nutzen denselben chdman-Befehl (createcd), da alle
    CD-basierte Systeme mit identischem Speicherformat sind.

Verwendung:
    python3 universal_chd_converter.py [ORDNER] [Optionen]
    python3 universal_chd_converter.py            # interaktives Menue

Beispiele:
    python3 universal_chd_converter.py /roms/ps2 --ps2     # PS2 ISOs
    python3 universal_chd_converter.py /roms/psp --psp     # PSP ISOs
    python3 universal_chd_converter.py /roms/ps1 --ps1     # PS1 Cue+Bin
    python3 universal_chd_converter.py /roms/saturn --saturn
    python3 universal_chd_converter.py /roms/dc --dreamcast
    python3 universal_chd_converter.py /roms/all --auto
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- Farben
RED = '\033[0;31m'
GREEN = '\033[0;32m'
YELLOW = '\033[1;33m'
CYAN = '\033[0;36m'
MAGENTA = '\033[0;35m'
BLUE = '\033[0;34m'
BOLD = '\033[1m'
RESET = '\033[0m'


def disable_colors():
    global RED, GREEN, YELLOW, CYAN, MAGENTA, BLUE, BOLD, RESET
    RED = GREEN = YELLOW = CYAN = MAGENTA = BLUE = BOLD = RESET = ''


def maybe_disable_colors_on_pipe():
    """Schaltet Farben ab, wenn stdout keine Konsole ist (Pipe/Datei)."""
    if not sys.stdout.isatty():
        disable_colors()


# ---------------------------------------------------------------- Logging
class Logger:
    """Schreibt Meldungen auf stdout und optional in eine Logdatei."""

    def __init__(self, log_path=None):
        self.handle = None
        if log_path is not None:
            self.handle = open(log_path, "a", encoding="utf-8")

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None

    def _emit(self, line):
        print(line, flush=True)
        if self.handle:
            self.handle.write(_strip_ansi(line) + "\n")
            self.handle.flush()

    def info(self, msg):
        self._emit(f"{CYAN}[INFO]{RESET} {msg}")

    def warn(self, msg):
        self._emit(f"{YELLOW}[WARN]{RESET} {msg}")

    def error(self, msg):
        self._emit(f"{RED}[ERROR]{RESET} {msg}")

    def success(self, msg):
        self._emit(f"{GREEN}[OK]{RESET} {msg}")

    def raw(self, msg=""):
        self._emit(msg)

    def header(self, title):
        width = max(len(title) + 4, 56)
        self.raw(f"\n{BOLD}{CYAN}{'=' * width}{RESET}")
        self.raw(f"  {title.center(width - 2)}")
        self.raw(f"{CYAN}{'=' * width}{RESET}\n")


def _strip_ansi(text):
    """Entfernt ANSI-Escape-Sequenzen fuer die Logdatei."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


# ---------------------------------------------------------------- chdman
def check_chdman(logger):
    if shutil.which("chdman") is None:
        logger.error("chdman nicht gefunden!")
        logger.info("Arch:          sudo pacman -S mame-tools")
        logger.info("Debian/Ubuntu: sudo apt install mame-tools")
        return False

    try:
        result = subprocess.run(["chdman", "--version"],
                                capture_output=True, text=True, timeout=10)
        version = (result.stdout or result.stderr or "").strip().splitlines()
        if version:
            logger.info(f"chdman: {version[0]}")
    except Exception:
        pass

    return True


def run_chdman_live(cmd, log_handle=None):
    """Fuehrt chdman aus und zeigt die Ausgabe live im Terminal.

    Ohne log_handle wird stdout/stderr geerbt -> ungepufferte Live-Ausgabe
    inklusive Fortschrittszeilen mit Wagenruecklauf.

    Mit log_handle laeuft die Ausgabe durch eine Pipe. Dann wird read1()
    verwendet: das liefert verfuegbare Bytes sofort zurueck, statt wie
    read(n) auf einen vollen Puffer zu warten.
    """
    sys.stdout.flush()

    if log_handle is None:
        return subprocess.run(cmd).returncode

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    assert process.stdout is not None

    while True:
        chunk = process.stdout.read1(4096)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        log_handle.write(_strip_ansi(chunk.decode("utf-8", "replace")))
        log_handle.flush()

    process.wait()
    return process.returncode


# ---------------------------------------------------------------- Cue/GDI-Analyse
def read_text_file(file: Path, desc: str):
    """Liest eine Textdatei (.cue oder .gdi) robust ein.

    Reihenfolge: utf-8 -> latin-1.
    Rueckgabe: (text, None) oder (None, fehlermeldung)
    """
    for enc in ("utf-8", "latin-1"):
        try:
            return file.read_text(encoding=enc), None
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            return None, f"{desc} nicht lesbar: {exc}"
    return None, f"{desc} nicht lesbar (unbekannte Kodierung)"


def parse_cue_references(cue_file: Path):
    """Liefert die in der .cue referenzierten Dateipfade.

    Rueckgabe: (liste_von_Paths, fehlermeldung_oder_None)
    """
    text, err = read_text_file(cue_file, "Cue")
    if err:
        return [], err

    refs = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("FILE"):
            continue

        parts = stripped.split('"')
        if len(parts) >= 2:
            name = parts[1]
        else:
            fields = stripped.split()
            name = fields[1] if len(fields) > 1 else ""

        if not name:
            continue

        refs.append(cue_file.parent / name)

    return refs, None


def parse_gdi_references(gdi_file: Path):
    """Liefert die in der .gdi referenzierten Dateipfade.

    GDI-Format (Dreamcast):
        track01.raw 2352 0
        track02.raw 2352 0
        ...

    Rueckgabe: (liste_von_Paths, fehlermeldung_oder_None)
    """
    text, err = read_text_file(gdi_file, "GDI")
    if err:
        return [], err

    refs = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        parts = stripped.split()
        if parts and "." in parts[0]:
            refs.append(gdi_file.parent / parts[0])

    return refs, None


def resolve_case_insensitive(path: Path):
    """Sucht eine Datei case-insensitiv, falls der exakte Name nicht passt."""
    if path.exists():
        return path

    parent = path.parent
    wanted = path.name.lower()

    try:
        for item in parent.iterdir():
            if item.name.lower() == wanted:
                return item
    except OSError:
        pass

    return None


def check_cd_inputs(source_file: Path) -> tuple:
    """Prueft die Referenzen einer .cue oder .gdi.

    Rueckgabe: (ok: bool, problem: str|None, hinweis: str|None)
    """
    source_type = "cue" if source_file.suffix.lower() == ".cue" else "gdi"

    if source_type == "cue":
        refs, err = parse_cue_references(source_file)
    else:
        refs, err = parse_gdi_references(source_file)

    if err:
        return False, err, None

    if not refs:
        return False, f"keine Track-Eintraege in der {source_type.upper()}", None

    missing = []
    fuzzy = []

    for ref in refs:
        resolved = resolve_case_insensitive(ref)
        if resolved is None:
            missing.append(ref.name)
        elif resolved.name != ref.name:
            fuzzy.append(f"{ref.name} -> {resolved.name}")

    if missing:
        return False, f"fehlende Datei(en): {', '.join(missing)}", None

    hint = None
    if fuzzy:
        hint = "Gross-/Kleinschreibung weicht ab: " + "; ".join(fuzzy)

    return True, None, hint


# ---------------------------------------------------------------- Konvertierung
def _prepare_output(source: Path, overwrite: bool, logger):
    """Prueft/raeumt die Zieldatei. Rueckgabe: (chd_path, status_oder_None)."""
    chd = source.with_suffix(".chd")

    if chd.exists():
        if not overwrite:
            logger.raw(f"{YELLOW}[SKIP]{RESET} {source.name} "
                       f"(CHD existiert bereits)")
            return chd, "skip"
        try:
            chd.unlink()
        except OSError as exc:
            logger.error(f"{source.name}: alte CHD nicht loeschbar ({exc})")
            return chd, "fail"

    return chd, None


def convert_iso(iso_file: Path, kind: str, verbose: bool = False,
                overwrite: bool = False, log_handle=None,
                dry_run: bool = False) -> str:
    """Konvertiert ein ISO (PS2 oder PSP) via createdvd.

    Hinweis: PS2 und PSP nutzen denselben chdman-Befehl (createdvd),
    da beide als DVD-Images behandelt werden (PS2: DVD-ROM, PSP: UMD).

    Rueckgabe: 'ok' | 'skip' | 'fail' | 'dry'
    """
    logger = LOGGER
    chd_file, status = _prepare_output(iso_file, overwrite, logger)
    if status:
        return status

    if dry_run:
        logger.raw(f"{CYAN}[DRY-RUN]{RESET} {kind}: {iso_file.name} "
                   f"-> {chd_file.name}")
        return "dry"

    # DVD-basiert: PS2 und PSP identisch
    cmd = ["chdman", "createdvd",
           "-i", str(iso_file),
           "-o", str(chd_file),
           "-c", "zstd"]

    logger.raw(f"{CYAN}[CONVERT]{RESET} {kind}: {iso_file.name} "
               f"-> {chd_file.name}")
    if verbose:
        logger.raw(f"  Befehl: {' '.join(cmd)}")

    returncode = run_chdman_live(cmd, log_handle=log_handle)

    if returncode == 0:
        logger.raw(f"  {GREEN}\u2713 Fertig{RESET}")
        return "ok"

    logger.error(f"  \u2717 Fehlgeschlagen: {iso_file.name} "
                 f"(Exit-Code {returncode})")
    _remove_partial(chd_file, logger)
    return "fail"


def convert_cue_or_gdi(source_file: Path, kind: str, verbose: bool = False,
                       overwrite: bool = False, log_handle=None,
                       dry_run: bool = False) -> str:
    """Konvertiert eine .cue oder .gdi (+ Tracks) via createcd.

    Unterstuetzt (alle CD-basiert, identischer Befehl):
      - PS1: .cue + .bin
      - Sega Saturn: .cue + .bin
      - Dreamcast: .gdi + .raw ODER .cue + .bin

    Hinweis: PS1, Saturn und Dreamcast nutzen denselben chdman-Befehl
    (createcd), da alle CD-basierte Systeme mit identischem
    Speicherformat sind.

    Rueckgabe: 'ok' | 'skip' | 'fail' | 'dry'
    """
    logger = LOGGER
    chd_file, status = _prepare_output(source_file, overwrite, logger)
    if status:
        return status

    ok, problem, hint = check_cd_inputs(source_file)
    if hint:
        logger.warn(f"{source_file.name}: {hint}")
    if not ok:
        logger.error(f"{source_file.name}: {problem}")
        return "fail"

    if dry_run:
        logger.raw(f"{CYAN}[DRY-RUN]{RESET} {kind}: {source_file.name} "
                   f"-> {chd_file.name}")
        return "dry"

    # CD-basiert: PS1, Saturn, Dreamcast identisch
    cmd = ["chdman", "createcd",
           "-i", str(source_file),
           "-o", str(chd_file)]

    logger.raw(f"{CYAN}[CONVERT]{RESET} {kind}: {source_file.name} "
               f"-> {chd_file.name}")
    if verbose:
        logger.raw(f"  Befehl: {' '.join(cmd)}")

    returncode = run_chdman_live(cmd, log_handle=log_handle)

    if returncode == 0:
        logger.raw(f"  {GREEN}\u2713 Fertig{RESET}")
        return "ok"

    logger.error(f"  \u2717 Fehlgeschlagen: {source_file.name} "
                 f"(Exit-Code {returncode})")
    _remove_partial(chd_file, logger)
    return "fail"


def _remove_partial(chd_file: Path, logger):
    if chd_file.exists():
        try:
            chd_file.unlink()
            logger.raw("  (unvollstaendige CHD entfernt)")
        except OSError as exc:
            logger.warn(f"  Unvollstaendige CHD nicht loeschbar: {exc}")


def verify_chd(chd_file: Path) -> bool:
    """Prueft eine CHD auf Integritaet (chdman verify)."""
    try:
        result = subprocess.run(["chdman", "verify", "-i", str(chd_file)],
                                capture_output=True, text=True, timeout=3600)
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------- Menue
MENU = """
  Quelle waehlen:

    1) PS2 ISO              -> CHD   (createdvd, zstd)
    2) PSP ISO              -> CHD   (createdvd, zstd)
    3) PS1 Cue+Bin          -> CHD   (createcd)
    4) Sega Saturn Cue+Bin  -> CHD   (createcd)
    5) Dreamcast GDI/Cue+Bin-> CHD   (createcd)
    6) Alles automatisch    -> CHD   (ISO + GDI + Cue)
    7) Beenden
"""


def interactive_menu():
    """Fragt Modus und Ordner ab. Rueckgabe: (mode, base) oder (None, None)."""
    print(f"{BOLD}{CYAN}Universal CHD Converter{RESET}")
    print(MENU)

    choice = input("  Auswahl (1-7): ").strip()
    if choice == "7":
        return None, None

    modes = {
        "1": "ps2",
        "2": "psp",
        "3": "ps1",
        "4": "saturn",
        "5": "dreamcast",
        "6": "auto"
    }
    mode = modes.get(choice)
    if mode is None:
        print(f"{RED}Ungueltige Auswahl.{RESET}")
        return None, None

    raw = input("  Quellordner (Enter = aktueller Ordner): ").strip()
    base = Path(raw).expanduser() if raw else Path.cwd()

    return mode, base


# ---------------------------------------------------------------- Main
LOGGER = Logger()


def collect_files(base: Path, mode: str):
    """Sammelt die zu bearbeitenden Dateien je nach Modus.

    BEHOBEN IN v8.1: Alle Modi verwenden nun .suffix.lower(),
    damit auch .ISO oder .CUE mit Grossbuchstaben erkannt werden.
    """
    # Unified approach: alle Modi nutzen case-insensitive Suche
    all_files = [
        p for p in base.rglob("*")
        if p.is_file() and p.suffix.lower() in (".iso", ".cue", ".gdi")
    ]

    if mode == "ps2" or mode == "psp":
        # ISO-Dateien (DVD-basiert)
        files = [p for p in all_files if p.suffix.lower() == ".iso"]
    elif mode == "ps1" or mode == "saturn":
        # Cue-Dateien (CD-basiert)
        files = [p for p in all_files if p.suffix.lower() == ".cue"]
    elif mode == "dreamcast":
        # GDI oder Cue (CD-basiert)
        files = [p for p in all_files if p.suffix.lower() in (".cue", ".gdi")]
    elif mode == "auto":
        # Alles: ISO, GDI, Cue
        files = all_files
    else:
        files = all_files

    return sorted(files, key=lambda p: str(p).lower())


def get_kind_name(mode: str, source: Path) -> str:
    """Rueckgabe des Systemnamens fuer Anzeige."""
    if mode == "ps2":
        return "PS2"
    if mode == "psp":
        return "PSP"
    if mode == "ps1":
        return "PS1"
    if mode == "saturn":
        return "Saturn"
    if mode == "dreamcast":
        if source.suffix.lower() == ".gdi":
            return "DC(GDI)"
        return "DC(Cue)"
    if mode == "auto":
        suffix = source.suffix.lower()
        if suffix == ".iso":
            return "ISO"
        if suffix == ".gdi":
            return "DC(GDI)"
        return "Cue"
    return "UNKNOWN"


def main():
    global LOGGER

    parser = argparse.ArgumentParser(
        description="Universal CHD Converter: PS2/PSP ISO, PS1/Saturn/DC Cue+Bin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Beispiele:\n"
               "  python3 universal_chd_converter.py\n"
               "  python3 universal_chd_converter.py /roms/ps2 --ps2\n"
               "  python3 universal_chd_converter.py /roms/ps1 --ps1\n"
               "  python3 universal_chd_converter.py /roms/psp --psp\n"
               "  python3 universal_chd_converter.py /roms/saturn --saturn\n"
               "  python3 universal_chd_converter.py /roms/dc --dreamcast\n"
               "  python3 universal_chd_converter.py /roms/all --auto --verify\n"
    )

    parser.add_argument("directory", nargs="?", default=None,
                        help="Startverzeichnis (ohne Angabe: Menue)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ps2", action="store_true",
                       help="Nur PS2 ISOs konvertieren")
    group.add_argument("--psp", action="store_true",
                       help="Nur PSP ISOs konvertieren")
    group.add_argument("--ps1", action="store_true",
                       help="Nur PS1 Cue+Bin konvertieren")
    group.add_argument("--saturn", action="store_true",
                       help="Nur Saturn Cue+Bin konvertieren")
    group.add_argument("--dreamcast", action="store_true",
                       help="Nur Dreamcast GDI/Cue+Bin konvertieren")
    group.add_argument("--auto", action="store_true",
                       help="ISO + GDI + Cue gemischt")

    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Befehl und Zusatzinfos anzeigen")
    parser.add_argument("--overwrite", action="store_true",
                        help="Bestehende CHDs ueberschreiben")
    parser.add_argument("--no-color", action="store_true",
                        help="Farbausgabe deaktivieren")
    parser.add_argument("--log", metavar="DATEI",
                        help="Zusaetzlich in Logdatei schreiben")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur anzeigen, was konvertiert wuerde")
    parser.add_argument("--verify", action="store_true",
                        help="Nach jeder Konvertierung chdman verify")

    args = parser.parse_args()

    if args.no_color:
        disable_colors()
    else:
        maybe_disable_colors_on_pipe()

    # Modus bestimmen: CLI-Flags haben Vorrang, sonst Menue
    if args.directory is None:
        mode, base = interactive_menu()
        if mode is None:
            sys.exit(0)
    else:
        base = Path(args.directory).expanduser().resolve()
        if args.ps2:
            mode = "ps2"
        elif args.psp:
            mode = "psp"
        elif args.ps1:
            mode = "ps1"
        elif args.saturn:
            mode = "saturn"
        elif args.dreamcast:
            mode = "dreamcast"
        else:
            mode = "auto"

    if not base.is_dir():
        print(f"{RED}[ERROR]{RESET} Verzeichnis nicht gefunden: {base}",
              flush=True)
        sys.exit(1)

    log_path = None
    if args.log:
        log_path = (base / args.log).resolve()

    LOGGER = Logger(log_path)

    try:
        LOGGER.info("Starte Universal CHD Converter")
        LOGGER.info(f"Verzeichnis: {base}")
        LOGGER.info(f"Modus:       {mode.upper()}")

        if not check_chdman(LOGGER):
            sys.exit(1)

        if log_path:
            LOGGER.info(f"Logdatei:    {log_path}")

        files = collect_files(base, mode)
        if not files:
            LOGGER.warn(f"Keine passenden Dateien in {base} gefunden")
            sys.exit(0)

        LOGGER.info(f"Gefundene Dateien: {len(files)}")
        LOGGER.raw()

        counters = {"ok": 0, "skip": 0, "fail": 0, "dry": 0}
        last_dir = base

        for source in files:
            # Ordnerwechsel sichtbar machen (nur einmal pro Ordner)
            if source.parent != last_dir:
                last_dir = source.parent
                try:
                    rel = source.parent.relative_to(base)
                except ValueError:
                    rel = source.parent
                LOGGER.raw(f"{MAGENTA}# {rel}{RESET}")

            kind = get_kind_name(mode, source)

            # Entscheidung: ISO-Befehl (DVD) oder CD-Befehl?
            suffix = source.suffix.lower()
            if suffix == ".iso":
                # PS2 oder PSP (beide DVD, aber unterschiedliche Plattform)
                plat = "PS2" if mode == "ps2" else "PSP"
                status = convert_iso(source, plat, verbose=args.verbose,
                                     overwrite=args.overwrite,
                                     log_handle=LOGGER.handle,
                                     dry_run=args.dry_run)
            else:
                # .cue oder .gdi -> createcd (PS1, Saturn, Dreamcast)
                status = convert_cue_or_gdi(source, kind, verbose=args.verbose,
                                            overwrite=args.overwrite,
                                            log_handle=LOGGER.handle,
                                            dry_run=args.dry_run)

            if status == "ok":
                counters["ok"] += 1
                if args.verify:
                    chd = source.with_suffix(".chd")
                    LOGGER.raw("  Verifiziere ...")
                    if verify_chd(chd):
                        LOGGER.success("  Verifizierung bestanden")
                    else:
                        LOGGER.error("  Verifizierung fehlgeschlagen!")
                        counters["ok"] -= 1
                        counters["fail"] += 1
            elif status == "skip":
                counters["skip"] += 1
            elif status == "dry":
                counters["dry"] += 1
            else:
                counters["fail"] += 1

            LOGGER.raw()

        LOGGER.raw("=" * 56)
        LOGGER.info("Abschlussbericht")
        LOGGER.raw(f"  Verarbeitet:      {len(files)}")
        LOGGER.raw(f"  {GREEN}Erfolgreich:{RESET}      {counters['ok']}")
        LOGGER.raw(f"  {YELLOW}Uebersprungen:{RESET}     {counters['skip']}")
        LOGGER.raw(f"  {RED}Fehlgeschlagen:{RESET}   {counters['fail']}")
        if counters["dry"]:
            LOGGER.raw(f"  Nur Dry-Run:      {counters['dry']}")
        LOGGER.raw("=" * 56)

        exit_code = 1 if counters["fail"] > 0 else 0

    finally:
        LOGGER.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAbgebrochen durch Benutzer.", flush=True)
        sys.exit(130)
