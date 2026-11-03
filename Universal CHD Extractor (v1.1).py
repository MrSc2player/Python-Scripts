#!/usr/bin/env python3
"""
Universal CHD Extractor v1.1
Wandelt CHD zurueck in ISO (DVD) bzw. Cue+Bin / GDI+Raw (CD).

Hinweise zur technischen Umsetzung:
  - PS2/PSP-CHD (DVD-basiert) -> 'chdman extractdvd' -> .iso
  - PS1/Saturn/Dreamcast-CHD (CD-basiert) -> 'chdman extractcd' -> .cue + .bin
    (Dreamcast-GDIs lassen sich aus dem erzeugten .cue per chdman/gditools
     rekonstruieren; chdman selbst liefert immer .cue + .bin)

WICHTIG zu 'extractcd':
  chdman erwartet bei -o den NAMEN DER ZIELDATEI (z.B. spiel.cue),
  nicht den Zielordner. chdman leitet die Track-Namen daraus ab.

Verwendung:
    python3 universal_chd_extractor.py [ORDNER] [Optionen]
    python3 universal_chd_extractor.py            # interaktives Menue

Beispiele:
    python3 universal_chd_extractor.py /chds/ps2 --ps2
    python3 universal_chd_extractor.py /chds/ps1 --ps1 -v
    python3 universal_chd_extractor.py /chds/all --auto --verify
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


def detect_chd_type(chd_file: Path) -> str:
    """Erkennt, ob ein CHD DVD- oder CD-basiert ist.

    Nutzt 'chdman info'. Rueckgabe: 'dvd' oder 'cd'.
    """
    try:
        result = subprocess.run(
            ["chdman", "info", "-i", str(chd_file)],
            capture_output=True, text=True, timeout=30
        )
    except Exception:
        return "cd"

    text = ((result.stdout or "") + (result.stderr or "")).lower()

    if "dvd" in text:
        return "dvd"
    return "cd"


def run_chdman_live(cmd, log_handle=None):
    """Fuehrt chdman aus und zeigt die Ausgabe live im Terminal."""
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


# ---------------------------------------------------------------- Ausgabe-Pruefung
def _existing_targets(chd_file: Path, chd_type: str):
    """Liefert die Zieldateien, die eine Extraktion erzeugen wuerde."""
    stem = chd_file.stem
    parent = chd_file.parent

    if chd_type == "dvd":
        return [parent / f"{stem}.iso"]

    targets = [parent / f"{stem}.cue"]
    # Moegliche Track-Dateien dazu sammeln
    for pattern in (f"{stem}*.bin", f"{stem}*.raw", f"{stem}*.img"):
        targets.extend(sorted(parent.glob(pattern)))
    return targets


def _cleanup_targets(targets, logger):
    """Loescht vorhandene Zieldateien (fuer --overwrite)."""
    for target in targets:
        if target.exists():
            try:
                target.unlink()
            except OSError as exc:
                logger.error(f"  {target.name} nicht loeschbar: {exc}")
                return False
    return True


# ---------------------------------------------------------------- Extraktion
def extract_dvd(chd_file: Path, verbose: bool = False,
                overwrite: bool = False, log_handle=None,
                dry_run: bool = False) -> str:
    """DVD-CHD (PS2/PSP) -> .iso via extractdvd.

    Hinweis: PS2 und PSP nutzen denselben chdman-Befehl (extractdvd),
    da beide als DVD-Images behandelt werden.

    Rueckgabe: 'ok' | 'skip' | 'fail' | 'dry'
    """
    logger = LOGGER
    target = chd_file.with_suffix(".iso")

    if target.exists():
        if not overwrite:
            logger.raw(f"{YELLOW}[SKIP]{RESET} {chd_file.name} "
                       f"(.iso existiert bereits)")
            return "skip"
        if not _cleanup_targets([target], logger):
            return "fail"

    if dry_run:
        logger.raw(f"{CYAN}[DRY-RUN]{RESET} DVD: {chd_file.name} "
                   f"-> {target.name}")
        return "dry"

    cmd = ["chdman", "extractdvd",
           "-i", str(chd_file),
           "-o", str(target)]

    logger.raw(f"{CYAN}[EXTRACT]{RESET} DVD: {chd_file.name} "
               f"-> {target.name}")
    if verbose:
        logger.raw(f"  Befehl: {' '.join(cmd)}")

    returncode = run_chdman_live(cmd, log_handle=log_handle)

    if returncode == 0:
        logger.raw(f"  {GREEN}\u2713 Fertig{RESET}")
        return "ok"

    logger.error(f"  \u2717 Fehlgeschlagen: {chd_file.name} "
                 f"(Exit-Code {returncode})")
    _remove_partial(target, logger)
    return "fail"


def extract_cd(chd_file: Path, kind: str, verbose: bool = False,
               overwrite: bool = False, log_handle=None,
               dry_run: bool = False) -> str:
    """CD-CHD (PS1/Saturn/Dreamcast) -> .cue + .bin via extractcd.

    Hinweis: PS1, Saturn und Dreamcast nutzen denselben chdman-Befehl
    (extractcd), da alle CD-basierte Systeme sind.

    WICHTIG: -o erwartet den Namen der ZIELDATEI (die .cue), nicht den
    Ordner. chdman leitet die Track-Namen (.bin) daraus ab.

    Rueckgabe: 'ok' | 'skip' | 'fail' | 'dry'
    """
    logger = LOGGER
    cue_target = chd_file.with_suffix(".cue")

    existing = _existing_targets(chd_file, "cd")
    if cue_target.exists():
        if not overwrite:
            logger.raw(f"{YELLOW}[SKIP]{RESET} {chd_file.name} "
                       f"(.cue existiert bereits)")
            return "skip"
        if not _cleanup_targets(existing, logger):
            return "fail"

    if dry_run:
        logger.raw(f"{CYAN}[DRY-RUN]{RESET} {kind}: {chd_file.name} "
                   f"-> {cue_target.name} + Track-Dateien")
        return "dry"

    cmd = ["chdman", "extractcd",
           "-i", str(chd_file),
           "-o", str(cue_target)]

    logger.raw(f"{CYAN}[EXTRACT]{RESET} {kind}: {chd_file.name} "
               f"-> {cue_target.name} + Tracks")
    if verbose:
        logger.raw(f"  Befehl: {' '.join(cmd)}")

    returncode = run_chdman_live(cmd, log_handle=log_handle)

    if returncode == 0:
        logger.raw(f"  {GREEN}\u2713 Fertig{RESET}")

        if verbose:
            produced = [
                p for p in _existing_targets(chd_file, "cd")
                if p.exists()
            ]
            if produced:
                logger.raw("  Erzeugte Dateien:")
                for item in produced:
                    size = item.stat().st_size
                    logger.raw(f"    - {item.name} ({size:,} bytes)")

        return "ok"

    logger.error(f"  \u2717 Fehlgeschlagen: {chd_file.name} "
                 f"(Exit-Code {returncode})")
    # Halbfertige Teile wieder entfernen
    for leftover in _existing_targets(chd_file, "cd"):
        if leftover.exists():
            _remove_partial(leftover, logger)
    return "fail"


def _remove_partial(target: Path, logger):
    if target.exists():
        try:
            target.unlink()
            logger.raw(f"  (unvollstaendige {target.name} entfernt)")
        except OSError as exc:
            logger.warn(f"  {target.name} nicht loeschbar: {exc}")


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
  Ziel waehlen (Quelle sind immer .chd Dateien):

    1) DVD-CHD  -> ISO            (PS2 / PSP)
    2) CD-CHD   -> Cue + Bin      (PS1 / Saturn / Dreamcast)
    3) Alles automatisch (Typ per 'chdman info' erkennen)
    4) Beenden
"""


def interactive_menu():
    """Fragt Modus und Ordner ab. Rueckgabe: (mode, base) oder (None, None)."""
    print(f"{BOLD}{CYAN}Universal CHD Extractor{RESET}")
    print(MENU)

    choice = input("  Auswahl (1-4): ").strip()
    if choice == "4":
        return None, None

    modes = {"1": "dvd", "2": "cd", "3": "auto"}
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
    """Sammelt .chd Dateien und filtert nach erkanntem Typ."""
    chd_files = [p for p in base.rglob("*")
                 if p.is_file() and p.suffix.lower() == ".chd"]

    if mode == "auto":
        return sorted(chd_files, key=lambda p: str(p).lower())

    wanted = "dvd" if mode == "dvd" else "cd"
    filtered = [p for p in chd_files if detect_chd_type(p) == wanted]
    return sorted(filtered, key=lambda p: str(p).lower())


def main():
    global LOGGER

    parser = argparse.ArgumentParser(
        description="Universal CHD Extractor: CHD -> ISO / Cue+Bin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Beispiele:\n"
               "  python3 universal_chd_extractor.py\n"
               "  python3 universal_chd_extractor.py /chds/ps2 --ps2\n"
               "  python3 universal_chd_extractor.py /chds/ps1 --ps1 -v\n"
               "  python3 universal_chd_extractor.py /chds/all --auto --verify\n"
    )

    parser.add_argument("directory", nargs="?", default=None,
                        help="Startverzeichnis (ohne Angabe: Menue)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ps2", action="store_true",
                       help="Nur DVD-CHDs (PS2) zu ISO extrahieren")
    group.add_argument("--psp", action="store_true",
                       help="Nur DVD-CHDs (PSP) zu ISO extrahieren")
    group.add_argument("--ps1", action="store_true",
                       help="Nur CD-CHDs (PS1) zu Cue+Bin extrahieren")
    group.add_argument("--saturn", action="store_true",
                       help="Nur CD-CHDs (Saturn) zu Cue+Bin extrahieren")
    group.add_argument("--dreamcast", action="store_true",
                       help="Nur CD-CHDs (Dreamcast) zu Cue+Bin extrahieren")
    group.add_argument("--auto", action="store_true",
                       help="Alle CHDs, Typ automatisch erkennen")

    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Befehl und erzeugte Dateien anzeigen")
    parser.add_argument("--overwrite", action="store_true",
                        help="Bestehende Zieldateien ueberschreiben")
    parser.add_argument("--no-color", action="store_true",
                        help="Farbausgabe deaktivieren")
    parser.add_argument("--log", metavar="DATEI",
                        help="Zusaetzlich in Logdatei schreiben")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur anzeigen, was extrahiert wuerde")
    parser.add_argument("--verify", action="store_true",
                        help="CHD vor der Extraktion verifizieren")

    args = parser.parse_args()

    if args.no_color:
        disable_colors()
    else:
        maybe_disable_colors_on_pipe()

    # Modus bestimmen
    if args.directory is None:
        mode, base = interactive_menu()
        if mode is None:
            sys.exit(0)
    else:
        base = Path(args.directory).expanduser().resolve()
        if args.ps2 or args.psp:
            mode = "dvd"
        elif args.ps1 or args.saturn or args.dreamcast:
            mode = "cd"
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
        LOGGER.info("Starte Universal CHD Extractor")
        LOGGER.info(f"Verzeichnis: {base}")
        LOGGER.info(f"Modus:       {mode.upper()}")

        if not check_chdman(LOGGER):
            sys.exit(1)

        if log_path:
            LOGGER.info(f"Logdatei:    {log_path}")

        files = collect_files(base, mode)
        if not files:
            LOGGER.warn(f"Keine passenden CHD-Dateien in {base} gefunden")
            sys.exit(0)

        LOGGER.info(f"Gefundene CHD-Dateien: {len(files)}")
        LOGGER.raw()

        counters = {"ok": 0, "skip": 0, "fail": 0, "dry": 0}
        last_dir = base

        for chd in files:
            if chd.parent != last_dir:
                last_dir = chd.parent
                try:
                    rel = chd.parent.relative_to(base)
                except ValueError:
                    rel = chd.parent
                LOGGER.raw(f"{MAGENTA}# {rel}{RESET}")

            chd_type = detect_chd_type(chd)

            if args.verify:
                LOGGER.raw("  Verifiziere CHD ...")
                if not verify_chd(chd):
                    LOGGER.error("  Verifizierung fehlgeschlagen - "
                                 "Datei wird uebersprungen")
                    counters["fail"] += 1
                    LOGGER.raw()
                    continue

            if chd_type == "dvd":
                status = extract_dvd(chd, verbose=args.verbose,
                                     overwrite=args.overwrite,
                                     log_handle=LOGGER.handle,
                                     dry_run=args.dry_run)
            else:
                kind = "CD"
                status = extract_cd(chd, kind, verbose=args.verbose,
                                    overwrite=args.overwrite,
                                    log_handle=LOGGER.handle,
                                    dry_run=args.dry_run)

            if status == "ok":
                counters["ok"] += 1
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
