# ETF-Watcher

Überwacht Kurse (S&P 500, Vanguard FTSE All-World, …) und schickt dir eine E-Mail,
sobald ein Kurs unter einen von dir gesetzten Wert fällt.

```
setup_gui.py   ->  Oberfläche: E-Mail + Schwellwerte eintragen, schreibt config.json
tracker.py     ->  holt die Kurse, vergleicht, verschickt die Mail
config.json    ->  deine Einstellungen (wird von der GUI erzeugt)
state.json     ->  merkt sich, ob schon ein Alarm raus ist (wird automatisch angelegt)
.github/workflows/watcher.yml  ->  lässt das Ganze alle 10 Min. bei GitHub laufen
```

## 1. Lokal einrichten

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python setup_gui.py
```

In der Oberfläche:

1. Anbieter anklicken (Gmail/GMX/Web.de/Outlook) → Server und Port werden gesetzt.
2. Absenderadresse, **App-Passwort** und Empfängeradresse eintragen.
3. Schwellwerte setzen. Der Button „Kurs“ zeigt dir den aktuellen Stand, damit du
   weißt, wo du ansetzen musst.
4. **Speichern** → **Testmail senden** → **Jetzt einmal prüfen**.

### App-Passwort

Dein normales Mail-Passwort funktioniert bei den meisten Anbietern nicht mehr.

* **Gmail:** Google-Konto → Sicherheit → 2-Faktor aktivieren → App-Passwörter → neues erzeugen (16 Zeichen).
* **GMX / Web.de:** Einstellungen → POP3/IMAP → externe Programme zulassen.

## 2. Symbole

| Wert | Symbol |
|---|---|
| S&P 500 (Index) | `^GSPC` |
| Vanguard FTSE All-World, IE00BK5BQT80 (VWCE, thesaurierend) | `VWCE.DE` |
| Vanguard FTSE All-World, ausschüttend (VWRL) | `VWRL.AS` |
| NASDAQ 100 | `^NDX` |
| DAX | `^GDAXI` |

Die Kurse kommen über `yfinance` von Yahoo Finance. Falls ein Symbol nicht geht:
auf finance.yahoo.com nach der ISIN suchen, das dortige Kürzel eintragen.

## 3. Dauerlauf auf dem eigenen Rechner

```bash
python tracker.py --loop
```

Läuft, solange das Fenster offen ist. Für „immer an“ → Abschnitt 4.

## 4. Bei GitHub laufen lassen (kostenlos)

1. Repo anlegen (**privat**, sonst steht deine Mailadresse in der `config.json` öffentlich)
   und alle Dateien hochladen.
2. In der `config.json`, die du hochlädst, muss `"password": ""` leer sein.
3. Repo → **Settings → Secrets and variables → Actions → New repository secret**.
   Diese fünf anlegen (die GUI zeigt dir die Werte unter „GitHub-Secrets“):

   | Secret | Beispiel |
   |---|---|
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `465` |
   | `SMTP_USER` | `deine.adresse@gmail.com` |
   | `SMTP_PASS` | dein App-Passwort |
   | `MAIL_TO` | Empfängeradresse |

4. Tab **Actions** öffnen, Workflows aktivieren, „ETF Watcher“ → **Run workflow**
   einmal manuell starten und prüfen, ob es durchläuft.

Ab dann läuft es automatisch alle 10 Minuten.

### Was du bei GitHub Actions wissen solltest

* Der Cron ist **nicht pünktlich**. GitHub verschiebt geplante Läufe bei Last,
  in der Praxis sind es oft 10–20 Minuten. Für einen Kursalarm reicht das, für
  sekundengenaues Trading nicht.
* Bei **öffentlichen** Repos sind Actions-Minuten gratis, bei **privaten** hast du
  ein Freikontingent (aktuell 2.000 Min./Monat im Free-Plan). Ein Lauf dauert
  ~1 Minute, alle 10 Minuten sind ~4.300 Läufe/Monat — das sprengt das Kontingent
  eines privaten Repos deutlich. Zwei sinnvolle Wege:
  * öffentliches Repo, aber **ohne** Mailadresse in der `config.json` (dann auch
    `MAIL_TO` als Secret nutzen und die Adresse aus der Datei rauslassen), oder
  * Intervall auf 30–60 Minuten hochsetzen (`cron: "*/30 * * * *"`).
* Geplante Workflows werden nach **60 Tagen ohne Commit** im Repo automatisch
  deaktiviert. Da der Workflow bei Statusänderungen selbst committet, passiert
  das meist nicht — schau trotzdem gelegentlich rein.

### Alternativen zum Hosting

Wenn dir das zu fummelig ist: ein Raspberry Pi zuhause mit `cron`, ein kleiner
VPS (Hetzner ab ~4 €/Monat), PythonAnywhere (Free-Tier mit Scheduled Task) oder
Fly.io / Render. Überall reicht `python tracker.py --once` per Cron bzw.
`python tracker.py --loop` als Dienst.

## 5. Warum kommt nicht alle 10 Minuten eine Mail?

Sobald ein Alarm raus ist, wird er in `state.json` vermerkt. Weitere Mails gibt es
erst wieder, wenn

* die Pause (`cooldown_hours`, Standard 12 h) abgelaufen ist, **oder**
* der Kurs zwischendurch wieder über die Schwelle gestiegen ist.

Zum Zurücksetzen einfach `state.json` löschen.

## 6. Nützliche Befehle

```bash
python tracker.py --once       # einmal prüfen
python tracker.py --dry-run    # prüfen, Mail nur anzeigen statt senden
python tracker.py --test-mail  # Mailversand testen
python tracker.py --loop       # Dauerlauf
```

---

Keine Anlageberatung. Kursdaten von Yahoo Finance sind teils verzögert und ohne
Gewähr — verlass dich bei echten Entscheidungen nicht blind darauf.
