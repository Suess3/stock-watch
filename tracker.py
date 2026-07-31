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

    if series:
        try:
            last = datetime.fromisoformat(series[-1][0])
        except (ValueError, IndexError, TypeError):
            last = None
        if last and (now - last) < timedelta(minutes=min_gap_minutes):
            return False

    series.append([now.isoformat(timespec="minutes"), round(price, 4)])
    del series[:-HISTORY_MAX_POINTS]
    return True


BACKFILL_TRIES_KEY = "__backfill_tries"
BACKFILL_MAX_TRIES = 5


def backfill_history(state: dict, symbol: str, min_points: int = 24) -> bool:
    """Traegt einmalig die letzte Woche stuendlich nach.

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
        hist = yf.Ticker(symbol).history(period="7d", interval="60m")
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
        print(f"    -> Keine Stundenwerte fuer {symbol} erhalten.")
        return True

    # Eigene Messwerte haben Vorrang vor den nachgetragenen.
    merged = {p[0]: p for p in points}
    merged.update({p[0]: p for p in series})
    history[symbol] = [merged[k] for k in sorted(merged)][-HISTORY_MAX_POINTS:]
    tries.pop(symbol, None)
    print(f"    -> {len(points)} Stundenwerte der letzten Woche fuer {symbol} nachgetragen.")
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
    triggered: list[str] = []
    state_changed = False

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
                triggered.append(
                    f"- {name} ({symbol})\n"
                    f"    aktueller Kurs : {price:,.2f} {currency}\n"
                    f"    Schwellwert    : {threshold:,.2f} {currency}\n"
                    f"    Abweichung     : {diff_pct:+.2f} %"
                )
                entry["last_alert"] = now.isoformat()
            entry["below"] = True
        else:
            if entry.get("below"):
                print(f"    -> {name} liegt wieder ueber der Schwelle, Alarm zurueckgesetzt.")
            entry["below"] = False

        if state.get(symbol) != entry:
            state[symbol] = entry
            state_changed = True

    if triggered:
        subject = f"[Kursalarm] {len(triggered)} Wert(e) unter deiner Schwelle"
        body = (
            "Hallo,\n\n"
            "folgende Werte liegen unter dem von dir gesetzten Schwellwert:\n\n"
            + "\n\n".join(triggered)
            + f"\n\nZeitpunkt: {now:%d.%m.%Y %H:%M} UTC\n\n"
            "Das ist eine automatische Nachricht deines ETF-Watchers.\n"
            "Keine Anlageberatung - Kursdaten koennen verzoegert oder fehlerhaft sein.\n"
        )
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
