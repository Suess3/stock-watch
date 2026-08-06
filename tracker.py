#!/usr/bin/env python3
"""
ETF/Index-Watcher
=================
Holt aktuelle Kurse (S&P 500, Vanguard FTSE All-World, ...) und schickt eine
E-Mail, sobald ein Kurs unter einen definierten Schwellwert faellt.

Modi:
    python tracker.py --once          einmal pruefen (fuer GitHub Actions / Cron)
    python tracker.py --loop          Dauerlauf, prueft alle N Minuten
    python tracker.py --dry-run       prueft, verschickt aber keine Mail
    python tracker.py --test-mail     verschickt eine Testmail

Konfiguration: config.json (siehe config.example.json) oder Umgebungsvariablen.
Umgebungsvariablen haben immer Vorrang -> so bleiben Passwoerter in GitHub Secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    sys.exit("yfinance fehlt.  ->  pip install -r requirements.txt")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

DEFAULT_CONFIG = {
    "smtp": {
        "host": "smtp.gmail.com",
        "port": 465,
        "user": "",
        "password": "",
        "from": "",
        "to": "",
    },
    "interval_minutes": 10,
    # Wie lange nach einer Alarm-Mail Ruhe ist, solange der Kurs unter der
    # Schwelle bleibt. Sonst kaeme alle 10 Minuten eine Mail.
    "cooldown_hours": 12,
    # Datum (ISO, z.B. "2026-12-31"), ab dem nicht mehr geprueft wird.
    # Leer = laeuft unbegrenzt.
    "run_until": "",
    # Mindestabstand zwischen zwei Punkten der Kurshistorie in state.json.
    # Kleiner Wert = feinere Grafik, aber mehr Commits durch den Workflow.
    "history_minutes": 30,
    # Zweiter Alarmtyp: nicht ein absoluter Kurs, sondern die Veraenderung
    # ueber einen Zeitraum. Beide Prozentwerte sind Betraege.
    "change_alert": {
        "enabled": True,
        "window_days": 1,
        "drop_percent": 1.0,
        "rise_percent": 1.0,
    },
    "watchlist": [
        {
            "name": "S&P 500",
            "symbol": "^GSPC",
            "threshold": 5000.0,
            "currency": "USD",
        },
        {
            "name": "Vanguard FTSE All-World (VWCE)",
            "symbol": "VWCE.DE",
            "threshold": 100.0,
            "currency": "EUR",
        },
    ],
}


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy

    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as fh:
            cfg = deep_merge(cfg, json.load(fh))

    # Umgebungsvariablen gewinnen (GitHub Actions Secrets)
    env_map = {
        "SMTP_HOST": ("smtp", "host", str),
        "SMTP_PORT": ("smtp", "port", int),
        "SMTP_USER": ("smtp", "user", str),
        "SMTP_PASS": ("smtp", "password", str),
        "MAIL_FROM": ("smtp", "from", str),
        "MAIL_TO": ("smtp", "to", str),
    }
    for env_key, (section, field, cast) in env_map.items():
        raw = os.environ.get(env_key)
        if raw:
            cfg[section][field] = cast(raw)

    if os.environ.get("COOLDOWN_HOURS"):
        cfg["cooldown_hours"] = float(os.environ["COOLDOWN_HOURS"])

    # Schwellwerte lassen sich auch per Env setzen: THRESHOLD_^GSPC=5000
    for item in cfg["watchlist"]:
        env_key = "THRESHOLD_" + item["symbol"].replace("^", "").replace(".", "_").upper()
        if os.environ.get(env_key):
            item["threshold"] = float(os.environ[env_key])

    if not cfg["smtp"]["from"]:
        cfg["smtp"]["from"] = cfg["smtp"]["user"]

    # Google zeigt App-Passwoerter in Viererbloecken an. Kopiert man sie aus
    # dem Browser, landen dort echte oder geschuetzte Leerzeichen (\xa0), an
    # denen der SMTP-Login scheitert - beim geschuetzten schon vor dem Senden.
    # str.split() erkennt beide Varianten.
    if cfg["smtp"].get("password"):
        cfg["smtp"]["password"] = "".join(str(cfg["smtp"]["password"]).split())

    # Adressen vertragen ebenfalls keine unsichtbaren Randzeichen
    for field in ("host", "user", "from", "to"):
        if cfg["smtp"].get(field):
            cfg["smtp"][field] = str(cfg["smtp"][field]).strip()

    return cfg


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# Status (damit nicht alle 10 Minuten dieselbe Mail kommt)
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with STATE_PATH.open(encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


# Kurshistorie liegt unter einem eigenen Schluessel im selben state.json.
# Grund: der Workflow committet diese Datei ohnehin schon zurueck, eine
# zweite Datei haette eine Aenderung an watcher.yml erfordert.
HISTORY_KEY = "__history"
HISTORY_MAX_POINTS = 1500


def record_price(state: dict, symbol: str, price: float, now: datetime,
                 min_gap_minutes: float) -> bool:
    """Haengt einen Kurspunkt an, wenn der letzte lang genug her ist."""
    history = state.setdefault(HISTORY_KEY, {})
    series = history.setdefault(symbol, [])
    value = round(price, 4)

    if series:
        # Ausserhalb der Handelszeiten liefert Yahoo unveraendert den letzten
        # Schlusskurs. Den erneut zu speichern erzeugt nur eine waagrechte
        # Linie und einen Commit ohne Informationsgehalt.
        if series[-1][1] == value:
            return False

        try:
            last = datetime.fromisoformat(series[-1][0])
        except (ValueError, IndexError, TypeError):
            last = None
        if last and (now - last) < timedelta(minutes=min_gap_minutes):
            return False

    series.append([now.isoformat(timespec="minutes"), value])
    del series[:-HISTORY_MAX_POINTS]
    return True


BACKFILL_TRIES_KEY = "__backfill_tries"
BACKFILL_MAX_TRIES = 5


def backfill_history(state: dict, symbol: str, min_points: int = 24) -> bool:
    """Traegt einmalig die letzte Woche halbstuendlich nach.

    Sonst startet die Grafik bei null und braucht Tage, bis sie etwas zeigt.
    Laeuft nur, solange zu wenige Punkte da sind, und gibt nach einigen
    Fehlversuchen auf, damit kein Dauerabruf entsteht.
    """
    history = state.setdefault(HISTORY_KEY, {})
    series = history.setdefault(symbol, [])
    if len(series) >= min_points:
        return False

    tries = state.setdefault(BACKFILL_TRIES_KEY, {})
    if tries.get(symbol, 0) >= BACKFILL_MAX_TRIES:
        return False

    try:
        hist = yf.Ticker(symbol).history(period="7d", interval="30m")
    except Exception as exc:
        tries[symbol] = tries.get(symbol, 0) + 1
        print(f"    -> Historie fuer {symbol} nicht abrufbar ({exc}).")
        return True  # Zaehler hat sich geaendert, also speichern

    points = []
    if hist is not None and not hist.empty:
        for ts, close in hist["Close"].dropna().items():
            stamp = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            stamp = stamp.astimezone(timezone.utc)
            points.append([stamp.isoformat(timespec="minutes"), round(float(close), 4)])

    if not points:
        tries[symbol] = tries.get(symbol, 0) + 1
        print(f"    -> Keine Halbstundenwerte fuer {symbol} erhalten.")
        return True

    # Eigene Messwerte haben Vorrang vor den nachgetragenen.
    merged = {p[0]: p for p in points}
    merged.update({p[0]: p for p in series})
    history[symbol] = [merged[k] for k in sorted(merged)][-HISTORY_MAX_POINTS:]
    tries.pop(symbol, None)
    print(f"    -> {len(points)} Halbstundenwerte der letzten Woche fuer {symbol} nachgetragen.")
    return True


def run_until_passed(cfg: dict, now: datetime) -> bool:
    """True, wenn das konfigurierte Enddatum ueberschritten ist."""
    raw = str(cfg.get("run_until") or "").strip()
    if not raw:
        return False
    try:
        end = datetime.fromisoformat(raw)
    except ValueError:
        print(f"[WARN] run_until '{raw}' ist kein gueltiges Datum - wird ignoriert.")
        return False
    # Reines Datum ohne Uhrzeit meint den ganzen Tag, nicht dessen Beginn.
    if "T" not in raw and " " not in raw:
        end = end.replace(hour=23, minute=59, second=59)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return now > end


# --------------------------------------------------------------------------- #
# Kursabfrage
# --------------------------------------------------------------------------- #
def get_price(symbol: str) -> float:
    """Aktuellen Kurs holen. Erst der schnelle Weg, dann die Historie."""
    ticker = yf.Ticker(symbol)

    try:
        fast = ticker.fast_info
        price = fast.get("last_price") if isinstance(fast, dict) else getattr(fast, "last_price", None)
        if price:
            return float(price)
    except Exception:
        pass

    for period, interval in (("5d", "1h"), ("1mo", "1d")):
        try:
            hist = ticker.history(period=period, interval=interval)
        except Exception:
            continue
        if hist is not None and not hist.empty:
            closes = hist["Close"].dropna()
            if not closes.empty:
                return float(closes.iloc[-1])

    raise RuntimeError(f"Kein Kurs fuer '{symbol}' gefunden (Symbol falsch oder Anbieter down?).")


# --------------------------------------------------------------------------- #
# E-Mail
# --------------------------------------------------------------------------- #
def send_mail(cfg: dict, subject: str, body: str) -> None:
    smtp_cfg = cfg["smtp"]
    missing = [k for k in ("host", "user", "password", "to") if not smtp_cfg.get(k)]
    if missing:
        raise RuntimeError(f"SMTP-Konfiguration unvollstaendig, es fehlt: {', '.join(missing)}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from"] or smtp_cfg["user"]
    msg["To"] = smtp_cfg["to"]
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    port = int(smtp_cfg["port"])
    context = ssl.create_default_context()

    if port == 465:
        with smtplib.SMTP_SSL(smtp_cfg["host"], port, context=context, timeout=30) as server:
            server.login(smtp_cfg["user"], smtp_cfg["password"])
            server.send_message(msg)
    else:  # 587 / STARTTLS
        with smtplib.SMTP(smtp_cfg["host"], port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(smtp_cfg["user"], smtp_cfg["password"])
            server.send_message(msg)


LAST_CHECK_KEY = "__last_check"
CHANGE_MAIL_KEY = "__change_last_mail"

EXTREMES_KEY = "__extremes"
EXTREMES_PERIOD = "5y"
EXTREMES_WINDOWS = (1, 3, 7)
EXTREMES_REFRESH_DAYS = 30


def rolling_extremes(punkte: list, window_days: int) -> tuple:
    """Groesste und kleinste prozentuale Veraenderung ueber das Fenster."""
    lo = hi = 0.0
    j = 0
    for i in range(len(punkte)):
        grenze = punkte[i][0] - timedelta(days=window_days)
        while j + 1 < i and punkte[j + 1][0] <= grenze:
            j += 1
        if j < i and punkte[j][0] <= grenze and punkte[j][1]:
            change = (punkte[i][1] - punkte[j][1]) / punkte[j][1] * 100
            lo = min(lo, change)
            hi = max(hi, change)
    return round(lo, 2), round(hi, 2)


def refresh_extremes(state: dict, symbols: list) -> bool:
    """Ermittelt einmalig aus mehreren Jahren Tagesdaten, wie stark sich die
    Werte historisch je bewegt haben.

    Die kurze Aufzeichnung in state.json taugt dafuer nicht: zwei Wochen
    Seitwaerts sagen nichts darueber, was ein Wert an einem schlechten Tag
    kann - und wuerden die Regler laecherlich eng begrenzen.
    """
    store = state.setdefault(EXTREMES_KEY, {})
    computed = store.get("computed")
    if computed:
        try:
            alter = datetime.now(timezone.utc) - datetime.fromisoformat(computed)
            if alter < timedelta(days=EXTREMES_REFRESH_DAYS) and \
               all(s in store.get("symbols", {}) for s in symbols):
                return False
        except (TypeError, ValueError):
            pass

    ergebnis = dict(store.get("symbols", {}))
    for symbol in symbols:
        try:
            hist = yf.Ticker(symbol).history(period=EXTREMES_PERIOD, interval="1d")
        except Exception as exc:
            print(f"    -> Extremwerte fuer {symbol} nicht abrufbar ({exc}).")
            continue
        if hist is None or hist.empty:
            print(f"    -> Keine Tagesdaten fuer {symbol}.")
            continue

        punkte = []
        for ts, close in hist["Close"].dropna().items():
            stamp = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            punkte.append((stamp.astimezone(timezone.utc), float(close)))
        if len(punkte) < 30:
            continue

        ergebnis[symbol] = {str(w): list(rolling_extremes(punkte, w)) for w in EXTREMES_WINDOWS}
        spanne = (punkte[-1][0] - punkte[0][0]).days
        print(f"    -> Extremwerte {symbol} aus {len(punkte)} Tagen ({spanne} Kalendertage): "
              + ", ".join(f"{w}T {ergebnis[symbol][str(w)][0]:+.2f}/{ergebnis[symbol][str(w)][1]:+.2f} %"
                          for w in EXTREMES_WINDOWS))

    if not ergebnis:
        return False

    store["symbols"] = ergebnis
    store["computed"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store["period"] = EXTREMES_PERIOD
    return True


def reference_point(series: list, now: datetime, window_days: float):
    """Aeltester Kurs, der noch innerhalb des Fensters liegt.

    Gesucht ist der letzte Punkt, der mindestens 'window_days' zurueckliegt -
    an ihm wird die Veraenderung gemessen. Gibt es keinen, reicht die
    Historie noch nicht weit genug zurueck.
    """
    grenze = now - timedelta(days=window_days)
    aeltere = []
    for stamp, value in series:
        try:
            when = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        if when <= grenze:
            aeltere.append((when, value))
    if not aeltere:
        return None
    return aeltere[-1]


def evaluate_change(state: dict, symbol: str, price: float, now: datetime,
                    settings: dict):
    """Prueft, ob sich der Kurs im Fenster stark bewegt hat.

    Wie oft daraus eine Mail wird, entscheidet nicht diese Funktion, sondern
    die Sperre in check_once - sonst meldet eine Bewegung, die tagelang
    bestehen bleibt, bei jedem Lauf erneut.

    Rueckgabe: (meldung_oder_None, zustand_geaendert)
    """
    if not settings.get("enabled", True):
        return None, False

    window = float(settings.get("window_days", 1))
    drop = abs(float(settings.get("drop_percent", 1.0)))
    rise = abs(float(settings.get("rise_percent", 1.0)))

    series = state.get(HISTORY_KEY, {}).get(symbol, [])
    ref = reference_point(series, now, window)
    if not ref:
        return None, False

    ref_when, ref_price = ref
    if not ref_price:
        return None, False

    change = (price - ref_price) / ref_price * 100
    if change <= -drop:
        richtung = "down"
    elif change >= rise:
        richtung = "up"
    else:
        # Zurueck im normalen Bereich: Sperre aufheben, damit die naechste
        # Bewegung wieder sofort meldet
        entry = state.setdefault(symbol, {})
        if entry.get("change_dir"):
            entry["change_dir"] = None
            return None, True
        return None, False

    entry = state.setdefault(symbol, {})
    geaendert = entry.get("change_dir") != richtung
    entry["change_dir"] = richtung

    return {
        "change": change,
        "grenze": -drop if richtung == "down" else rise,
        "ref_price": ref_price,
        "ref_when": ref_when,
        "richtung": richtung,
    }, geaendert


def de(wert: float, nachkomma: int = 2) -> str:
    """Zahl in deutscher Schreibweise: 7.723,55"""
    text = f"{wert:,.{nachkomma}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fussnote(now: datetime, previous: datetime | None) -> str:
    """Knappe Zeitangaben, mit denen sich die Verzoegerung nachrechnen laesst.

    Das Skript weiss nur, wann es die Bewegung gesehen hat - nicht, wann sie
    eingetreten ist. Der Abstand zur vorherigen Pruefung grenzt das ein.
    """
    zeilen = [f"Geprüft   {now:%d.%m.%Y %H:%M:%S} UTC"]
    if previous:
        minuten = (now - previous).total_seconds() / 60
        zeilen.append(f"Davor     {previous:%H:%M:%S} UTC — vor {minuten:.0f} Min.")

    run_id = os.environ.get("GITHUB_RUN_ID")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if run_id and repo:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        zeilen.append(f"Lauf      {server}/{repo}/actions/runs/{run_id}")
    return "\n".join(zeilen)


def pz(wert: float) -> str:
    """Prozentwert mit Vorzeichen, deutsche Schreibweise: +5,51 %"""
    return f"{wert:+.2f}".replace(".", ",") + " %"


# Dativ, weil das Wort hinter "binnen" steht
ZEITRAUM = {1: "24 Stunden", 3: "3 Tagen", 7: "einer Woche"}


def baue_mail(triggered: list, moves: list, fenster: float,
              now: datetime, previous: datetime | None) -> tuple:
    """Setzt Betreff und Text zusammen."""
    zeitraum = ZEITRAUM.get(fenster, f"{fenster:g} Tagen")

    kurz = [f"{m['symbol']} {pz(m['change'])}" for m in moves]
    kurz += [f"{t['symbol']} unter {de(t['threshold'], 0)}" for t in triggered]

    if len(kurz) == 1:
        betreff = kurz[0] + (f" binnen {zeitraum}" if moves else "")
    elif len(kurz) == 2:
        betreff = " · ".join(kurz)
    else:
        betreff = f"{len(kurz)} Meldungen · " + " · ".join(kurz[:2]) + " …"
    betreff = "[stock-watch] " + betreff

    teile = []

    if moves:
        block = [f"STARKE BEWEGUNG binnen {zeitraum}", ""]
        for m in moves:
            block.append(f"  {m['name']} ({m['symbol']})")
            block.append(f"    Kurs         {de(m['price'])} {m['currency']}")
            block.append(f"    Veränderung  {pz(m['change']):<12}"
                         f" Grenze {pz(m['grenze'])}")
            block.append(f"    Vergleich    {de(m['ref_price'])} {m['currency']}"
                         f" am {m['ref_when']:%d.%m. %H:%M} UTC")
            block.append("")
        teile.append("\n".join(block).rstrip())

    if triggered:
        block = ["UNTER SCHWELLE", ""]
        for t in triggered:
            block.append(f"  {t['name']} ({t['symbol']})")
            block.append(f"    Kurs         {de(t['price'])} {t['currency']}")
            block.append(f"    Schwelle     {de(t['threshold'])} {t['currency']}"
                         f"       {pz(t['diff'])}")
            block.append("")
        teile.append("\n".join(block).rstrip())

    teile.append("—" * 46)
    teile.append(fussnote(now, previous))
    teile.append("Automatisch erzeugt · keine Anlageberatung ·"
                 " Kursdaten können verzögert sein.")
    return betreff, "\n\n".join(teile) + "\n"


# --------------------------------------------------------------------------- #
# Hauptlogik
# --------------------------------------------------------------------------- #
def check_once(cfg: dict, dry_run: bool = False) -> int:
    state = load_state()
    now = datetime.now(timezone.utc)

    if run_until_passed(cfg, now):
        print(f"Laufzeit bis {cfg['run_until']} ist abgelaufen - keine Pruefung mehr.")
        return 0

    cooldown = timedelta(hours=float(cfg.get("cooldown_hours", 12)))
    history_gap = float(cfg.get("history_minutes", 30))
    change_cfg = cfg.get("change_alert", {}) or {}
    triggered: list[str] = []
    moves: list[str] = []
    state_changed = False

    # Zeitpunkt der vorherigen Pruefung merken, bevor er ueberschrieben wird.
    # Er wird jedes Mal fortgeschrieben, sonst waere das Fenster in der
    # Alarmmail groesser als die tatsaechliche Luecke.
    raw_previous = state.get(LAST_CHECK_KEY)
    try:
        previous_check = datetime.fromisoformat(raw_previous) if raw_previous else None
    except (TypeError, ValueError):
        previous_check = None
    if not dry_run:
        state[LAST_CHECK_KEY] = now.isoformat(timespec="seconds")
        state_changed = True

    if refresh_extremes(state, [i["symbol"] for i in cfg["watchlist"]]):
        state_changed = True

    for item in cfg["watchlist"]:
        symbol = item["symbol"]
        name = item.get("name", symbol)
        threshold = float(item["threshold"])
        currency = item.get("currency", "")

        try:
            price = get_price(symbol)
        except Exception as exc:
            print(f"[FEHLER] {name} ({symbol}): {exc}")
            continue

        if backfill_history(state, symbol):
            state_changed = True

        if record_price(state, symbol, price, now, history_gap):
            state_changed = True

        bewegung, geaendert = evaluate_change(state, symbol, price, now, change_cfg)
        if geaendert:
            state_changed = True
        if bewegung:
            moves.append({**bewegung, "name": name, "symbol": symbol,
                          "price": price, "currency": currency})

        # Kopie, sonst wuerde die Aenderung unbemerkt bleiben (gleiche Referenz)
        entry = dict(state.get(symbol, {"below": False, "last_alert": None}))
        below = price < threshold
        diff_pct = (price - threshold) / threshold * 100

        print(
            f"{now:%Y-%m-%d %H:%M} UTC | {name:<32} {price:>12,.2f} {currency:<4}"
            f" | Schwelle {threshold:,.2f} ({diff_pct:+.2f} %)"
            f" | {'UNTER SCHWELLE' if below else 'ok'}"
        )

        if below:
            last_alert = entry.get("last_alert")
            last_dt = datetime.fromisoformat(last_alert) if last_alert else None
            fresh_cross = not entry.get("below", False)
            cooldown_over = last_dt is None or (now - last_dt) >= cooldown

            if fresh_cross or cooldown_over:
                triggered.append({"name": name, "symbol": symbol, "price": price,
                                  "currency": currency, "threshold": threshold,
                                  "diff": diff_pct})
                entry["last_alert"] = now.isoformat()
            entry["below"] = True
        else:
            if entry.get("below"):
                print(f"    -> {name} liegt wieder ueber der Schwelle, Alarm zurueckgesetzt.")
            entry["below"] = False

        if state.get(symbol) != entry:
            state[symbol] = entry
            state_changed = True

    # Eine Bewegung ueber ein Wochenfenster besteht tagelang fort. Ohne diese
    # Sperre meldet sie bei jedem Lauf erneut - bei 12 Stunden Ruhezeit waren
    # das bis zu vierzehn Mails pro Woche und Wert. Es geht hoechstens eine
    # Bewegungsmail je Fenster raus, egal wie viele Werte betroffen sind.
    if moves:
        fenster_tage = float(change_cfg.get("window_days", 1))
        sperre = timedelta(days=fenster_tage)
        raw_letzte = state.get(CHANGE_MAIL_KEY)
        try:
            letzte_mail = datetime.fromisoformat(raw_letzte) if raw_letzte else None
        except (TypeError, ValueError):
            letzte_mail = None

        if letzte_mail and (now - letzte_mail) < sperre:
            rest = sperre - (now - letzte_mail)
            print(f"    -> {len(moves)} Bewegungsmeldung(en) unterdrueckt,"
                  f" naechste Mail fruehestens in {rest.total_seconds() / 3600:.1f} Std.")
            moves = []
        elif not dry_run:
            state[CHANGE_MAIL_KEY] = now.isoformat(timespec="seconds")
            state_changed = True

    if triggered or moves:
        subject, body = baue_mail(triggered, moves,
                                  float(change_cfg.get("window_days", 1)),
                                  now, previous_check)

        if dry_run:
            print("\n[DRY-RUN] Mail waere jetzt rausgegangen:\n" + body)
        else:
            send_mail(cfg, subject, body)
            print(f"\n[OK] Alarm-Mail an {cfg['smtp']['to']} verschickt.")

    if state_changed and not dry_run:
        save_state(state)

    return len(triggered)


def run_loop(cfg: dict, dry_run: bool = False) -> None:
    interval = int(cfg.get("interval_minutes", 10)) * 60
    print(f"Dauerlauf gestartet - Abfrage alle {interval // 60} Minuten. Abbrechen mit Strg+C.\n")
    while True:
        try:
            check_once(cfg, dry_run=dry_run)
        except KeyboardInterrupt:
            raise
        except Exception:
            print("[FEHLER] Durchlauf fehlgeschlagen:")
            traceback.print_exc()
        print("-" * 70)
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF/Index Kursalarm per E-Mail")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="einmal pruefen und beenden (Standard)")
    group.add_argument("--loop", action="store_true", help="dauerhaft laufen lassen")
    group.add_argument("--test-mail", action="store_true", help="Testmail verschicken")
    parser.add_argument("--dry-run", action="store_true", help="nichts verschicken, nur anzeigen")
    args = parser.parse_args()

    cfg = load_config()

    if args.test_mail:
        send_mail(
            cfg,
            "[Kursalarm] Testmail",
            "Das ist eine Testmail deines ETF-Watchers. Wenn du das liest, funktioniert der Versand.\n",
        )
        print(f"[OK] Testmail an {cfg['smtp']['to']} verschickt.")
        return 0

    if args.loop:
        try:
            run_loop(cfg, dry_run=args.dry_run)
        except KeyboardInterrupt:
            print("\nBeendet.")
        return 0

    check_once(cfg, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
