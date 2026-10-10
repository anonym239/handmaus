# HandMaus

Steuert den Windows-Mauszeiger berührungslos mit deiner Hand über die Webcam – in jedem Programm,
auf jeder Website. Läuft ohne Fenster im Hintergrund; du siehst dich selbst nie.

Das Kamerabild wird nur im Arbeitsspeicher ausgewertet (MediaPipe Hand-Tracking) und weder angezeigt
noch gespeichert noch verschickt. In der Pause ist die Kamera ausgeschaltet.

## Installieren

1. Unter [Releases](../../releases/latest) die Datei `HandMaus.exe` herunterladen.
2. Doppelklick. Windows zeigt beim ersten Mal evtl. „Der Computer wurde durch Windows geschützt“ –
   das liegt daran, dass die Datei nicht signiert ist: „Weitere Informationen → Trotzdem ausführen“.
3. Nach ein paar Sekunden erscheint ein Hand-Symbol im Infobereich (unten rechts, evtl. hinter dem Pfeil ^).

## Gesten

| Geste | Aktion |
| --- | --- |
| Hand bewegen | Mauszeiger bewegen |
| Daumen + Zeigefinger kurz zusammen | Linksklick |
| Daumen + Zeigefinger halten und bewegen | Ziehen / Markieren |
| Daumen + Mittelfinger zusammen | Rechtsklick |
| Zeige- und Mittelfinger gestreckt (✌), Hand hoch/runter | Scrollen |
| **Strg + Alt + H** (Tastatur) | Pause / Weiter |

## Hand-Symbol im Infobereich

- **Grün** – Hand erkannt · **Blau** – sucht Hand · **Grau** – Pause · **Orange** – keine Kamera
- **Linksklick** auf das Symbol: Pause / Weiter
- **Rechtsklick**: Einstellungen
  - **Hand** – Automatisch, rechte oder linke Hand. Es wird immer nur eine Hand verfolgt.
  - **Steuerzone** – wo im Kamerabild du die Hand bewegst (z. B. „Unten“ oder „Rechts unten“,
    damit die Hand nicht vor dem Bildschirm ist).
  - **Bewegung** – wie weit du die Hand für den ganzen Bildschirm bewegen musst.
  - **Kamera** – falls mehrere Kameras angeschlossen sind.
  - **Mit Windows starten** – HandMaus automatisch beim Anmelden starten.
  - **Beenden**

## Selbst bauen

```bash
pip install -r requirements.txt
```

```bash
build.bat
```

Die fertige Datei liegt dann in `dist\HandMaus.exe`. Ein Push eines Tags `v*` baut die .exe außerdem
automatisch per GitHub Actions und hängt sie an ein Release.
