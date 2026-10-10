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
4. Beim ersten Start öffnet sich eine ausführliche Anleitung im Browser. Später findest du sie über
   Rechtsklick auf das Hand-Symbol → „Anleitung öffnen“.

## Gesten

| Geste | Aktion |
| --- | --- |
| Hand bewegen | Mauszeiger bewegen |
| Daumen + Zeigefinger **oder** Daumen + Mittelfinger kurz zusammen | Linksklick |
| Zusammenhalten und Hand bewegen | Ziehen / Markieren |
| Daumen + Ringfinger halten, Hand hoch/runter | Scrollen |
| Daumen + kleiner Finger zusammen | Rechtsklick |
| Hand 3× schnell zu- und aufmachen (Faust) | Steuerung stoppen / fortsetzen |
| **Strg + Alt + H** (Tastatur) | Pause / Weiter (Kamera aus) |

Beim Stoppen per Geste ertönt ein kurzer Ton, der Ring leuchtet rot auf und das Hand-Symbol wird rot.
Die Kamera läuft dabei weiter, damit dieselbe Geste die Steuerung wieder einschalten kann – die Maus
wird aber nicht mehr bewegt und es wird nicht geklickt. Eine Faust allein löst nie einen Klick aus.

Ein farbiger Ring um den Mauszeiger zeigt, wo der Zeiger ist und was die Hand gerade tut:
Blau = Zeiger, Grün = Linksklick, Orange = Rechtsklick, Lila = Scrollen.
Der Zeiger wird rund 200-mal pro Sekunde auf einer vorausberechneten, glatten Bahn geführt – auch
schnelle Handbewegungen springen deshalb nicht in Stufen. Beim Klicken bleibt der Zeiger stehen, damit der Klick nicht verrutscht; erst eine deutliche
Handbewegung beginnt das Ziehen. Bewegst du die echte Maus, lässt HandMaus den Zeiger kurz los.

## Hand-Symbol im Infobereich

- **Grün** – Hand erkannt · **Blau** – sucht Hand · **Rot** – per Geste gestoppt · **Grau** – Pause ·
  **Orange** – keine Kamera
- **Linksklick** auf das Symbol: Pause / Weiter
- **Rechtsklick**: Einstellungen
  - **Hand** – Automatisch, rechte oder linke Hand. Es wird immer nur eine Hand verfolgt.
  - **Steuerzone** – wo im Kamerabild du die Hand bewegst (z. B. „Unten“ oder „Rechts unten“,
    damit die Hand nicht vor dem Bildschirm ist).
  - **Bewegung** – wie weit du die Hand für den ganzen Bildschirm bewegen musst.
  - **Kamera** – falls mehrere Kameras angeschlossen sind.
  - **Ring um den Mauszeiger** – den farbigen Ring ein- oder ausschalten.
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
