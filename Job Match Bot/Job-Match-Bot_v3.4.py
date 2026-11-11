#!/usr/bin/env python3
"""
Job-Match-Bot v3.4 — DE-Jobs mit Support-Spezialisten-Filter
=============================================================

Personalisierter Telegram-Bot, der Stellenanzeigen in Deutschland sucht,
gegen ein individuelles IT-Profil bewertet und passende Treffer als
Telegram-Nachrichten zustellt.

Quellen:
  * Bundesagentur für Arbeit (öffentliche API, stabil)
  * GermanTechJobs.de (Web-Parsing-Fallback — siehe Hinweis unten)

WARNUNG zu GermanTechJobs:
  GermanTechJobs.de bietet keine öffentliche API an; dieses Modul parst
  die normale Suchseite. Das Scraping ist NICHT offiziell unterstützt,
  die CSS-Selektoren sind heuristisch. Bei Layout-Änderungen der Seite
  funktioniert das Parsing nicht mehr. Bitte respektiere die
  robots.txt der Seite und die Host-spezifischen Bedingungen. Wer das
  Modul nicht nutzen möchte, kann es in SOURCES deaktivieren
  ("germantechjobs": False).

START:
    python3 job_matcher.py

ABHÄNGIGKEITEN:
    Debian/Ubuntu:  sudo apt install python3-requests python3-dotenv \
                        python3-beautifulsoup4 python3-lxml
    Arch/CachyOS:   sudo pacman -S python-requests python-dotenv \
                        python-beautifulsoup4 python-lxml
    Fedora:         sudo dnf install python3-requests python3-dotenv \
                        python3-beautifulsoup4 python-lxml

TELEGRAM SETUP (.env im Skriptverzeichnis):
    TELEGRAM_BOT_TOKEN=123456789:AAE_dein_token_hier
    TELEGRAM_CHAT_ID=987654321

  Token:            Via @BotFather auf Telegram erstellen (/newbot).
  Chat-ID:          Dem Bot einmal schreiben und
                    https://api.telegram.org/bot<TOKEN>/getUpdates
                    im Browser öffnen — dort steht die "chat":{"id":...}.

HINWEIS: Diese Datei steht unter The Unlicense (Public Domain).
"""

import os
import re
import sys
import json
import time
import signal
import hashlib
import traceback
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, asdict

import requests
from dotenv import load_dotenv

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    print("[WARN] 'beautifulsoup4' fehlt. jobvector/GermanTechJobs werden nicht funktionieren.")
    print("       Installiere: python3-beautifulsoup4 und python3-lxml (siehe README)")

load_dotenv()

# ══════════════════════════════════════════════════════════════════
#  KONFIGURATION
# ══════════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BATCH_SIZE = 10
DAILY_RUN_TIME = "08:00"
POLL_TIMEOUT = 25
STATE_FILE = ".job_state.json"
MIN_SCORE = 30

# ══════════════════════════════════════════════════════════════════
#  QUELLEN-KONFIGURATION
# ══════════════════════════════════════════════════════════════════

SOURCES = {
    "jobsuche_api": True,        # Bundesagentur für Arbeit (stabil, DE-native)
    "germantechjobs": True,      # GermanTechJobs (Scraping — siehe Warnung oben)
}

# ══════════════════════════════════════════════════════════════════
#  SUCHBEGRIFFE (v3.4: Support-Rollen hinzugefügt)
# ══════════════════════════════════════════════════════════════════

SEARCH_TERMS = [
    "Systemadministrator",
    "IT-Administrator",
    "System Engineer",
    "Infrastructure",
    "DevOps",
    "IT-Security Analyst",
    "SOC Analyst",
    "Network Administrator",
    # v3.4: Support-Spezialisten
    "IT Support Spezialist",
    "IT-Support",
    "2nd Level Support",
    "Second Level Support",
]

# ══════════════════════════════════════════════════════════════════
#  MATCHING-REGELN
# ══════════════════════════════════════════════════════════════════

TITLE_MUST_MATCH = [
    "administrator", "systemadmin", "sysadmin", "system engineer",
    "systems engineer", "systembetreuer", "fachinformatiker",
    "infrastructure", "infrastruktur", "devops", "cloud engineer",
    "cloud administrator", "network engineer", "netzwerk",
    "security analyst", "soc analyst", "security engineer",
    "cybersecurity", "it-security", "it security",
    "penetration", "pentest", "pen tester",
    "it support", "it-support", "support engineer", "technical support",
    "service desk", "helpdesk", "help desk", "desktop support",
    "linux", "windows server", "vmware", "proxmox", "active directory",
    "it operations", "it-operations", "it specialist", "it-spezialist",
    # v3.4: Support-Rollen
    "support spezialist", "it-supporter", "2nd level", "second level", "level 2",
]

TITLE_EXCLUDE = [
    "nurse", "physician", "doctor", "medical", "clinical", "pharmac",
    "marketing", "sales", "account executive", "business development",
    "recruiter", "talent", "copywriter", "content writer", "social media",
    "customer care", "customer service", "call center", "teacher", "tutor",
    "driver", "warehouse", "bookkeep", "accountant", "payroll",
    "commissioning agent", "mechanical", "civil engineer", "electrician",
    "legal", "attorney", "paralegal", "insurance agent", "real estate",
    # v3.4: Support-Fallen
    "customer support", "sales support", "administrative support",
    "office support", "management support", "project support",
]

PROFILE_TECH = [
    "proxmox", "vmware", "esxi", "microsoft 365", "m365", "exchange",
    "azure ad", "entra id", "powershell", "active directory", "sophos",
    "unifi", "veeam", "bitlocker", "vpn", "zfs", "hyper-v", "intune",
    "windows server", "linux", "samba", "radius", "nps", "firewall",
    "netzwerk", "tcp/ip", "dns", "dhcp", "backup", "virtualisierung",
    # v3.4: Support-Tech
    "ticketing", "servicenow", "jira", "otrs", "freshservice", "zendesk",
]

SECURITY_TERMS = [
    "security", "sicherheit", "pentest", "penetration", "soc ",
    "blue team", "red team", "siem", "wazuh", "vulnerability",
    "schwachstelle", "hardening", "incident response", "forensik",
    "forensics", "oscp", "ejpt", "security+", "comptia",
]


# ══════════════════════════════════════════════════════════════════
#  DATENSTRUKTUR
# ══════════════════════════════════════════════════════════════════

@dataclass
class JobPosting:
    id: str
    title: str
    company: str
    location: str
    remote: bool
    url: str
    description: str
    posted_date: Optional[str]
    source: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ══════════════════════════════════════════════════════════════════
#  HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════════════

def truncate(text: str, length: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= length else text[:length - 1] + "…"


def is_de_location(location: str) -> bool:
    """
    Prüft, ob ein Standort in Deutschland ist.
    """
    loc = (location or "").lower().strip()

    # Explizite Länderangaben
    if "deutschland" in loc or "germany" in loc:
        return True

    # Reine Remote-Angabe ohne Land: akzeptieren
    if loc in ("remote", "homeoffice", "home office", "remote (deutschland)"):
        return True

    # Deutsche Städte (Auszug)
    de_cities = [
        "hamburg", "berlin", "münchen", "munich", "köln", "cologne",
        "frankfurt", "stuttgart", "düsseldorf", "dortmund", "essen",
        "leipzig", "dresden", "bremen", "hannover", "nürnberg", "kiel",
        "bonn", "münster", "karlsruhe", "mannheim", "augsburg", "wiesbaden",
        "mainz", "kassel", "regensburg", "freiburg", "aachen", "lübeck",
    ]
    if any(city in loc for city in de_cities):
        return True

    # Deutsches PLZ-Muster (5 Ziffern)
    if re.search(r"\b\d{5}\b", loc):
        return True

    return False


# ══════════════════════════════════════════════════════════════════
#  MATCH-ENGINE
# ══════════════════════════════════════════════════════════════════

class MatchEngine:
    def calculate_score(self, job: JobPosting) -> Tuple[int, List[str]]:
        title = (job.title or "").lower()
        description = (job.description or "").lower()
        full_text = f"{title} {description}"

        for bad in TITLE_EXCLUDE:
            if bad in title:
                return 0, [f"✗ Ausgeschlossen: '{bad}'"]

        if not any(kw in title for kw in TITLE_MUST_MATCH):
            return 0, ["✗ Kein IT-Bezug im Titel"]

        score = 0
        reasons: List[str] = []

        if job.remote:
            score += 40
            reasons.append("✓ Remote")

        if is_de_location(job.location):
            score += 15
            reasons.append("✓ DE-Standort")

        title_hits = [kw for kw in TITLE_MUST_MATCH if kw in title]
        if title_hits:
            score += min(30, 10 * len(title_hits))
            reasons.append(f"✓ Titel: {title_hits[0]}")

        tech_hits = [t for t in PROFILE_TECH if t in full_text]
        if tech_hits:
            score += min(25, 5 * len(tech_hits))
            reasons.append(f"✓ Stack: {', '.join(tech_hits[:3])}")

        sec_hits = [s for s in SECURITY_TERMS if s in full_text]
        if sec_hits:
            score += min(20, 7 * len(sec_hits))
            reasons.append(f"✓ Security: {', '.join(s.strip() for s in sec_hits[:2])}")

        for term in ("junior", "entry level", "einstieg", "trainee", "berufseinsteiger"):
            if term in title:
                score += 8
                reasons.append("✓ Einstiegsniveau")
                break

        if job.posted_date:
            try:
                posted = datetime.fromisoformat(str(job.posted_date).replace("Z", "+00:00"))
                days_old = (datetime.now() - posted).days
                if days_old <= 2:
                    score += 10
                    reasons.append("✓ Frisch (<2 Tage)")
                elif days_old <= 7:
                    score += 5
                    reasons.append("✓ Neu (<7 Tage)")
            except Exception:
                pass

        return max(0, min(100, score)), reasons


# ══════════════════════════════════════════════════════════════════
#  QUELLEN: BUNDESAGENTUR FÜR ARBEIT
# ══════════════════════════════════════════════════════════════════

def fetch_jobsuche(terms: List[str]) -> List[JobPosting]:
    jobs: List[JobPosting] = []
    headers = {"X-API-Key": "jobboerse-jobsuche", "Accept": "application/json"}

    for term in terms:  # Alle Begriffe durchlaufen
        try:
            r = requests.get(
                "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs",
                headers=headers,
                params={"was": term, "wo": "Deutschland", "size": 100, "page": 1},
                timeout=20,
            )
            if r.status_code != 200:
                continue

            data = r.json()
            liste = data.get("ergebnisliste", [])

            if not isinstance(liste, list):
                continue

            for j in liste[:30]:
                if not isinstance(j, dict):
                    continue

                refnr = j.get("referenznummer") or j.get("refnr") or ""
                if not refnr:
                    continue

                firma_raw = j.get("firma")
                company = str(firma_raw.get("name") or "Unbekannt") if isinstance(firma_raw, dict) else str(firma_raw or "Unbekannt")

                anschrift_raw = j.get("anschrift")
                location = str(
                    anschrift_raw.get("plzOrtName") or anschrift_raw.get("ort") or "Deutschland"
                ) if isinstance(anschrift_raw, dict) else str(j.get("ort") or "Deutschland")

                if not is_de_location(location):
                    continue

                jobs.append(JobPosting(
                    id=f"jobsuche_{refnr}",
                    title=str(j.get("titel") or ""),
                    company=company,
                    location=location,
                    remote=bool(j.get("homeofficemoeglich", False)),
                    url=f"https://www.arbeitsagentur.de/jobsuche/suche?id={refnr}",
                    description=str(j.get("beruf") or ""),
                    posted_date=j.get("veroeffentlichungsdatum"),
                    source="jobsuche_de",
                ))
        except Exception as e:
            print(f"  [Jobsuche DE] Fehler bei '{term}': {e}")

    return jobs

# ══════════════════════════════════════════════════════════════════
#  QUELLEN: GERMANTECHJOBS.DE (Web-Scraping)
# ══════════════════════════════════════════════════════════════════

def fetch_germantechjobs(terms: List[str]) -> List[JobPosting]:
    """
    GermanTechJobs.de Scraper — v3.4: terms[:8] statt terms[:3] + time.sleep(1)
    HINWEIS: Heuristisches Parsing ohne offizielle API. Kann bei
    Layout-Änderungen der Seite brechen.
    """
    jobs: List[JobPosting] = []

    if not HAS_BS4:
        print("  [GermanTechJobs] beautifulsoup4 fehlt, überspringe")
        return jobs

    headers = {"User-Agent": "Mozilla/5.0 (compatible; JobMatcherBot/1.0; +private)"}

    # v3.4: 8 Begriffe statt 3, mit Delay dazwischen
    for idx, term in enumerate(terms[:8]):
        try:
            if idx > 0:
                time.sleep(1)  # Höflich bleiben

            url = f"https://germantechjobs.de/de/jobs/search?query={term.replace(' ', '+')}"

            r = requests.get(url, headers=headers, timeout=15)

            if r.status_code != 200:
                print(f"  [GermanTechJobs] HTTP {r.status_code} bei '{term}'")
                continue

            soup = BeautifulSoup(r.text, "lxml")

            job_entries = soup.find_all(["div", "article", "li"], class_=lambda c: c and ("job" in c.lower() or "listing" in c.lower() or "item" in c.lower()))

            # Fallback
            if not job_entries:
                job_entries = soup.find_all("a", href=re.compile(r"/jobs/|/job/", re.I))

            print(f"  [GermanTechJobs] '{term}': {len(job_entries)} potenzielle Einträge")

            for jdx, entry in enumerate(job_entries[:20]):
                try:
                    if entry.name == "a":
                        title = entry.get_text(strip=True)
                        url_entry = entry.get("href", "")
                    else:
                        title_elem = entry.find(["h2", "h3", "h4"])
                        title = title_elem.get_text(strip=True) if title_elem else ""

                        url_elem = entry.find("a", href=True)
                        url_entry = url_elem.get("href", "") if url_elem else ""

                    if not title or not url_entry:
                        continue

                    if url_entry.startswith("/"):
                        url_entry = f"https://germantechjobs.de{url_entry}"
                    elif url_entry.startswith("http") is False:
                        continue

                    company = entry.get("data-company", "GermanTechJobs")
                    location = entry.get("data-location", "Deutschland")

                    jobs.append(JobPosting(
                        id=f"germantech_{idx}_{jdx}_{hashlib.md5(url_entry.encode()).hexdigest()}",
                        title=title,
                        company=company,
                        location=location,
                        remote="Remote" in location or "remote" in location.lower(),
                        url=url_entry,
                        description="",
                        posted_date=None,
                        source="germantechjobs",
                    ))
                except Exception:
                    continue

        except Exception as e:
            print(f"  [GermanTechJobs] Fehler bei '{term}': {e}")

    return jobs


# ══════════════════════════════════════════════════════════════════
#  ZUSTAND, TELEGRAM, NACHRICHTEN, MAIN LOOP
# ══════════════════════════════════════════════════════════════════

def default_state() -> Dict[str, Any]:
    return {
        "pending": [],
        "seen": [],
        "update_offset": 0,
        "last_fetch": None,
        "sent_total": 0,
        "initialized": False,
    }

def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = default_state()
            base.update(data)
            return base
        except Exception as e:
            print(f"[WARN] Zustandsdatei unlesbar ({e}), starte frisch.")
    return default_state()

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[WARN] Zustand konnte nicht gespeichert werden: {e}")

TG_API = "https://api.telegram.org/bot{token}/{method}"

def tg_call(method: str, payload: Dict[str, Any], timeout: int = 30) -> Optional[Dict[str, Any]]:
    if not TELEGRAM_BOT_TOKEN:
        print("[FEHLER] TELEGRAM_BOT_TOKEN fehlt in .env")
        return None

    url = TG_API.format(token=TELEGRAM_BOT_TOKEN, method=method)
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            data = r.json()
            if data.get("ok"):
                return data
            if r.status_code == 429:
                wait = data.get("parameters", {}).get("retry_after", 5)
                print(f"[TELEGRAM] Rate-Limit, warte {wait}s")
                time.sleep(wait + 1)
                continue
            print(f"[TELEGRAM] Fehler {r.status_code}: {data.get('description')}")
            return None
        except Exception as e:
            print(f"[TELEGRAM] Netzwerkfehler: {e}")
            time.sleep(3)
    return None

def send_message(text: str) -> bool:
    if len(text) > 4096:
        text = text[:4090] + "\n…"
    result = tg_call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text})
    return result is not None

def get_updates(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"timeout": POLL_TIMEOUT}
    if state.get("update_offset"):
        params["offset"] = state["update_offset"]

    try:
        url = TG_API.format(token=TELEGRAM_BOT_TOKEN, method="getUpdates")
        r = requests.get(url, params=params, timeout=POLL_TIMEOUT + 15)
        data = r.json()
        if not data.get("ok"):
            print(f"[TELEGRAM] getUpdates: {data.get('description')}")
            return []
        updates = data.get("result", [])
        if updates:
            state["update_offset"] = updates[-1]["update_id"] + 1
            save_state(state)
        return updates
    except requests.exceptions.ReadTimeout:
        return []
    except Exception as e:
        print(f"[TELEGRAM] Polling-Fehler: {e}")
        time.sleep(5)
        return []

def clear_backlog(state: Dict[str, Any]) -> None:
    try:
        url = TG_API.format(token=TELEGRAM_BOT_TOKEN, method="getUpdates")
        r = requests.get(url, params={"timeout": 0}, timeout=15)
        data = r.json()
        updates = data.get("result", [])
        if updates:
            state["update_offset"] = updates[-1]["update_id"] + 1
            print(f"[TELEGRAM] {len(updates)} alte Nachrichten übersprungen.")
    except Exception as e:
        print(f"[TELEGRAM] Backlog-Bereinigung fehlgeschlagen: {e}")

def format_jobs(items: List[Dict[str, Any]], start_no: int, header: str) -> str:
    lines = [header, ""]
    for idx, item in enumerate(items, start=start_no):
        job = item["job"]
        score = item["score"]
        reasons = item.get("reasons", [])[:3]
        flag = "🌐" if job.get("remote") else "🏢"

        lines.append(f"{idx}. [{score}/100] {truncate(job.get('title', ''), 85)}")
        lines.append(f"   {flag} {truncate(job.get('company', 'Unbekannt'), 45)}"
                     f" · {truncate(job.get('location', ''), 30)}")
        if reasons:
            lines.append(f"   {' | '.join(reasons)}")
        lines.append(f"   {job.get('url', '')}")
        lines.append("")
    return "\n".join(lines).strip()

def send_batch(state: Dict[str, Any], count: int = BATCH_SIZE,
               announce: bool = True) -> int:
    pending = state.get("pending", [])
    if not pending:
        if announce:
            send_message("📭 Warteschlange ist leer.\n\n"
                         "/fetch für eine neue Suche · /status für Übersicht")
        return 0

    batch = pending[:count]
    state["pending"] = pending[count:]
    start_no = state.get("sent_total", 0) + 1
    state["sent_total"] = start_no + len(batch) - 1
    save_state(state)

    rest = len(state["pending"])
    header = f"🎯 Job-Matches {start_no}–{state['sent_total']} (DE-Jobs)"
    if rest:
        header += f"\n({rest} weitere in der Warteschlange — /more)"

    send_message(format_jobs(batch, start_no, header))
    print(f"[BOT] {len(batch)} Jobs gesendet, {rest} verbleiben.")
    return len(batch)

def run_search(state: Dict[str, Any], notify_empty: bool = True) -> int:
    print("\n" + "─" * 55)
    print(f"🔍 Suche gestartet: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("─" * 55)

    all_jobs: List[JobPosting] = []

    if SOURCES["jobsuche_api"]:
        print("[Bundesagentur DE] …")
        found = fetch_jobsuche(SEARCH_TERMS)
        print(f"  → {len(found)} Jobs")
        all_jobs.extend(found)

    if SOURCES["germantechjobs"]:
        print("[GermanTechJobs] …")
        found = fetch_germantechjobs(SEARCH_TERMS)
        print(f"  → {len(found)} Jobs")
        all_jobs.extend(found)

    print(f"\nGesamt geholt: {len(all_jobs)}")

    seen = set(state.get("seen", []))
    unique: List[JobPosting] = []
    for job in all_jobs:
        h = hashlib.md5(f"{job.id}|{job.title}".encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        unique.append(job)

    print(f"Nach Dublettenfilter: {len(unique)} neue Jobs")

    engine = MatchEngine()
    scored: List[Dict[str, Any]] = []
    for job in unique:
        score, reasons = engine.calculate_score(job)
        if score >= MIN_SCORE:
            scored.append({"job": job.to_dict(), "score": score, "reasons": reasons})

    scored.sort(key=lambda x: x["score"], reverse=True)

    state["seen"] = list(seen)
    state["last_fetch"] = datetime.now().isoformat()
    state["pending"] = state.get("pending", []) + scored
    save_state(state)

    print(f"Treffer über Schwelle ({MIN_SCORE}): {len(scored)}")
    if scored:
        print("\nTop 5:")
        for item in scored[:5]:
            print(f"  [{item['score']}] {item['job']['title'][:70]}")

    if scored:
        send_message(f"🔍 Neue Suche abgeschlossen (v3.4 Support)\n"
                     f"{len(scored)} neue DE-Treffer gefunden.")
        send_batch(state)
    elif notify_empty:
        send_message("🔍 Neue Suche abgeschlossen — keine neuen Treffer.")

    return len(scored)

HELP_TEXT = """🤖 Job-Match-Bot v3.4 — Befehle

/more — nächste 10 Treffer (DE)
/all — alle verbleibenden Treffer
/top — die 5 besten aus der Warteschlange
/status — Warteschlange & letzte Suche
/fetch — sofort neu suchen (DE-Jobs nur)
/reset — Cache leeren und alles neu suchen
/help — diese Übersicht

Automatische Suche täglich um {time} Uhr.
Nur Deutschland + Remote-Stellen.

Quellen: Bundesagentur, GermanTechJobs
v3.4: Support-Rollen (IT Support, 2nd/Second Level) aktiviert"""

def handle_command(text: str, state: Dict[str, Any]) -> None:
    cmd = text.strip().split()[0].lower()
    cmd = cmd.split("@")[0]

    print(f"[BOT] Befehl empfangen: {cmd}")

    if cmd in ("/start", "/help"):
        send_message(HELP_TEXT.format(time=DAILY_RUN_TIME))

    elif cmd == "/more":
        if not state.get("pending"):
            send_message("📭 Warteschlange leer. /fetch für neue Suche.")
        else:
            send_batch(state, BATCH_SIZE, announce=False)

    elif cmd == "/all":
        pending = state.get("pending", [])
        if not pending:
            send_message("📭 Warteschlange leer.")
            return
        total = len(pending)
        send_message(f"📤 Sende alle {total} verbleibenden Treffer …")
        while state.get("pending"):
            send_batch(state, BATCH_SIZE, announce=False)
            time.sleep(1.2)

    elif cmd == "/top":
        pending = state.get("pending", [])
        if not pending:
            send_message("📭 Warteschlange leer.")
            return
        best = sorted(pending, key=lambda x: x["score"], reverse=True)[:5]
        send_message(format_jobs(best, 1, "⭐ Top 5 DE-Jobs"))

    elif cmd == "/status":
        pending = state.get("pending", [])
        last = state.get("last_fetch")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                last_txt = f"{last_dt:%d.%m.%Y %H:%M}"
                age_h = (datetime.now() - last_dt).total_seconds() / 3600
                last_txt += f" (vor {age_h:.1f} h)"
            except Exception:
                last_txt = str(last)
        else:
            last_txt = "noch nie"

        send_message(
            f"📊 Status (DE-Jobs, v3.4)\n\n"
            f"In Warteschlange: {len(pending)}\n"
            f"Insgesamt gemeldet: {state.get('sent_total', 0)}\n"
            f"Cache: {len(state.get('seen', []))}\n"
            f"Letzte Suche: {last_txt}\n"
            f"Quellen: jobsuche.de, GermanTechJobs"
        )

    elif cmd == "/fetch":
        send_message("🔍 Suche nach DE-Jobs läuft …")
        run_search(state, notify_empty=True)

    elif cmd == "/reset":
        state["seen"] = []
        state["pending"] = []
        state["sent_total"] = 0
        save_state(state)
        send_message("♻️ Cache geleert. Starte neue DE-Suche …")
        run_search(state, notify_empty=True)

    else:
        send_message(f"❓ Unbekannter Befehl: {cmd}\n\n/help für die Übersicht")

running = True

def shutdown(signum, frame):
    global running
    print("\n[BOT] Beende …")
    running = False

def next_run_datetime(hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target

def main() -> None:
    global running

    print("=" * 55)
    print("🤖 Job-Match-Bot v3.4 — DE-Jobs (Support-Rollen)")
    print("=" * 55)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[FEHLER] TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt.")
        print("         Prüfe die .env-Datei im aktuellen Verzeichnis.")
        sys.exit(1)

    me = tg_call("getMe", {}, timeout=15)
    if not me:
        print("[FEHLER] Telegram nicht erreichbar — Token prüfen.")
        sys.exit(1)
    print(f"Verbunden als @{me['result'].get('username')}")

    state = load_state()

    if not state.get("initialized"):
        clear_backlog(state)
        state["initialized"] = True
        save_state(state)

    if not state.get("pending"):
        run_search(state, notify_empty=False)
    else:
        print(f"[BOT] {len(state['pending'])} Jobs aus früherem Lauf.")

    next_run = next_run_datetime(DAILY_RUN_TIME)
    print(f"[BOT] Nächste automatische Suche: {next_run:%d.%m.%Y %H:%M}")
    print("[BOT] Warte auf Befehle … (Strg+C zum Beenden)\n")

    send_message(
        f"🤖 Bot ist online (DE-Jobs, v3.4).\n"
        f"{len(state.get('pending', []))} Treffer in Warteschlange.\n\n"
        f"/more · /all · /status · /help"
    )

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while running:
        try:
            for update in get_updates(state):
                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue
                if str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
                    continue
                text = msg.get("text", "")
                if text.startswith("/"):
                    handle_command(text, state)
        except Exception:
            print("[FEHLER] Unerwarteter Fehler im Loop:")
            traceback.print_exc()
            time.sleep(5)

        if datetime.now() >= next_run:
            try:
                run_search(state, notify_empty=True)
            except Exception:
                traceback.print_exc()
            next_run = next_run_datetime(DAILY_RUN_TIME)
            print(f"[BOT] Nächste Suche: {next_run:%d.%m.%Y %H:%M}")

    print("[BOT] Beendet.")

if __name__ == "__main__":
    main()
