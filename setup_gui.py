#!/usr/bin/env python3
"""
Einrichtungs-Oberflaeche fuer den ETF-Watcher.

Start:  python setup_gui.py

Hier traegst du E-Mail-Zugang, Empfaenger und Schwellwerte ein. Die Eingaben
landen in config.json. Von dort liest tracker.py sie beim Start.
Tkinter ist bei den meisten Python-Installationen dabei (Linux ggf.
`sudo apt install python3-tk`).
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
TRACKER = BASE_DIR / "tracker.py"

PRESETS = [
    ("S&P 500", "^GSPC", "USD"),
    ("Vanguard FTSE All-World (VWCE, IE00BK5BQT80)", "VWCE.DE", "EUR"),
    ("Vanguard FTSE All-World (VWRL, ausschuettend)", "VWRL.AS", "EUR"),
    ("MSCI World (iShares Core, IWDA)", "IWDA.AS", "EUR"),
    ("NASDAQ 100", "^NDX", "USD"),
    ("DAX", "^GDAXI", "EUR"),
]

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
    "cooldown_hours": 12,
    "watchlist": [
        {"name": "S&P 500", "symbol": "^GSPC", "threshold": 5000.0, "currency": "USD"},
        {
            "name": "Vanguard FTSE All-World (VWCE)",
            "symbol": "VWCE.DE",
            "threshold": 100.0,
            "currency": "EUR",
        },
    ],
}


class WatchRow:
    """Eine Zeile der Beobachtungsliste."""

    def __init__(self, parent: ttk.Frame, app: "SetupApp", data: dict):
        self.app = app
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="x", pady=2)

        self.name = tk.StringVar(value=data.get("name", ""))
        self.symbol = tk.StringVar(value=data.get("symbol", ""))
        self.threshold = tk.StringVar(value=str(data.get("threshold", "")))
        self.currency = tk.StringVar(value=data.get("currency", "EUR"))

        ttk.Entry(self.frame, textvariable=self.name, width=30).pack(side="left", padx=(0, 4))
        ttk.Entry(self.frame, textvariable=self.symbol, width=11).pack(side="left", padx=4)
        ttk.Entry(self.frame, textvariable=self.threshold, width=11).pack(side="left", padx=4)
        ttk.Combobox(
            self.frame, textvariable=self.currency, width=5,
            values=["EUR", "USD", "GBP", "CHF"], state="readonly",
        ).pack(side="left", padx=4)
        ttk.Button(self.frame, text="Kurs", width=6, command=self.fetch).pack(side="left", padx=4)
        ttk.Button(self.frame, text="X", width=3, command=self.remove).pack(side="left")

    def fetch(self):
        symbol = self.symbol.get().strip()
        if not symbol:
            return
        self.app.log(f"Frage Kurs fuer {symbol} ab ...")
        threading.Thread(target=self._fetch_worker, args=(symbol,), daemon=True).start()

    def _fetch_worker(self, symbol: str):
        try:
            sys.path.insert(0, str(BASE_DIR))
            from tracker import get_price

            price = get_price(symbol)
            self.app.log(f"{symbol}: aktuell {price:,.2f}")
        except Exception as exc:
            self.app.log(f"Fehler bei {symbol}: {exc}")

    def remove(self):
        self.frame.destroy()
        if self in self.app.rows:
            self.app.rows.remove(self)

    def to_dict(self) -> dict:
        return {
            "name": self.name.get().strip() or self.symbol.get().strip(),
            "symbol": self.symbol.get().strip(),
            "threshold": float(str(self.threshold.get()).replace(",", ".").strip()),
            "currency": self.currency.get(),
        }


class SetupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ETF-Watcher - Einrichtung")
        self.geometry("760x700")
        self.minsize(700, 620)

        self.rows: list[WatchRow] = []
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.proc: subprocess.Popen | None = None

        cfg = self.load_config()
        self.build_ui(cfg)
        self.after(150, self.drain_log)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------------- config
    def load_config(self) -> dict:
        if CONFIG_PATH.exists():
            try:
                with CONFIG_PATH.open(encoding="utf-8") as fh:
                    saved = json.load(fh)
                cfg = json.loads(json.dumps(DEFAULT_CONFIG))
                cfg.update({k: v for k, v in saved.items() if k != "smtp"})
                cfg["smtp"].update(saved.get("smtp", {}))
                return cfg
            except Exception:
                pass
        return json.loads(json.dumps(DEFAULT_CONFIG))

    # -------------------------------------------------------------------- UI
    def build_ui(self, cfg: dict):
        pad = {"padx": 12, "pady": 6}

        header = ttk.Label(
            self,
            text="Kursalarm einrichten",
            font=("TkDefaultFont", 14, "bold"),
        )
        header.pack(anchor="w", **pad)

        # --- E-Mail ---------------------------------------------------------
        mail_box = ttk.LabelFrame(self, text="E-Mail-Versand (SMTP)")
        mail_box.pack(fill="x", **pad)

        smtp = cfg["smtp"]
        self.host = tk.StringVar(value=smtp.get("host", "smtp.gmail.com"))
        self.port = tk.StringVar(value=str(smtp.get("port", 465)))
        self.user = tk.StringVar(value=smtp.get("user", ""))
        self.password = tk.StringVar(value=smtp.get("password", ""))
        self.to = tk.StringVar(value=smtp.get("to", ""))

        grid = ttk.Frame(mail_box)
        grid.pack(fill="x", padx=10, pady=8)
        grid.columnconfigure(1, weight=1)

        def row(r, label, var, show=None, width=38):
            ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w", pady=3)
            entry = ttk.Entry(grid, textvariable=var, width=width, show=show)
            entry.grid(row=r, column=1, sticky="ew", padx=(8, 0), pady=3)
            return entry

        provider_frame = ttk.Frame(grid)
        provider_frame.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(provider_frame, text="Anbieter:").pack(side="left")
        for label, host, port in (
            ("Gmail", "smtp.gmail.com", "465"),
            ("GMX", "mail.gmx.net", "465"),
            ("Web.de", "smtp.web.de", "587"),
            ("Outlook", "smtp-mail.outlook.com", "587"),
        ):
            ttk.Button(
                provider_frame, text=label, width=8,
                command=lambda h=host, p=port: (self.host.set(h), self.port.set(p)),
            ).pack(side="left", padx=3)

        row(1, "SMTP-Server", self.host)
        row(2, "Port (465 = SSL, 587 = TLS)", self.port, width=10)
        row(3, "Absender / Login", self.user)
        row(4, "Passwort (App-Passwort!)", self.password, show="*")
        row(5, "Mail geht an", self.to)

        hint = ttk.Label(
            mail_box,
            text=("Bei Gmail/GMX brauchst du ein App-Passwort, nicht dein normales "
                  "Login-Passwort.\nGmail: Konto -> Sicherheit -> 2FA aktivieren -> App-Passwoerter."),
            foreground="#555",
            justify="left",
        )
        hint.pack(anchor="w", padx=10, pady=(0, 8))

        # --- Watchlist ------------------------------------------------------
        watch_box = ttk.LabelFrame(self, text="Was soll ueberwacht werden?")
        watch_box.pack(fill="both", expand=True, **pad)

        head = ttk.Frame(watch_box)
        head.pack(fill="x", padx=10, pady=(8, 0))
        for text, width in (("Name", 30), ("Symbol", 11), ("Alarm unter", 11), ("Waehrung", 7)):
            ttk.Label(head, text=text, font=("TkDefaultFont", 9, "bold"), width=width).pack(
                side="left", padx=4
            )

        self.rows_frame = ttk.Frame(watch_box)
        self.rows_frame.pack(fill="both", expand=True, padx=10, pady=4)

        for item in cfg["watchlist"]:
            self.rows.append(WatchRow(self.rows_frame, self, item))

        add_frame = ttk.Frame(watch_box)
        add_frame.pack(fill="x", padx=10, pady=(0, 8))
        self.preset = tk.StringVar(value=PRESETS[0][0])
        ttk.Combobox(
            add_frame, textvariable=self.preset, values=[p[0] for p in PRESETS],
            state="readonly", width=42,
        ).pack(side="left")
        ttk.Button(add_frame, text="+ hinzufuegen", command=self.add_preset).pack(side="left", padx=6)

        # --- Intervall ------------------------------------------------------
        opt = ttk.Frame(self)
        opt.pack(fill="x", **pad)
        self.interval = tk.StringVar(value=str(cfg.get("interval_minutes", 10)))
        self.cooldown = tk.StringVar(value=str(cfg.get("cooldown_hours", 12)))
        ttk.Label(opt, text="Abfrage alle").pack(side="left")
        ttk.Entry(opt, textvariable=self.interval, width=5).pack(side="left", padx=4)
        ttk.Label(opt, text="Minuten     Mail-Pause nach Alarm:").pack(side="left")
        ttk.Entry(opt, textvariable=self.cooldown, width=5).pack(side="left", padx=4)
        ttk.Label(opt, text="Stunden").pack(side="left")

        # --- Buttons --------------------------------------------------------
        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="Speichern", command=self.save).pack(side="left")
        ttk.Button(btns, text="Testmail senden", command=lambda: self.run_tracker("--test-mail")).pack(
            side="left", padx=6
        )
        ttk.Button(btns, text="Jetzt einmal pruefen", command=lambda: self.run_tracker("--once")).pack(
            side="left", padx=6
        )
        self.loop_btn = ttk.Button(btns, text="Dauerlauf starten", command=self.toggle_loop)
        self.loop_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="GitHub-Secrets", command=self.show_secrets).pack(side="right")

        # --- Log ------------------------------------------------------------
        self.log_widget = tk.Text(self, height=9, wrap="word", bg="#111", fg="#ddd",
                                  insertbackground="#ddd", font=("TkFixedFont", 9))
        self.log_widget.pack(fill="both", expand=False, padx=12, pady=(0, 12))
        self.log("Bereit. Zuerst Daten eintragen, dann 'Speichern', dann 'Testmail senden'.")

    # ---------------------------------------------------------------- Actions
    def add_preset(self):
        choice = self.preset.get()
        for name, symbol, currency in PRESETS:
            if name == choice:
                self.rows.append(
                    WatchRow(self.rows_frame, self,
                             {"name": name, "symbol": symbol, "threshold": 0, "currency": currency})
                )
                return

    def collect(self) -> dict:
        watchlist = []
        for r in self.rows:
            try:
                item = r.to_dict()
            except ValueError:
                raise ValueError(f"Schwellwert bei '{r.name.get()}' ist keine Zahl.")
            if item["symbol"]:
                watchlist.append(item)
        if not watchlist:
            raise ValueError("Die Beobachtungsliste ist leer.")

        return {
            "smtp": {
                "host": self.host.get().strip(),
                "port": int(self.port.get().strip() or 465),
                "user": self.user.get().strip(),
                "password": self.password.get(),
                "from": self.user.get().strip(),
                "to": self.to.get().strip(),
            },
            "interval_minutes": int(self.interval.get().strip() or 10),
            "cooldown_hours": float(str(self.cooldown.get()).replace(",", ".") or 12),
            "watchlist": watchlist,
        }

    def save(self) -> bool:
        try:
            cfg = self.collect()
        except ValueError as exc:
            messagebox.showerror("Eingabe pruefen", str(exc))
            return False
        with CONFIG_PATH.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        self.log(f"Gespeichert: {CONFIG_PATH}")
        return True

    def run_tracker(self, *flags: str):
        if not self.save():
            return
        self.log(f"Starte: tracker.py {' '.join(flags)}")
        threading.Thread(target=self._run_worker, args=flags, daemon=True).start()

    def _run_worker(self, *flags: str):
        try:
            proc = subprocess.Popen(
                [sys.executable, str(TRACKER), *flags],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=str(BASE_DIR), bufsize=1,
            )
            self.proc = proc
            for line in proc.stdout:
                self.log_queue.put(line.rstrip())
            proc.wait()
            self.log_queue.put(f"[fertig, Exit-Code {proc.returncode}]")
        except Exception as exc:
            self.log_queue.put(f"[Fehler] {exc}")
        finally:
            self.proc = None
            self.log_queue.put("__LOOP_DONE__")

    def toggle_loop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.log("Dauerlauf gestoppt.")
            self.loop_btn.config(text="Dauerlauf starten")
            return
        self.run_tracker("--loop")
        self.loop_btn.config(text="Dauerlauf stoppen")

    def show_secrets(self):
        try:
            cfg = self.collect()
        except ValueError as exc:
            messagebox.showerror("Eingabe pruefen", str(exc))
            return

        win = tk.Toplevel(self)
        win.title("Werte fuer GitHub Secrets")
        win.geometry("620x420")
        text = tk.Text(win, wrap="word", font=("TkFixedFont", 10))
        text.pack(fill="both", expand=True, padx=10, pady=10)
        smtp = cfg["smtp"]
        text.insert(
            "1.0",
            "Repo -> Settings -> Secrets and variables -> Actions -> New repository secret\n"
            "Lege diese vier (bzw. sechs) Secrets an:\n\n"
            f"SMTP_HOST = {smtp['host']}\n"
            f"SMTP_PORT = {smtp['port']}\n"
            f"SMTP_USER = {smtp['user']}\n"
            f"SMTP_PASS = {smtp['password']}\n"
            f"MAIL_TO   = {smtp['to']}\n\n"
            "Wichtig: In der config.json, die du ins Repo committest, muss das Feld\n"
            "'password' LEER sein. Passwoerter gehoeren nur in die Secrets.\n\n"
            "Die Schwellwerte kommen aus der config.json im Repo - die darfst du\n"
            "committen, sie enthaelt dann keine Zugangsdaten mehr.\n",
        )
        text.config(state="disabled")

    # -------------------------------------------------------------------- Log
    def log(self, msg: str):
        self.log_queue.put(msg)

    def drain_log(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if msg == "__LOOP_DONE__":
                self.loop_btn.config(text="Dauerlauf starten")
                continue
            self.log_widget.insert("end", msg + "\n")
            self.log_widget.see("end")
        self.after(150, self.drain_log)

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.destroy()


if __name__ == "__main__":
    SetupApp().mainloop()
