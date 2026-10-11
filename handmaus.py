"""HandMaus – steuert den Mauszeiger berührungslos per Webcam.

Läuft ohne Fenster im Infobereich (Tray). Das Kamerabild wird nur im Arbeitsspeicher
ausgewertet, nie angezeigt, gespeichert oder verschickt.
"""
import collections
import ctypes
from ctypes import wintypes
import json
import math
import os
import shutil
import sys
import threading
import time

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions, vision
from PIL import Image, ImageDraw
import pystray

APP = 'HandMaus'
BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
MODEL_FILE = os.path.join(BASE_DIR, 'hand_landmarker.task')
CONFIG_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), APP)
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')

# Steuerzone im gespiegelten Kamerabild (0..1): Mittelpunkt je Voreinstellung, Kantenlänge je Bewegungsgröße
ZONES = {'mitte': (.5, .5), 'unten': (.5, .75), 'links': (.25, .5), 'rechts': (.75, .5),
         'linksunten': (.25, .75), 'rechtsunten': (.75, .75)}
ZONE_NAMES = {'mitte': 'Mitte', 'unten': 'Unten', 'links': 'Links', 'rechts': 'Rechts',
              'linksunten': 'Links unten', 'rechtsunten': 'Rechts unten', 'eigen': 'Eigener Bereich (kalibriert)'}
SIZES = {'gross': .6, 'normal': .42, 'klein': .28}
SIZE_NAMES = {'gross': 'Groß (weite Handbewegung)', 'normal': 'Normal', 'klein': 'Klein (kurze Handbewegung)'}
HAND_NAMES = {'auto': 'Automatisch', 'rechts': 'Rechte Hand', 'links': 'Linke Hand'}
MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
# Tasten, mit denen sich die Kalibrierung öffnen lässt: Name, Modifikatoren, virtueller Tastencode
CALIB_KEYS = {'strg_alt_k': ('Strg+Alt+K', MOD_CTRL | MOD_ALT, 0x4B), 'strg_alt_c': ('Strg+Alt+C', MOD_CTRL | MOD_ALT, 0x43),
              'strg_umschalt_k': ('Strg+Umschalt+K', MOD_CTRL | MOD_SHIFT, 0x4B),
              'f7': ('F7', 0, 0x76), 'f8': ('F8', 0, 0x77), 'f9': ('F9', 0, 0x78), 'f10': ('F10', 0, 0x79),
              'aus': ('Keine Taste', 0, 0)}
MIN_ZONE = .06  # kleinster erlaubter Kalibrier-Bereich (Anteil des Kamerabilds)

config = {'hand': 'auto', 'zone': 'mitte', 'size': 'normal', 'camera': 0, 'ring': True, 'guide_seen': False,
          'custom': None, 'calib_key': 'strg_alt_k'}


def load_config():
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            config.update({k: v for k, v in json.load(f).items() if k in config})
    except (OSError, ValueError):
        pass


def save_config():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f)
    except OSError:
        pass


# ---------- Windows-Maus ----------
user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
MOVE_ABS, LEFTDOWN, LEFTUP, RIGHTDOWN, RIGHTUP, WHEEL = 0x8001, 0x0002, 0x0004, 0x0008, 0x0010, 0x0800


def mouse_event(flags, data=0):
    user32.mouse_event(flags, 0, 0, data, 0)


def screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def move_cursor(x, y):
    """Bewegt den Zeiger als echtes Maus-Ereignis – so blendet Windows ihn auch wieder ein."""
    w, h = screen_size()
    user32.mouse_event(MOVE_ABS, x * 65536 // w + 1, y * 65536 // h + 1, 0, 0)


class OneEuro:
    """Glättet Zittern bei ruhiger Hand, bleibt bei schnellen Bewegungen direkt."""

    def __init__(self, min_cutoff=1.6, beta=25.0, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x = self.dx = self.t = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1 / (2 * math.pi * cutoff)
        return 1 / (1 + tau / dt)

    def reset(self):
        self.x = None

    def __call__(self, x, t):
        if self.x is None:
            self.x, self.dx, self.t = x, 0.0, t
            return x
        dt = max(t - self.t, 1e-3)
        dx = (x - self.x) / dt
        self.dx += self._alpha(self.d_cutoff, dt) * (dx - self.dx)
        self.x += self._alpha(self.min_cutoff + self.beta * abs(self.dx), dt) * (x - self.x)
        self.t = t
        return self.x


def dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def custom_rect():
    """Kalibrierter Bereich (x, y, Breite, Höhe im Kamerabild) oder None, wenn keiner gültig gespeichert ist."""
    try:
        x, y, w, h = (float(v) for v in config['custom'])
    except (TypeError, ValueError):
        return None
    if w < MIN_ZONE or h < MIN_ZONE or x < 0 or y < 0 or x + w > 1.001 or y + h > 1.001:
        return None
    return x, y, w, h


def zone_rect(zone=None):
    """Bereich im Kamerabild, der auf den ganzen Bildschirm abgebildet wird: (x, y, Breite, Höhe)."""
    zone = zone or config['zone']
    if zone == 'eigen' and custom_rect():
        return custom_rect()
    s = SIZES.get(config['size'], SIZES['normal'])
    cx, cy = ZONES.get(zone, ZONES['mitte'])
    fit = lambda v: clamp(v, s / 2 + .02, 1 - s / 2 - .02)
    return fit(cx) - s / 2, fit(cy) - s / 2, s, s


PINCH_ON, PINCH_OFF = .30, .48  # Abstand Daumen–Fingerspitze relativ zur Handgröße
DRAG_START = .035               # so weit muss sich die Hand beim Halten bewegen, bevor gezogen wird
GRACE = .25                     # so lange darf die Hand kurz verschwinden, ohne dass losgelassen wird
PUMP_WINDOW = 2.0               # in dieser Zeit muss die Hand dreimal zur Faust geschlossen werden


def beep(stopped):
    import winsound
    tones = ((660, 90), (440, 140)) if stopped else ((440, 90), (660, 140))
    threading.Thread(target=lambda: [winsound.Beep(f, ms) for f, ms in tones], daemon=True).start()


class Mover(threading.Thread):
    """Führt den Zeiger rund 200-mal pro Sekunde auf einer glatten Bahn.

    Die Kamera liefert nur etwa 30 Positionen pro Sekunde. Würde der Zeiger nur dann gesetzt, spränge er
    bei schnellen Bewegungen in sichtbaren Stufen. Stattdessen wird aus den letzten Positionen die
    Geschwindigkeit der Hand bestimmt, die Bahn bis zum nächsten Kamerabild vorausberechnet und der
    Zeiger in vielen kleinen Schritten daran entlanggeführt.
    """
    TAU = .018       # Sekunden: wie weich der Zeiger der berechneten Bahn folgt
    MAX_AHEAD = .05  # länger als das wird nie vorausgerechnet (z. B. wenn ein Kamerabild ausfällt)

    def __init__(self, threaded=True):
        super().__init__(daemon=True)
        self.threaded = threaded  # False im Test: jede Position sofort und exakt setzen
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.active = False
        self.target = self.cur = self.sent = self.last_seen = None
        self.vel = (0.0, 0.0)
        self.t_aim = self.t_tick = self.yield_until = 0.0
        self.frame_dt = .033

    def aim(self, pos, now, snap=False):
        """Neue Zielposition (0..1) aus einem Kamerabild; snap setzt den Zeiger sofort exakt (für Klicks)."""
        w, h = screen_size()
        tx, ty = pos[0] * (w - 1), pos[1] * (h - 1)
        with self.lock:
            dt = now - self.t_aim
            if snap or not self.active or self.target is None or not 0 < dt < .2:
                self.vel = (0.0, 0.0)
            else:
                self.vel, self.frame_dt = ((tx - self.target[0]) / dt, (ty - self.target[1]) / dt), dt
            self.target, self.t_aim, self.active = (tx, ty), now, True
            if snap or not self.threaded:
                self._tick(now, direct=True)

    def idle(self):
        with self.lock:
            self.active, self.target, self.sent = False, None, None

    def yielding(self):
        return time.perf_counter() < self.yield_until

    def _tick(self, now, direct=False):
        if not self.active or self.target is None:
            return
        here = cursor_pos()
        if now < self.yield_until:  # solange die echte Maus weiterbewegt wird, weiter warten
            if here != self.last_seen:
                self.yield_until = now + 1.0
            self.last_seen = here
            return
        # Steht der Zeiger nicht dort, wo wir ihn hingesetzt haben, hat jemand die echte Maus bewegt
        if self.sent and abs(here[0] - self.sent[0]) + abs(here[1] - self.sent[1]) > 8:
            self.yield_until, self.sent, self.last_seen = now + 1.0, None, here
            return
        if direct:
            self.cur = self.target
        else:
            if self.sent is None or self.cur is None:  # neu übernommen: von der aktuellen Stelle losgleiten
                self.cur = (float(here[0]), float(here[1]))
            ahead = min(max(now - self.t_aim, 0.0), self.frame_dt, self.MAX_AHEAD)
            goal = (self.target[0] + self.vel[0] * ahead, self.target[1] + self.vel[1] * ahead)
            k = 1 - math.exp(-max(now - self.t_tick, 0.0) / self.TAU)
            self.cur = (self.cur[0] + (goal[0] - self.cur[0]) * k, self.cur[1] + (goal[1] - self.cur[1]) * k)
        self.t_tick = now
        w, h = screen_size()
        px = (clamp(round(self.cur[0]), 0, w - 1), clamp(round(self.cur[1]), 0, h - 1))
        if px != self.sent:
            move_cursor(*px)
            self.sent = px

    def run(self):
        winmm = ctypes.WinDLL('winmm')
        winmm.timeBeginPeriod(1)  # sonst schläft Windows in 15-ms-Schritten
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    self._tick(time.perf_counter())
                time.sleep(.005)
        finally:
            winmm.timeEndPeriod(1)


class Gestures:
    """Wählt eine Hand aus, bleibt bei ihr und leitet daraus Zeiger, Klicks und Scrollen ab."""

    def __init__(self, mover=None):
        self.mover = mover
        self.act = mover is not None  # ohne Mover (Selbsttest) nichts an die Maus schicken
        self.lock = None
        self.lock_seen = 0.0
        self.fx, self.fy = OneEuro(), OneEuro()
        self.history = collections.deque(maxlen=20)  # (Zeit, Zeigerposition) der letzten Bilder
        self.pos = (.5, .5)        # Zeigerposition 0..1
        self.offset = (0.0, 0.0)   # Versatz Hand → Zeiger nach einem Klick, klingt wieder ab
        self.t = 0.0
        self.seen = 0.0
        self.stopped = False       # per Geste angehalten: Kamera läuft weiter, die Maus bleibt in Ruhe
        self.toggled = -9.0
        self.pumps = collections.deque(maxlen=3)  # Zeitpunkte der letzten Faust-Schlüsse
        self.hand_open = None
        self.calibrating = False   # Kalibrierung offen: nur die Handposition melden, die Maus bleibt in Ruhe
        self.raw = None            # Handmitte im Kamerabild (0..1), für die Kalibrierung
        self._idle()

    def _idle(self):
        self.present = self.left = self.right = self.scrolling = self.dragging = self.was_frozen = False
        self.curl = None
        self.press_at = (0.0, 0.0)
        self.scroll_y = self.scroll_rest = 0.0

    def reset_lock(self):
        self.lock = None

    def release(self):
        if self.left and self.act:
            mouse_event(LEFTUP)
        self._idle()
        if self.mover:
            self.mover.idle()
        self.offset = (0.0, 0.0)
        self.history.clear()
        self.fx.reset()
        self.fy.reset()

    def pick(self, result, now):
        hands = []
        for i, lm in enumerate(result.hand_landmarks or []):
            cx = (lm[0].x + lm[5].x + lm[17].x) / 3
            cy = (lm[0].y + lm[5].y + lm[17].y) / 3
            label = result.handedness[i][0].category_name if result.handedness else None
            hands.append({'lm': lm, 'c': (cx, cy), 'size': dist(lm[0], lm[9]), 'label': label})
        hand = None
        if self.lock:
            near = lambda h: math.hypot(h['c'][0] - self.lock[0], h['c'][1] - self.lock[1])
            close = [h for h in hands if near(h) < max(.25, h['size'] * 1.5)]
            hand = min(close, key=near) if close else None
            if not hand:
                if now - self.lock_seen < .7:
                    return None
                self.lock = None
        if not hand:
            # Das Bild wird gespiegelt ausgewertet, dadurch meldet MediaPipe die Hände vertauscht
            want = {'rechts': 'Left', 'links': 'Right'}.get(config['hand'])
            fits = [h for h in hands if not want or h['label'] == want]
            hand = max(fits, key=lambda h: h['size']) if fits else None
        if hand:
            self.lock, self.lock_seen = hand['c'], now
        return hand

    def _pump(self, curl, now):
        """Hand dreimal schnell zu- und aufmachen schaltet die Steuerung aus bzw. wieder ein."""
        if max(curl.values()) < 1.1:
            is_open = False
        elif min(curl.values()) > 1.5:
            is_open = True
        else:
            return
        if is_open == self.hand_open:
            return
        if self.hand_open and not is_open:  # gerade zur Faust geschlossen
            self.pumps.append(now)
            if len(self.pumps) == 3 and now - self.pumps[0] < PUMP_WINDOW and now - self.toggled > 1:
                self.pumps.clear()
                self.toggled = now
                self.stopped = not self.stopped
                if self.left and self.act:
                    mouse_event(LEFTUP)
                self.left = self.right = self.scrolling = self.dragging = False
                if self.act:
                    beep(self.stopped)
        self.hand_open = is_open

    def _anchor(self, now):
        """Zeigerposition kurz vor dem Kneifen – das Schließen der Finger verzieht die Hand leicht."""
        before = [p for t, p in self.history if t <= now - .12]
        if not before:
            return self.pos
        old = before[-1]
        drift = math.hypot(self.pos[0] - old[0], self.pos[1] - old[1])
        return old if drift < .03 else self.pos  # größere Strecke = gewollte Bewegung, nicht zurückspringen

    def _move(self, now, snap=False):
        if self.mover:
            self.mover.aim(self.pos, now, snap)

    def update(self, result, now):
        dt, self.t = min(now - self.t, .2), now
        hand = self.pick(result, now)
        if not hand:
            if self.present and now - self.seen > GRACE:
                self.release()
            return {'held': True} if self.present else None
        self.present, self.seen, self.raw = True, now, hand['c']
        if self.calibrating:
            if self.left and self.act:
                mouse_event(LEFTUP)
            self.left = self.right = self.scrolling = self.dragging = False
            self.curl = None
            if self.mover:
                self.mover.idle()
            return {'calibrating': True, 'raw': hand['c']}
        lm = hand['lm']
        size = max(dist(lm[0], lm[9]), dist(lm[5], lm[17]) * 1.25) or 1e-6
        # Streckung je Finger: ~2 = gestreckt, unter 1 = in die Handfläche eingerollt
        curl = {tip: dist(lm[tip], lm[0]) / (dist(lm[tip - 3], lm[0]) or 1e-6) for tip in (8, 12, 16, 20)}
        self._pump(curl, now)
        closing = abs(sum(curl.values()) - sum(self.curl.values())) / 4 / max(dt, 1e-3) > 2.5 if self.curl else False
        self.curl = curl
        if self.stopped:
            if self.mover:
                self.mover.idle()
            return {'stopped': True}

        pinch = lambda tip: dist(lm[4], lm[tip]) / size
        # Der Finger, der dem Daumen am nächsten ist, bestimmt die Aktion
        left_tip = min((8, 12), key=pinch)
        p_left, p_scroll, p_right = pinch(left_tip), pinch(16), pinch(20)
        nearest = min(p_left, p_scroll, p_right)
        busy = self.left or self.right or self.scrolling
        # Eine Faust oder eine sich gerade schließende/öffnende Hand ist kein Kneifen
        may = lambda tip: curl[tip] >= .95 and not closing

        x0, y0, zw, zh = zone_rect()
        hx = self.fx(clamp((hand['c'][0] - x0) / zw, 0, 1), now)
        hy = self.fy(clamp((hand['c'][1] - y0) / zh, 0, 1), now)

        # Linksklick: Daumen + Zeige- oder Mittelfinger (halten und bewegen = ziehen)
        if self.left:
            if p_left > PINCH_OFF:
                self.left = self.dragging = False
                if self.act:
                    mouse_event(LEFTUP)
        elif p_left < PINCH_ON and p_left == nearest and not busy and may(left_tip):
            self.left, self.press_at = True, (hx, hy)
            self.pos = self._anchor(now)
            self._move(now, snap=True)
            if self.act:
                mouse_event(LEFTDOWN)

        # Rechtsklick: Daumen + kleiner Finger (einmal pro Kneifen)
        if self.right:
            if p_right > PINCH_OFF:
                self.right = False
        elif p_right < PINCH_ON and p_right == nearest and not busy and may(20):
            self.right = True
            self.pos = self._anchor(now)
            self._move(now, snap=True)
            if self.act:
                mouse_event(RIGHTDOWN)
                mouse_event(RIGHTUP)

        # Scrollen: Daumen + Ringfinger halten und die Hand hoch oder runter bewegen
        cy = hand['c'][1]
        if self.scrolling:
            if p_scroll > PINCH_OFF:
                self.scrolling = False
            else:
                self.scroll_rest += (self.scroll_y - cy) * 6000
                steps = int(self.scroll_rest / 30) * 30
                if steps and self.act:
                    mouse_event(WHEEL, steps)
                self.scroll_rest -= steps
        elif p_scroll < PINCH_ON and p_scroll == nearest and not busy and may(16):
            self.scrolling, self.scroll_rest = True, 0.0
            self.pos = self._anchor(now)
        self.scroll_y = cy

        # Zeiger: bleibt beim Klicken stehen; erst eine deutliche Handbewegung beginnt das Ziehen
        if self.left and not self.dragging:
            if math.hypot(hx - self.press_at[0], hy - self.press_at[1]) > DRAG_START:
                self.dragging = True
                self.offset = (self.pos[0] - hx, self.pos[1] - hy)
        frozen = self.scrolling or self.right or (self.left and not self.dragging)
        if not frozen:
            if self.was_frozen and not self.left:  # nach Klick/Scrollen nicht zur Hand springen, sondern gleiten
                self.offset = (self.pos[0] - hx, self.pos[1] - hy)
            if not self.left:  # Versatz sanft abbauen
                k = max(0.0, 1 - dt * 6)
                self.offset = (self.offset[0] * k, self.offset[1] * k)
            self.pos = (clamp(hx + self.offset[0], 0, 1), clamp(hy + self.offset[1], 0, 1))
        self.was_frozen = frozen
        self.history.append((now, self.pos))
        self._move(now)
        return {'label': hand['label'], 'left': self.left, 'right': self.right, 'scroll': self.scrolling,
                'pos': (round(self.pos[0], 3), round(self.pos[1], 3))}


def make_landmarker():
    with open(MODEL_FILE, 'rb') as f:
        model = f.read()
    return vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_buffer=model), running_mode=vision.RunningMode.VIDEO,
        num_hands=2, min_hand_detection_confidence=.6, min_hand_presence_confidence=.6, min_tracking_confidence=.6))


class Camera:
    """Liest die Kamera in einem eigenen Thread und hält nur das neueste Bild bereit.

    So wird nie ein veraltetes Bild aus dem Puffer ausgewertet – das spart spürbar Verzögerung.
    """

    def __init__(self, index):
        self.cap = cv2.VideoCapture(int(index), cv2.CAP_DSHOW)
        self.ok = self.cap.isOpened()
        self.frame = None
        self.fresh = threading.Event()
        if self.ok:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))  # volle Bildrate auch bei wenig Licht
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.cap.set(cv2.CAP_PROP_FPS, 30)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        else:
            self.cap.release()

    def _loop(self):
        while self.ok:
            ok, frame = self.cap.read()
            if not ok:
                self.ok = False
                break
            self.frame = frame
            self.fresh.set()
        self.cap.release()
        self.fresh.set()

    def read(self):
        """Wartet auf das nächste neue Bild; None, wenn die Kamera ausgefallen ist."""
        if not self.fresh.wait(2) or not self.ok:
            return None
        self.fresh.clear()
        return self.frame

    def close(self):
        self.ok = False
        self.thread.join(1.5)  # warten, bis die Kamera wirklich freigegeben ist


class Tracker(threading.Thread):
    def __init__(self, on_status):
        super().__init__(daemon=True)
        self.on_status = on_status
        self.paused = False
        self.stop_event = threading.Event()
        self.mover = Mover()
        self.gestures = Gestures(self.mover)
        self.status = None
        self.want_calibration = threading.Event()  # vom Menü/Hotkey gesetzt, vom Ring-Thread (Tk) abgeholt

    def set_status(self, status):
        if status != self.status:
            self.status = status
            self.on_status(status)

    def run(self):
        landmarker = make_landmarker()
        self.mover.start()
        cap, camera, t0, last_ts, last_hand = None, None, time.perf_counter(), 0, 0.0
        while not self.stop_event.is_set():
            if self.paused or camera != config['camera']:
                if cap:
                    cap.close()
                    cap = None
                self.gestures.release()
                camera = config['camera']
                if self.paused:
                    self.set_status('pause')
                    time.sleep(.2)
                    continue
            if cap is None:
                cap = Camera(config['camera'])
                if not cap.ok:
                    cap = None
                    self.set_status('nocam')
                    time.sleep(2)
                    continue
            frame = cap.read()
            if frame is None:
                cap.close()
                cap = None
                self.gestures.release()
                self.set_status('nocam')
                time.sleep(1)
                continue
            now = time.perf_counter()
            ts = max(last_ts + 1, int((now - t0) * 1000))
            last_ts = ts
            # Gespiegelt wie ein Selfie: Hand nach rechts = Zeiger nach rechts
            rgb = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
            result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
            seen = self.gestures.update(result, now)
            if seen:
                last_hand = now
            self.set_status('gestoppt' if self.gestures.stopped else 'aktiv' if seen else 'suche')
            if not seen and not self.gestures.calibrating:
                if now - last_hand > 3:
                    time.sleep(.15)  # ohne Hand nur ein paar Mal pro Sekunde nachsehen
        if cap:
            cap.close()
        self.gestures.release()
        self.mover.stop_event.set()
        landmarker.close()


class Ring(threading.Thread):
    """Farbiger Ring um den Mauszeiger: zeigt, wo der Zeiger ist und was die Hand gerade tut.

    Ein kleines, durchsichtiges Fenster, das immer oben liegt und Klicks durchlässt.
    """
    SIZE, KEY = 46, '#010203'
    COLORS = {'move': '#35a8ff', 'left': '#2ecc71', 'right': '#ff9f1c', 'scroll': '#b06bff', 'stop': '#ff4d4d'}

    def __init__(self, tracker):
        super().__init__(daemon=True)
        self.tracker = tracker
        self.error = None

    def state(self):
        g = self.tracker.gestures
        if (self.tracker.paused or g.calibrating or not config['ring'] or not g.present
                or self.tracker.mover.yielding()):
            return None
        if g.stopped:  # kurz rot aufleuchten, dann verschwinden
            return 'stop' if g.t - g.toggled < 1.5 else None
        return 'left' if g.left else 'right' if g.right else 'scroll' if g.scrolling else 'move'

    def run(self):
        try:
            import tkinter as tk
            s = self.SIZE
            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes('-topmost', True)
            root.attributes('-transparentcolor', self.KEY)
            root.config(bg=self.KEY)
            canvas = tk.Canvas(root, width=s, height=s, bg=self.KEY, highlightthickness=0)
            canvas.pack()
            canvas.create_oval(3, 3, s - 3, s - 3, outline='#101418', width=5)
            ring = canvas.create_oval(3, 3, s - 3, s - 3, outline=self.COLORS['move'], width=3)
            root.geometry(f'{s}x{s}+-200+-200')
            root.update_idletasks()
            hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
            # durchklickbar, nicht in der Taskleiste, nimmt nie den Fokus
            user32.SetWindowLongW(hwnd, -20, user32.GetWindowLongW(hwnd, -20) | 0x80000 | 0x20 | 0x80 | 0x08000000)
            shown = [None]

            def tick():
                if self.tracker.stop_event.is_set():
                    root.destroy()
                    return
                if self.tracker.want_calibration.is_set():
                    self.tracker.want_calibration.clear()
                    if not self.tracker.gestures.calibrating:
                        Calibration(root, self.tracker)
                state = self.state()
                if state:
                    x, y = cursor_pos()
                    root.geometry(f'+{x - s // 2}+{y - s // 2}')
                    if state != shown[0]:
                        canvas.itemconfig(ring, outline=self.COLORS[state])
                        root.attributes('-topmost', True)
                elif shown[0]:
                    root.geometry('+-200+-200')
                shown[0] = state
                root.after(16 if state else 100, tick)

            tick()
            root.mainloop()
        except Exception as e:  # ohne Ring läuft HandMaus trotzdem weiter
            self.error = e


class Calibration:
    """Vollbild-Fenster zum Festlegen des Bereichs, in dem die Hand den ganzen Bildschirm abdeckt.

    Das Rechteck in der Mitte steht für das Kamerabild, der Punkt für deine Hand (das Kamerabild selbst wird
    nie gezeigt). Den Kasten zieht man mit der echten Maus auf, verschiebt oder vergrößert ihn an den Ecken –
    oder lässt ihn aus einer Handbewegung bestimmen. Je kleiner der Kasten, desto weniger muss sich die Hand
    bewegen und desto schneller läuft der Zeiger. So kann die Hand z. B. aufgestützt bleiben.
    """
    BG, CAM, BOX, HANDLE, TEXT, MUTED = '#0a0d13', '#121722', '#35a8ff', '#ffffff', '#e7ecf5', '#8e9ab3'
    COUNTDOWN, RECORD = 2.0, 5.0  # Sekunden: Vorlauf und Aufnahmedauer beim Festlegen mit der Hand

    def __init__(self, root, tracker):
        import tkinter as tk
        self.tk, self.tracker, self.g = tk, tracker, tracker.gestures
        tracker.paused = False
        self.g.stopped = False
        self.g.calibrating = True
        self.rect = list(zone_rect())
        self.drag = None
        self.record = None  # Startzeit beim Festlegen mit der Hand
        self.points = []
        self.trail = collections.deque(maxlen=40)

        self.win = win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.attributes('-alpha', .94)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f'{sw}x{sh}+0+0')
        self.canvas = c = tk.Canvas(win, width=sw, height=sh, bg=self.BG, highlightthickness=0, cursor='crosshair')
        c.pack()
        # Kamerabild (4:3) so groß wie möglich, oben und unten bleibt Platz für Text und Knöpfe
        ah = sh - 300
        aw = min(sw - 80, ah * 4 / 3)
        ah = aw * 3 / 4
        self.area = ((sw - aw) / 2, 160 + (sh - 300 - ah) / 2, aw, ah)
        ax, ay = self.area[:2]
        c.create_rectangle(ax, ay, ax + aw, ay + ah, fill=self.CAM, outline='#232b3d', width=2)
        c.create_text(ax + 12, ay + 10, anchor='nw', fill=self.MUTED, font=('Segoe UI', 11),
                      text='Kamerabild (gespiegelt) – das Bild selbst wird nie angezeigt')
        c.create_text(sw / 2, 40, fill=self.TEXT, font=('Segoe UI', 22, 'bold'), text='HandMaus kalibrieren')
        c.create_text(sw / 2, 88, fill=self.MUTED, font=('Segoe UI', 12), justify='center', width=sw - 60,
                      text='Ziehe mit der Maus einen Kasten dort auf, wo du deine Hand bewegen willst. '
                           'Verschieben: in den Kasten klicken und ziehen · Größe ändern: an den Ecken ziehen.\n'
                           'Je kleiner der Kasten, desto schneller der Zeiger – die Hand kann dann aufgestützt '
                           'bleiben und muss nur wenig bewegt werden.')
        self.box = c.create_rectangle(0, 0, 0, 0, outline=self.BOX, width=3, fill='#35a8ff', stipple='gray12')
        self.handles = [c.create_rectangle(0, 0, 0, 0, fill=self.HANDLE, outline=self.BOX, width=2) for _ in range(4)]
        self.trail_line = c.create_line(0, 0, 0, 0, fill='#2ecc71', width=2, smooth=True)
        self.dot = c.create_oval(0, 0, 0, 0, fill='#2ecc71', outline='white', width=2)
        self.info = c.create_text(sw / 2, ay + ah + 26, fill=self.TEXT, font=('Segoe UI', 13))
        self.status = c.create_text(sw / 2, ay - 24, fill='#ff9f1c', font=('Segoe UI', 14, 'bold'))

        buttons = [('Mit der Hand festlegen  (H)', self.start_record, '#b06bff'),
                   ('Zurücksetzen  (R)', self.reset, '#3a455e'),
                   ('Abbrechen  (Esc)', self.cancel, '#3a455e'),
                   ('Speichern  (Enter)', self.save, '#2ecc71')]
        x = sw / 2 - 2 * 250 + 125
        for text, cmd, color in buttons:
            b = tk.Button(win, text=text, command=cmd, bg=color, fg='white' if color != '#2ecc71' else '#06101c',
                          activebackground=color, relief='flat', font=('Segoe UI', 12, 'bold'), padx=14, pady=8,
                          cursor='hand2', borderwidth=0)
            c.create_window(x, sh - 50, window=b, width=230)
            x += 250

        c.bind('<ButtonPress-1>', self.press)
        c.bind('<B1-Motion>', self.motion)
        c.bind('<ButtonRelease-1>', self.release)
        win.bind('<Escape>', lambda e: self.cancel())
        win.bind('<Return>', lambda e: self.save())
        for key in 'hH':
            win.bind(key, lambda e: self.start_record())
        for key in 'rR':
            win.bind(key, lambda e: self.reset())
        win.lift()
        win.focus_force()
        self.redraw()
        self.tick()

    # Umrechnung Kamerabild (0..1) <-> Bildschirm
    def to_px(self, u, v):
        ax, ay, aw, ah = self.area
        return ax + u * aw, ay + v * ah

    def to_norm(self, x, y):
        ax, ay, aw, ah = self.area
        return clamp((x - ax) / aw, 0, 1), clamp((y - ay) / ah, 0, 1)

    def corners(self):
        x, y, w, h = self.rect
        return [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]

    def redraw(self):
        x, y, w, h = self.rect
        x0, y0 = self.to_px(x, y)
        x1, y1 = self.to_px(x + w, y + h)
        self.canvas.coords(self.box, x0, y0, x1, y1)
        for handle, (u, v) in zip(self.handles, self.corners()):
            px, py = self.to_px(u, v)
            self.canvas.coords(handle, px - 7, py - 7, px + 7, py + 7)
        speed = (SIZES['normal'] / w + SIZES['normal'] / h) / 2
        self.canvas.itemconfig(self.info, text=f'Zeiger-Tempo ungefähr {speed:.1f}×  (Standard = 1.0×)   ·   '
                                               f'Kasten: {w * 100:.0f} % × {h * 100:.0f} % des Kamerabilds')

    def press(self, e):
        if self.record:
            return
        u, v = self.to_norm(e.x, e.y)
        x, y, w, h = self.rect
        for i, (cu, cv) in enumerate(self.corners()):
            px, py = self.to_px(cu, cv)
            if abs(px - e.x) < 16 and abs(py - e.y) < 16:  # Ecke: die gegenüberliegende Ecke bleibt fest
                self.drag = ('new', self.corners()[3 - i], list(self.rect))
                return
        if x <= u <= x + w and y <= v <= y + h:
            self.drag = ('move', (u - x, v - y), list(self.rect))
        else:
            self.drag = ('new', (u, v), list(self.rect))

    def motion(self, e):
        if not self.drag:
            return
        u, v = self.to_norm(e.x, e.y)
        mode, ref, _ = self.drag
        if mode == 'move':
            w, h = self.rect[2:]
            self.rect = [clamp(u - ref[0], 0, 1 - w), clamp(v - ref[1], 0, 1 - h), w, h]
        else:
            self.rect = [min(u, ref[0]), min(v, ref[1]), abs(u - ref[0]), abs(v - ref[1])]
        self.redraw()

    def release(self, e):
        if self.drag and (self.rect[2] < MIN_ZONE or self.rect[3] < MIN_ZONE):
            self.rect = self.drag[2]  # zu klein (z. B. nur geklickt): alten Kasten behalten
            self.flash('Der Kasten war zu klein – bitte größer aufziehen.')
        self.drag = None
        self.redraw()

    def flash(self, text, ms=2500):
        self.canvas.itemconfig(self.status, text=text)
        self.win.after(ms, lambda: self.record or self.canvas.itemconfig(self.status, text=''))

    def start_record(self):
        if not self.record:
            self.record, self.points = time.perf_counter(), []

    def finish_record(self):
        self.record = None
        if len(self.points) < 10:
            self.flash('Keine Hand erkannt – halte die Hand ins Bild und versuche es noch einmal.', 4000)
            return
        xs, ys = sorted(p[0] for p in self.points), sorted(p[1] for p in self.points)
        cut = len(xs) // 40  # einzelne Ausreißer ignorieren
        x0, x1, y0, y1 = xs[cut], xs[-1 - cut], ys[cut], ys[-1 - cut]
        # etwas verkleinern, damit die Bildschirmränder bequem erreichbar sind
        mx, my = (x1 - x0) * .06, (y1 - y0) * .06
        x0, x1, y0, y1 = x0 + mx, x1 - mx, y0 + my, y1 - my
        w, h = max(x1 - x0, MIN_ZONE), max(y1 - y0, MIN_ZONE)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.rect = [clamp(cx - w / 2, 0, 1 - w), clamp(cy - h / 2, 0, 1 - h), w, h]
        self.redraw()
        self.flash('Bereich übernommen – mit „Speichern“ bestätigen oder mit der Maus anpassen.', 5000)

    def reset(self):
        if not self.record:
            self.rect = list(zone_rect('mitte' if config['zone'] == 'eigen' else config['zone']))
            self.redraw()

    def tick(self):
        if self.tracker.stop_event.is_set():
            self.close()
            return
        now = time.perf_counter()
        hand = self.g.raw if self.g.present and now - self.g.seen < .4 else None
        if hand:
            self.trail.append(self.to_px(*hand))
            px, py = self.trail[-1]
            x, y, w, h = self.rect
            inside = x <= hand[0] <= x + w and y <= hand[1] <= y + h
            color = '#2ecc71' if inside else '#ff9f1c'
            self.canvas.coords(self.dot, px - 11, py - 11, px + 11, py + 11)
            self.canvas.itemconfig(self.dot, fill=color, state='normal')
            if len(self.trail) > 1:
                self.canvas.coords(self.trail_line, *[v for p in self.trail for v in p])
                self.canvas.itemconfig(self.trail_line, state='normal')
        else:
            self.trail.clear()
            self.canvas.itemconfig(self.dot, state='hidden')
            self.canvas.itemconfig(self.trail_line, state='hidden')
        if self.record:
            t = now - self.record
            if t < self.COUNTDOWN:
                text = f'Gleich geht es los … {self.COUNTDOWN - t:.0f}  –  Hand in die Ausgangsposition'
            elif t < self.COUNTDOWN + self.RECORD:
                if hand:
                    self.points.append(hand)
                text = (f'Bewege die Hand jetzt bequem in alle Richtungen, so weit du willst … '
                        f'{self.COUNTDOWN + self.RECORD - t:.0f}')
            else:
                text = ''
                self.finish_record()
            if self.record:
                self.canvas.itemconfig(self.status, text=text)
        elif not hand and not self.canvas.itemcget(self.status, 'text'):
            self.canvas.itemconfig(self.status, text='Keine Hand im Bild')
        elif hand and self.canvas.itemcget(self.status, 'text') == 'Keine Hand im Bild':
            self.canvas.itemconfig(self.status, text='')
        self.win.after(30, self.tick)

    def save(self):
        if self.record:
            return
        config['custom'] = [round(v, 4) for v in self.rect]
        config['zone'] = 'eigen'
        save_config()
        self.close()

    def cancel(self):
        self.close()

    def close(self):
        self.g.calibrating = False
        self.g.reset_lock()
        self.g.fx.reset()
        self.g.fy.reset()
        try:
            self.win.destroy()
        except self.tk.TclError:
            pass


# ---------- Infobereich (Tray) ----------
STATUS = {'aktiv': ('#2ecc71', 'Hand erkannt'), 'suche': ('#3498db', 'sucht Hand'),
          'gestoppt': ('#e74c3c', 'per Geste gestoppt – Hand 3× zu und auf zum Fortsetzen'),
          'pause': ('#7f8c8d', 'Pause – Kamera aus'), 'nocam': ('#e67e22', 'keine Kamera gefunden')}


def make_icon(color):
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=color)
    for x, top in ((17, 20), (26, 12), (35, 14), (44, 20)):  # vier Finger
        d.rounded_rectangle((x, top, x + 6, 44), radius=3, fill='white')
    d.rounded_rectangle((17, 34, 50, 54), radius=8, fill='white')  # Handfläche
    d.polygon(((10, 32), (15, 28), (22, 42), (18, 46)), fill='white')  # Daumen
    return img


RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP)
        return True
    except OSError:
        return False


def toggle_autostart():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if autostart_enabled():
            winreg.DeleteValue(key, APP)
        else:
            winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, f'"{sys.executable}"')


def open_guide(*_):
    """Öffnet die Anleitung im Standard-Browser (als Kopie, weil der Programmordner nur temporär ist)."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        target = os.path.join(CONFIG_DIR, 'Anleitung.html')
        shutil.copyfile(os.path.join(BASE_DIR, 'anleitung.html'), target)
        os.startfile(target)
    except OSError:
        pass


WM_HOTKEY, WM_REBIND = 0x0312, 0x8001  # WM_REBIND (WM_APP + 1): Kalibrier-Taste neu anmelden


class Hotkeys(threading.Thread):
    """Globale Tasten: Strg + Alt + H schaltet Pause um, die einstellbare Kalibrier-Taste öffnet die Kalibrierung."""

    def __init__(self, on_pause, on_calibrate):
        super().__init__(daemon=True)
        self.on_pause, self.on_calibrate = on_pause, on_calibrate
        self.thread_id = None
        self.ready = threading.Event()
        self.calib_ok = True  # False, wenn die Taste schon von einem anderen Programm belegt ist

    def _bind_calibration(self):
        user32.UnregisterHotKey(None, 2)
        _, mods, vk = CALIB_KEYS.get(config['calib_key'], CALIB_KEYS['strg_alt_k'])
        self.calib_ok = not vk or bool(user32.RegisterHotKey(None, 2, mods | MOD_NOREPEAT, vk))

    def rebind(self):
        """Nach dem Ändern der Kalibrier-Taste aufrufen (aus jedem Thread)."""
        if self.ready.wait(2) and self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_REBIND, 0, 0)

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # Nachrichtenschlange anlegen
        user32.RegisterHotKey(None, 1, MOD_CTRL | MOD_ALT | MOD_NOREPEAT, 0x48)
        self._bind_calibration()
        self.ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                (self.on_pause if msg.wParam == 1 else self.on_calibrate)()
            elif msg.message == WM_REBIND:
                self._bind_calibration()


def main():
    try:
        user32.SetProcessDPIAware()
    except OSError:
        pass
    kernel32.CreateMutexW(None, False, 'HandMaus_single_instance')
    if ctypes.get_last_error() == 183:
        user32.MessageBoxW(0, 'HandMaus läuft bereits – siehe Hand-Symbol im Infobereich.', APP, 0x40)
        return
    load_config()

    icons = {name: make_icon(color) for name, (color, _) in STATUS.items()}
    icon = pystray.Icon(APP, icons['suche'], APP)

    def on_status(status):
        icon.icon = icons[status]
        icon.title = f'{APP} – {STATUS[status][1]}'

    tracker = Tracker(on_status)

    def toggle_pause(*_):
        tracker.paused = not tracker.paused
        tracker.gestures.stopped = False
        icon.update_menu()

    def calibrate(*_):
        tracker.want_calibration.set()

    hotkeys = Hotkeys(toggle_pause, calibrate)

    def toggle_ring(*_):
        config['ring'] = not config['ring']
        save_config()

    def choose(key, value):
        def action(*_):
            if key == 'zone' and value == 'eigen' and not custom_rect():
                calibrate()  # noch kein eigener Bereich: erst kalibrieren, Speichern wählt ihn dann aus
                return
            config[key] = value
            tracker.gestures.reset_lock()
            save_config()
            if key == 'calib_key':
                hotkeys.rebind()
                time.sleep(.2)  # kurz warten, ob die Taste angemeldet werden konnte
                if not hotkeys.calib_ok:
                    user32.MessageBoxW(0, f'Die Taste {CALIB_KEYS[value][0]} wird schon von einem anderen Programm '
                                          'benutzt. Bitte wähle eine andere.', APP, 0x30)
        return action

    def calib_label(item):
        name = CALIB_KEYS.get(config['calib_key'], CALIB_KEYS['strg_alt_k'])[0]
        return 'Kalibrieren …' if config['calib_key'] == 'aus' else f'Kalibrieren …  ({name})'

    def radio(key, names):
        return pystray.Menu(*[pystray.MenuItem(text, choose(key, value), radio=True,
                                               checked=lambda item, v=value: config[key] == v)
                              for value, text in names.items()])

    def quit_app(*_):
        tracker.stop_event.set()
        tracker.join(timeout=3)
        icon.stop()

    items = [
        pystray.MenuItem('Aktiv  (Strg+Alt+H)', toggle_pause, checked=lambda item: not tracker.paused, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Hand', radio('hand', HAND_NAMES)),
        pystray.MenuItem('Steuerzone', radio('zone', ZONE_NAMES)),
        pystray.MenuItem('Bewegung', radio('size', SIZE_NAMES)),
        pystray.MenuItem(calib_label, calibrate),
        pystray.MenuItem('Taste für Kalibrieren', radio('calib_key', {k: v[0] for k, v in CALIB_KEYS.items()})),
        pystray.MenuItem('Kamera', radio('camera', {0: 'Kamera 1', 1: 'Kamera 2', 2: 'Kamera 3'})),
        pystray.MenuItem('Ring um den Mauszeiger', toggle_ring, checked=lambda item: config['ring']),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Anleitung öffnen', open_guide),
    ]
    if getattr(sys, 'frozen', False):
        items.append(pystray.MenuItem('Mit Windows starten', lambda *_: toggle_autostart(),
                                      checked=lambda item: autostart_enabled()))
    items += [pystray.Menu.SEPARATOR, pystray.MenuItem('Beenden', quit_app)]
    icon.menu = pystray.Menu(*items)

    def setup(icon):
        icon.visible = True
        tracker.start()
        Ring(tracker).start()
        hotkeys.start()
        if not config['guide_seen']:  # beim ersten Start einmal die Anleitung zeigen
            config['guide_seen'] = True
            save_config()
            open_guide()

    icon.run(setup)


def check_image(path, out_file=None):
    """Selbsttest ohne Kamera und ohne Mausbewegung: wertet ein Foto wie ein Kamerabild aus."""
    image = cv2.imread(path)
    landmarker, gestures = make_landmarker(), Gestures()
    rgb = cv2.cvtColor(cv2.flip(image, 1), cv2.COLOR_BGR2RGB)
    out = None
    for i in range(5):
        result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), i * 33)
        out = gestures.update(result, i * .033)
    if out_file:
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(out, f)
    else:
        print(json.dumps(out))


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == '--check':
        check_image(*sys.argv[2:4])
    elif sys.argv[1:] == ['--anleitung']:
        open_guide()
    else:
        main()
