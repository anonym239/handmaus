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
    'Daumen + Zeigefinger ODER Daumen + Mittelfinger kurz zusammen  →  Linksklick\n'
    'Zusammenhalten und Hand bewegen  →  Ziehen / Markieren\n'
    'Daumen + Ringfinger halten, Hand hoch/runter  →  Scrollen\n'
    'Daumen + kleiner Finger zusammen  →  Rechtsklick\n\n'
    'Der Ring um den Mauszeiger zeigt den Zustand:\n'
    'Blau = Zeiger, Grün = Linksklick, Orange = Rechtsklick, Lila = Scrollen\n\n'
    'Strg + Alt + H  →  Pause / Weiter\n'
    'Bewegst du die echte Maus, lässt HandMaus den Zeiger kurz los.\n\n'
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

config = {'hand': 'auto', 'zone': 'mitte', 'size': 'normal', 'camera': 0, 'ring': True}


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

    def __init__(self, min_cutoff=1.0, beta=10.0, d_cutoff=1.0):
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


def zone_rect():
    s = SIZES.get(config['size'], SIZES['normal'])
    cx, cy = ZONES.get(config['zone'], ZONES['mitte'])
    fit = lambda v: clamp(v, s / 2 + .02, 1 - s / 2 - .02)
    return fit(cx) - s / 2, fit(cy) - s / 2, s


PINCH_ON, PINCH_OFF = .30, .48  # Abstand Daumen–Fingerspitze relativ zur Handgröße
DRAG_START = .035               # so weit muss sich die Hand beim Halten bewegen, bevor gezogen wird
GRACE = .25                     # so lange darf die Hand kurz verschwinden, ohne dass losgelassen wird


class Gestures:
    """Wählt eine Hand aus, bleibt bei ihr und leitet daraus Zeiger, Klicks und Scrollen ab."""

    def __init__(self, act=True):
        self.act = act  # False im Selbsttest: nichts an die Maus schicken
        self.lock = None
        self.lock_seen = 0.0
        self.fx, self.fy = OneEuro(), OneEuro()
        self.history = collections.deque(maxlen=20)  # (Zeit, Zeigerposition) der letzten Bilder
        self.pos = (.5, .5)        # Zeigerposition 0..1
        self.offset = (0.0, 0.0)   # Versatz Hand → Zeiger nach einem Klick, klingt wieder ab
        self.sent_px = self.last_px = None
        self.yield_until = 0.0     # die echte Maus wurde bewegt: bis dahin nicht eingreifen
        self.t = 0.0
        self.seen = 0.0
        self._idle()

    def _idle(self):
        self.present = self.left = self.right = self.scrolling = self.dragging = self.was_frozen = False
        self.press_at = (0.0, 0.0)
        self.scroll_y = self.scroll_rest = 0.0

    def reset_lock(self):
        self.lock = None

    def release(self):
        if self.left and self.act:
            mouse_event(LEFTUP)
        self._idle()
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

    def _anchor(self, now):
        """Zeigerposition kurz vor dem Kneifen – das Schließen der Finger verzieht die Hand leicht."""
        before = [p for t, p in self.history if t <= now - .12]
        if not before:
            return self.pos
        old = before[-1]
        drift = math.hypot(self.pos[0] - old[0], self.pos[1] - old[1])
        return old if drift < .03 else self.pos  # größere Strecke = gewollte Bewegung, nicht zurückspringen

    def _move(self, now):
        if not self.act:
            return
        w, h = screen_size()
        px = (round(self.pos[0] * (w - 1)), round(self.pos[1] * (h - 1)))
        here = cursor_pos()
        if now < self.yield_until:  # solange die echte Maus weiterbewegt wird, weiter warten
            if here != self.last_px:
                self.yield_until = now + 1.0
            self.last_px = here
            return
        # Steht der Zeiger nicht dort, wo wir ihn hingesetzt haben, hat jemand die echte Maus bewegt
        if self.sent_px and abs(here[0] - self.sent_px[0]) + abs(here[1] - self.sent_px[1]) > 8:
            self.yield_until, self.sent_px, self.last_px = now + 1.0, None, here
            return
        if px != self.sent_px:
            move_cursor(*px)
            self.sent_px = px

    def update(self, result, now):
        dt, self.t = min(now - self.t, .2), now
        hand = self.pick(result, now)
        if not hand:
            if self.present and now - self.seen > GRACE:
                self.release()
            return {'held': True} if self.present else None
        self.present, self.seen = True, now
        lm = hand['lm']
        size = max(dist(lm[0], lm[9]), dist(lm[5], lm[17]) * 1.25) or 1e-6
        pinch = lambda tip: dist(lm[4], lm[tip]) / size
        # Der Finger, der dem Daumen am nächsten ist, bestimmt die Aktion
        p_left, p_scroll, p_right = min(pinch(8), pinch(12)), pinch(16), pinch(20)
        nearest = min(p_left, p_scroll, p_right)
        busy = self.left or self.right or self.scrolling

        x0, y0, s = zone_rect()
        hx = self.fx(clamp((hand['c'][0] - x0) / s, 0, 1), now)
        hy = self.fy(clamp((hand['c'][1] - y0) / s, 0, 1), now)

        # Linksklick: Daumen + Zeige- oder Mittelfinger (halten und bewegen = ziehen)
        if self.left:
            if p_left > PINCH_OFF:
                self.left = self.dragging = False
                if self.act:
                    mouse_event(LEFTUP)
        elif p_left < PINCH_ON and p_left == nearest and not busy:
            self.left, self.press_at = True, (hx, hy)
            self.pos = self._anchor(now)
            self._move(now)
            if self.act:
                mouse_event(LEFTDOWN)

        # Rechtsklick: Daumen + kleiner Finger (einmal pro Kneifen)
        if self.right:
            if p_right > PINCH_OFF:
                self.right = False
        elif p_right < PINCH_ON and p_right == nearest and not busy:
            self.right = True
            self.pos = self._anchor(now)
            self._move(now)
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
        elif p_scroll < PINCH_ON and p_scroll == nearest and not busy:
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
            # Gespiegelt wie ein Selfie: Hand nach rechts = Zeiger nach rechts
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


class Ring(threading.Thread):
    """Farbiger Ring um den Mauszeiger: zeigt, wo der Zeiger ist und was die Hand gerade tut.

    Ein kleines, durchsichtiges Fenster, das immer oben liegt und Klicks durchlässt.
    """
    SIZE, KEY = 46, '#010203'
    COLORS = {'move': '#35a8ff', 'left': '#2ecc71', 'right': '#ff9f1c', 'scroll': '#b06bff'}

    def __init__(self, tracker):
        super().__init__(daemon=True)
        self.tracker = tracker
        self.error = None

    def state(self):
        g = self.tracker.gestures
        if self.tracker.paused or not config['ring'] or not g.present or g.t < g.yield_until:
            return None
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
        icon.update_menu()

    def toggle_ring(*_):
        config['ring'] = not config['ring']
        save_config()

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
        pystray.MenuItem('Ring um den Mauszeiger', toggle_ring, checked=lambda item: config['ring']),
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
        Ring(tracker).start()
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
