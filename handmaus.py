"""HandMaus – steuert den Mauszeiger berührungslos per Webcam.

Läuft ohne Fenster im Infobereich (Tray). Das Kamerabild wird nur im Arbeitsspeicher
ausgewertet, nie angezeigt, gespeichert oder verschickt.
"""
import ctypes
import json
import math
import os
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

HELP_TEXT = (
    'Hand bewegen  →  Mauszeiger bewegen\n'
    'Daumen + Zeigefinger kurz zusammen  →  Linksklick\n'
    'Daumen + Zeigefinger halten und bewegen  →  Ziehen / Markieren\n'
    'Daumen + Mittelfinger zusammen  →  Rechtsklick\n'
    'Zeige- und Mittelfinger gestreckt (✌), Hand hoch/runter  →  Scrollen\n\n'
    'Strg + Alt + H  →  Pause / Weiter\n\n'
    'Einstellungen: Rechtsklick auf das Hand-Symbol im Infobereich.\n'
    'In der Pause ist die Kamera ausgeschaltet.'
)

# Steuerzone im gespiegelten Kamerabild (0..1): Mittelpunkt je Voreinstellung, Kantenlänge je Bewegungsgröße
ZONES = {'mitte': (.5, .5), 'unten': (.5, .75), 'links': (.25, .5), 'rechts': (.75, .5),
         'linksunten': (.25, .75), 'rechtsunten': (.75, .75)}
ZONE_NAMES = {'mitte': 'Mitte', 'unten': 'Unten', 'links': 'Links', 'rechts': 'Rechts',
              'linksunten': 'Links unten', 'rechtsunten': 'Rechts unten'}
SIZES = {'gross': .6, 'normal': .42, 'klein': .28}
SIZE_NAMES = {'gross': 'Groß (weite Handbewegung)', 'normal': 'Normal', 'klein': 'Klein (kurze Handbewegung)'}
HAND_NAMES = {'auto': 'Automatisch', 'rechts': 'Rechte Hand', 'links': 'Linke Hand'}

config = {'hand': 'auto', 'zone': 'mitte', 'size': 'normal', 'camera': 0}


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
user32 = ctypes.windll.user32
LEFTDOWN, LEFTUP, RIGHTDOWN, RIGHTUP, WHEEL = 0x0002, 0x0004, 0x0008, 0x0010, 0x0800


def mouse_event(flags, data=0):
    user32.mouse_event(flags, 0, 0, data, 0)


class OneEuro:
    """Glättet Zittern bei ruhiger Hand, bleibt bei schnellen Bewegungen direkt."""

    def __init__(self, min_cutoff=1.2, beta=10.0, d_cutoff=1.0):
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
        a_d = self._alpha(self.d_cutoff, dt)
        self.dx += a_d * (dx - self.dx)
        a = self._alpha(self.min_cutoff + self.beta * abs(self.dx), dt)
        self.x += a * (x - self.x)
        self.t = t
        return self.x


def dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def zone_rect():
    s = SIZES.get(config['size'], SIZES['normal'])
    cx, cy = ZONES.get(config['zone'], ZONES['mitte'])
    fit = lambda v: clamp(v, s / 2 + .02, 1 - s / 2 - .02)
    return fit(cx) - s / 2, fit(cy) - s / 2, s


class Gestures:
    """Wählt eine Hand aus, bleibt bei ihr und leitet daraus Zeiger, Klicks und Scrollen ab."""

    def __init__(self, act=True):
        self.act = act  # False im Selbsttest: nichts an die Maus schicken
        self.lock = None
        self.lock_seen = 0.0
        self.left = self.right = self.scrolling = False
        self.left_since = 0.0
        self.scroll_y = self.scroll_rest = 0.0
        self.fx, self.fy = OneEuro(), OneEuro()
        self.last_px = None
        self.present = False

    def reset_lock(self):
        self.lock = None

    def release(self):
        if self.left and self.act:
            mouse_event(LEFTUP)
        self.left = self.right = self.scrolling = False
        self.present = False
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

    def update(self, result, now):
        hand = self.pick(result, now)
        if not hand:
            if self.present:
                self.release()
            return None
        self.present = True
        lm = hand['lm']
        size = max(dist(lm[0], lm[9]), dist(lm[5], lm[17]) * 1.25) or 1e-6
        pinch_i, pinch_m = dist(lm[4], lm[8]) / size, dist(lm[4], lm[12]) / size
        ext = lambda i: dist(lm[i], lm[0]) > dist(lm[i - 2], lm[0]) * 1.15

        # Linksklick: Daumen + Zeigefinger (gedrückt halten = ziehen)
        if self.left:
            if pinch_i > .48:
                self.left = False
                if self.act:
                    mouse_event(LEFTUP)
        elif pinch_i < .30 and pinch_i <= pinch_m and not self.right:
            self.left, self.left_since = True, now
            if self.act:
                mouse_event(LEFTDOWN)

        # Rechtsklick: Daumen + Mittelfinger (einmal pro Kneifen)
        if self.right:
            if pinch_m > .48:
                self.right = False
        elif pinch_m < .30 and pinch_m < pinch_i and not self.left:
            self.right = True
            if self.act:
                mouse_event(RIGHTDOWN)
                mouse_event(RIGHTUP)

        # Scrollen: Zeige- und Mittelfinger gestreckt, Ring- und kleiner Finger eingeklappt
        pose = (not self.left and not self.right and pinch_i > .5 and pinch_m > .5
                and ext(8) and ext(12) and not ext(16) and not ext(20))
        cy = hand['c'][1]
        if pose and self.scrolling:
            self.scroll_rest += (self.scroll_y - cy) * 6000
            steps = int(self.scroll_rest / 30) * 30
            if steps and self.act:
                mouse_event(WHEEL, steps)
            self.scroll_rest -= steps
        self.scrolling, self.scroll_y = pose, cy
        if not pose:
            self.scroll_rest = 0.0

        # Zeiger: beim Klicken kurz festhalten, damit der Klick nicht verrutscht
        x0, y0, s = zone_rect()
        nx = self.fx(clamp((hand['c'][0] - x0) / s, 0, 1), now)
        ny = self.fy(clamp((hand['c'][1] - y0) / s, 0, 1), now)
        frozen = self.scrolling or self.right or (self.left and now - self.left_since < .3)
        if not frozen and self.act:
            w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
            px = (round(nx * (w - 1)), round(ny * (h - 1)))
            if self.last_px is None or abs(px[0] - self.last_px[0]) + abs(px[1] - self.last_px[1]) >= 2:
                user32.SetCursorPos(*px)
                self.last_px = px
        return {'label': hand['label'], 'left': self.left, 'right': self.right, 'scroll': self.scrolling,
                'pos': (round(nx, 3), round(ny, 3))}


def make_landmarker():
    with open(MODEL_FILE, 'rb') as f:
        model = f.read()
    return vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_buffer=model), running_mode=vision.RunningMode.VIDEO,
        num_hands=2, min_hand_detection_confidence=.6, min_hand_presence_confidence=.6, min_tracking_confidence=.6))


class Tracker(threading.Thread):
    def __init__(self, on_status):
        super().__init__(daemon=True)
        self.on_status = on_status
        self.paused = False
        self.stop_event = threading.Event()
        self.gestures = Gestures()
        self.status = None

    def set_status(self, status):
        if status != self.status:
            self.status = status
            self.on_status(status)

    def open_camera(self):
        cap = cv2.VideoCapture(int(config['camera']), cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def run(self):
        landmarker = make_landmarker()
        cap, camera, t0, last_ts, last_hand = None, None, time.perf_counter(), 0, 0.0
        while not self.stop_event.is_set():
            if self.paused or camera != config['camera']:
                if cap:
                    cap.release()
                    cap = None
                self.gestures.release()
                camera = config['camera']
                if self.paused:
                    self.set_status('pause')
                    time.sleep(.2)
                    continue
            if cap is None:
                cap = self.open_camera()
                if cap is None:
                    self.set_status('nocam')
                    time.sleep(2)
                    continue
            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = None
                self.gestures.release()
                self.set_status('nocam')
                time.sleep(1)
                continue
            now = time.perf_counter()
            ts = max(last_ts + 1, int((now - t0) * 1000))
            last_ts = ts
            # Gespiegelt wie ein Selfie: Hand nach rechts = Zeiger nach rechts, und die Händigkeit stimmt
            rgb = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
            result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
            if self.gestures.update(result, now):
                last_hand = now
                self.set_status('aktiv')
            else:
                self.set_status('suche')
                if now - last_hand > 3:
                    time.sleep(.15)  # ohne Hand nur ein paar Mal pro Sekunde nachsehen
        if cap:
            cap.release()
        self.gestures.release()
        landmarker.close()


# ---------- Infobereich (Tray) ----------
STATUS = {'aktiv': ('#2ecc71', 'Hand erkannt'), 'suche': ('#3498db', 'sucht Hand'),
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


def message_box(text):
    threading.Thread(target=lambda: user32.MessageBoxW(0, text, APP, 0x40), daemon=True).start()


def hotkey_loop(callback):
    """Strg + Alt + H schaltet Pause um."""
    from ctypes import wintypes
    if not user32.RegisterHotKey(None, 1, 0x0001 | 0x0002, 0x48):
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312:
            callback()


def main():
    try:
        user32.SetProcessDPIAware()
    except OSError:
        pass
    ctypes.windll.kernel32.CreateMutexW(None, False, 'HandMaus_single_instance')
    if ctypes.windll.kernel32.GetLastError() == 183:
        user32.MessageBoxW(0, 'HandMaus läuft bereits – siehe Hand-Symbol im Infobereich.', APP, 0x40)
        return
    load_config()

    icon = pystray.Icon(APP, make_icon(STATUS['suche'][0]), APP)

    def on_status(status):
        color, text = STATUS[status]
        icon.icon = make_icon(color)
        icon.title = f'{APP} – {text}'

    tracker = Tracker(on_status)

    def toggle_pause(*_):
        tracker.paused = not tracker.paused
        icon.update_menu()

    def choose(key, value):
        def action(*_):
            config[key] = value
            tracker.gestures.reset_lock()
            save_config()
        return action

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
        pystray.MenuItem('Kamera', radio('camera', {0: 'Kamera 1', 1: 'Kamera 2', 2: 'Kamera 3'})),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Gesten anzeigen', lambda *_: message_box(HELP_TEXT)),
    ]
    if getattr(sys, 'frozen', False):
        items.append(pystray.MenuItem('Mit Windows starten', lambda *_: toggle_autostart(),
                                      checked=lambda item: autostart_enabled()))
    items += [pystray.Menu.SEPARATOR, pystray.MenuItem('Beenden', quit_app)]
    icon.menu = pystray.Menu(*items)

    def setup(icon):
        icon.visible = True
        tracker.start()
        threading.Thread(target=hotkey_loop, args=(toggle_pause,), daemon=True).start()
        if not os.path.exists(CONFIG_FILE):
            save_config()
            message_box('HandMaus läuft jetzt im Infobereich (Hand-Symbol unten rechts).\n\n' + HELP_TEXT)

    icon.run(setup)


def check_image(path, out_file=None):
    """Selbsttest ohne Kamera und ohne Mausbewegung: wertet ein Foto wie ein Kamerabild aus."""
    image = cv2.imread(path)
    landmarker, gestures = make_landmarker(), Gestures(act=False)
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
    else:
        main()
