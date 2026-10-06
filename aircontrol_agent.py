"""AirControl AI desktop agent.

Reads your webcam, tracks one hand with MediaPipe, and controls the real mouse
and keyboard. The dashboard (aircontrol-ai.html) starts/stops it over a local
WebSocket on 127.0.0.1:8765.

Run:  python aircontrol_agent.py [--camera 0] [--port 8765] [--web-port 8766] [--preview]
It serves the dashboard at http://127.0.0.1:8766 and opens it in your browser.
Emergency stop: Ctrl+Alt+Q
"""
import argparse
import asyncio
import functools
import http.server
import json
import math
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from urllib.parse import urlparse

if sys.platform == "win32":  # must run before any mouse code: use real pixels
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision
import websockets
from pynput.keyboard import Controller as Keyboard, GlobalHotKeys, Key
from pynput.mouse import Button, Controller as Mouse

PINCH_ON, PINCH_OFF = 0.30, 0.42   # pinch distance / hand size (hysteresis)
HOLD_TO_DRAG = 0.35                # seconds a pinch is held before it becomes a drag
STABLE_FRAMES = 3                  # frames a pose must persist to count
MOVE_MODES = ("move", "drag")


MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/1/hand_landmarker.task")
CONNECTIONS = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10),
               (10, 11), (11, 12), (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (17, 18),
               (18, 19), (19, 20), (0, 17)]


def ensure_model():
    """Find (or download once) the MediaPipe hand model file."""
    here = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "hand_landmarker.task")
    if os.path.exists(here):
        return here
    cache = os.path.join(os.path.expanduser("~"), ".aircontrol", "hand_landmarker.task")
    if not os.path.exists(cache):
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        print("Downloading hand model (one time, about 8 MB)...")
        try:
            urllib.request.urlretrieve(MODEL_URL, cache + ".part")
            os.replace(cache + ".part", cache)
        except Exception as e:
            raise RuntimeError(f"Could not download the hand model ({e}). Connect to the internet once, "
                               f"or download {MODEL_URL} and put it next to aircontrol_agent.py.")
    return cache


class Tracker:
    """MediaPipe Tasks HandLandmarker (the current, supported API)."""

    def __init__(self):
        opts = vision.HandLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(model_asset_path=ensure_model()),
            running_mode=vision.RunningMode.VIDEO, num_hands=1,
            min_hand_detection_confidence=0.6, min_hand_presence_confidence=0.6,
            min_tracking_confidence=0.6)
        self.lm = vision.HandLandmarker.create_from_options(opts)
        self.last_ts = 0

    def detect(self, rgb, now):
        ts = max(int(now * 1000), self.last_ts + 1)   # timestamps must increase
        self.last_ts = ts
        res = self.lm.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
        if not res.hand_landmarks:
            return None
        return [(l.x, l.y) for l in res.hand_landmarks[0]]

    def close(self):
        self.lm.close()


def draw_preview(frame, P):
    h, w = frame.shape[:2]
    pts = [(int(x * w), int(y * h)) for x, y in P]
    for a, b in CONNECTIONS:
        cv2.line(frame, pts[a], pts[b], (200, 230, 120), 2)
    for pt in pts:
        cv2.circle(frame, pt, 4, (40, 140, 255), -1)


def screen_size():
    if sys.platform == "win32":
        u = ctypes.windll.user32
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    import tkinter
    r = tkinter.Tk()
    r.withdraw()
    w, h = r.winfo_screenwidth(), r.winfo_screenheight()
    r.destroy()
    return w, h


class OneEuro:
    """One Euro filter: smooth when slow (no jitter), responsive when fast."""

    def __init__(self, min_cutoff=1.3, beta=4.0, dcut=1.0):
        self.min_cutoff, self.beta, self.dcut = min_cutoff, beta, dcut
        self.reset()

    def reset(self):
        self.x = None
        self.dx = 0.0
        self.t = None

    @staticmethod
    def _alpha(cut, dt):
        tau = 1.0 / (2 * math.pi * cut)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, v, t):
        if self.x is None:
            self.x, self.t = v, t
            return v
        dt = max(t - self.t, 1e-3)
        self.t = t
        self.dx += self._alpha(self.dcut, dt) * ((v - self.x) / dt - self.dx)
        cut = self.min_cutoff + self.beta * abs(self.dx)
        self.x += self._alpha(cut, dt) * (v - self.x)
        return self.x


class Engine:
    """Turns hand landmarks into real mouse/keyboard actions."""

    def __init__(self, emit):
        self.emit_cb = emit
        self.mouse, self.kb = Mouse(), Keyboard()
        self.W, self.H = screen_size()
        self.cfg = {"cursorSpeed": 50, "smoothing": 60, "scrollSpeed": 40}
        self.en = {k: True for k in ("move", "click", "rclick", "dbl", "scroll", "zoom", "pause")}
        self.fx, self.fy = OneEuro(), OneEuro()
        self.last_name = None
        self.reset()

    def configure(self, s):
        for k in self.cfg:
            if k in s:
                self.cfg[k] = float(s[k])
        for k, v in (s.get("gestures") or {}).items():
            if k in self.en:
                self.en[k] = bool(v)
        mc = 3.0 - 2.8 * self.cfg["smoothing"] / 100.0
        self.fx.min_cutoff = self.fy.min_cutoff = mc

    def emit(self, name, detail="", force=False):
        if force or name != self.last_name:
            self.last_name = name
            self.emit_cb({"gesture": name, "detail": detail})

    def release(self):
        if getattr(self, "dragging", False):
            self.mouse.release(Button.left)
        self.dragging = False

    def reset(self):
        self.release()
        self.paused = self.pinching = self.rpinch = False
        self.pinch_t = self.last_click = 0.0
        self.mode, self.pose = "none", "none"
        self.cand, self.cand_n = None, 0
        self.offset = (0.0, 0.0)
        self.reanchor = True
        self.prev_y = self.prev_d = None
        self.acc = 0.0
        self.fx.reset()
        self.fy.reset()

    def lost(self):
        """Hand left the frame: freeze the cursor, drop any held button."""
        self.release()
        self.pinching = self.rpinch = False
        self.enter("none")

    def enter(self, mode):
        if mode != self.mode:
            if mode in MOVE_MODES and self.mode not in MOVE_MODES:
                self.reanchor = True   # continue from where the cursor is, no jump
                self.fx.reset()
                self.fy.reset()
            if mode not in ("scroll", "zoom"):
                self.prev_y = self.prev_d = None
                self.acc = 0.0
            self.mode = mode

    def move(self, p, now):
        zw = 0.9 - 0.5 * self.cfg["cursorSpeed"] / 100.0   # active zone width
        nx = min(max((p[0] - 0.5) / zw + 0.5, 0.0), 1.0)
        ny = min(max((p[1] - 0.45) / (zw * 0.75) + 0.5, 0.0), 1.0)
        sx, sy = self.fx(nx, now) * self.W, self.fy(ny, now) * self.H
        cx, cy = self.mouse.position
        if self.reanchor:
            self.offset = (cx - sx, cy - sy)
            self.reanchor = False
        self.offset = (self.offset[0] * 0.92, self.offset[1] * 0.92)  # ease back to absolute map
        x = min(max(sx + self.offset[0], 0), self.W - 1)
        y = min(max(sy + self.offset[1], 0), self.H - 1)
        if abs(x - cx) > 1 or abs(y - cy) > 1:
            self.mouse.position = (int(x), int(y))

    def update(self, P, now):
        def d(a, b):
            return math.hypot(P[a][0] - P[b][0], P[a][1] - P[b][1])

        size = max(d(0, 9), 1e-6)
        fin = lambda tip, pip: d(tip, 0) > d(pip, 0) * 1.25
        idx, mid, ring, pin = fin(8, 6), fin(12, 10), fin(16, 14), fin(20, 18)
        thm = d(4, 17) > d(3, 17) * 1.1
        n = idx + mid + ring + pin
        if n == 0:
            raw = "fist"
        elif n == 4:
            raw = "palm"
        elif idx and mid and not ring and not pin:
            raw = "zoom" if thm else "scroll"
        elif idx and not mid and not ring and not pin:
            raw = "point"
        else:
            raw = "other"
        self.cand_n = self.cand_n + 1 if raw == self.cand else 1
        self.cand = raw
        if self.cand_n >= STABLE_FRAMES:
            self.pose = raw

        r_ti, r_tm = d(4, 8) / size, d(4, 12) / size
        pinch = r_ti < (PINCH_OFF if self.pinching else PINCH_ON) and (mid or ring or pin or self.pinching)

        # Clutch: fist pauses everything, open palm resumes.
        if self.paused:
            if self.pose == "palm":
                self.paused = False
                self.emit("Resume control", "Open palm")
            else:
                return
        if self.pose == "fist" and self.en["pause"] and not pinch:
            self.paused = True
            self.release()
            self.pinching = self.rpinch = False
            self.enter("none")
            self.emit("Pause control", "Fist: control paused")
            return

        # Left click / drag (thumb + index pinch)
        if pinch and self.en["click"]:
            if not self.pinching:
                self.pinching, self.pinch_t = True, now
            elif not self.dragging and now - self.pinch_t > HOLD_TO_DRAG:
                self.mouse.press(Button.left)
                self.dragging = True
                self.emit("Drag", "Pinch held", True)
            if self.dragging:
                self.enter("drag")
                self.move(P[8], now)
            else:
                self.enter("freeze")   # keep cursor still while pinching
            return
        if self.pinching:
            if self.dragging:
                self.mouse.release(Button.left)
                self.dragging = False
            else:
                self.mouse.click(Button.left)
                if now - self.last_click < 0.5:
                    self.emit("Double click", "Two quick pinches", True)
                else:
                    self.emit("Left click", "Thumb and index pinch", True)
                self.last_click = now
            self.pinching = False
            return

        # Right click (thumb + middle pinch with index up)
        rp = r_tm < (PINCH_OFF if self.rpinch else PINCH_ON) and idx
        if rp and not self.rpinch and self.en["rclick"]:
            self.mouse.click(Button.right)
            self.emit("Right click", "Thumb and middle pinch", True)
        self.rpinch = rp
        if rp:
            self.enter("freeze")
            return

        if self.pose == "zoom" and self.en["zoom"]:
            self.enter("zoom")
            if self.prev_d is not None:
                self.acc += (r_ti - self.prev_d) * 25.0
                steps = int(self.acc)
                if steps:
                    self.acc -= steps
                    with self.kb.pressed(Key.ctrl):
                        self.mouse.scroll(0, steps)
                    self.emit("Zoom", "In" if steps > 0 else "Out")
            self.prev_d = r_ti
        elif self.pose == "scroll" and self.en["scroll"]:
            self.enter("scroll")
            y = (P[8][1] + P[12][1]) / 2
            if self.prev_y is not None and abs(self.prev_y - y) > 0.002:
                self.acc += (self.prev_y - y) * 100.0 * self.cfg["scrollSpeed"] / 40.0
                steps = int(self.acc)
                if steps:
                    self.acc -= steps
                    self.mouse.scroll(0, steps)
                    self.emit("Scroll", "Up" if steps > 0 else "Down")
            self.prev_y = y
        elif self.pose == "point" and self.en["move"]:
            self.enter("move")
            self.move(P[8], now)
            self.emit("Move cursor", "Pointer follows index finger")
        else:
            self.enter("none")


ALLOW_FILE = os.path.join(os.path.expanduser("~"), ".aircontrol", "allowed_origins.json")


def load_allowed():
    try:
        with open(ALLOW_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def remember_origin(origin):
    allowed = load_allowed()
    allowed.add(origin)
    os.makedirs(os.path.dirname(ALLOW_FILE), exist_ok=True)
    with open(ALLOW_FILE, "w") as f:
        json.dump(sorted(allowed), f)


def ask_permission(origin):
    """Windows pop-up: let the user approve a new website once (no typing needed)."""
    if sys.platform != "win32":
        return False
    text = (f"The website\n{origin}\nwants to control this laptop with your hand gestures.\n\n"
            "Only allow websites you trust. Allow it?")
    # 0x04 Yes/No, 0x20 question icon, 0x1000 always on top
    return ctypes.windll.user32.MessageBoxW(0, text, "AirControl AI", 0x04 | 0x20 | 0x1000) == 6


def origin_ok(origin):
    if origin in (None, "null"):          # local file or non-browser client
        return True
    if origin in load_allowed():
        return True
    host = urlparse(origin).hostname or ""
    extra = [x.strip() for x in os.environ.get("AIRCONTROL_ORIGINS", "").split(",") if x.strip()]
    allowed = ("localhost", "127.0.0.1", "claude.ai", "claudeusercontent.com", *extra)
    return any(host == h or host.endswith("." + h) for h in allowed)


class Agent:
    def __init__(self, camera, preview):
        self.camera, self.preview = camera, preview
        self.clients = set()
        self.loop = None
        self.thread = None
        self.stop_evt = threading.Event()
        self.engine = Engine(self.send)

    def send(self, msg):
        if self.loop and self.clients:
            asyncio.run_coroutine_threadsafe(self._broadcast(json.dumps(msg)), self.loop)

    async def _broadcast(self, text):
        for ws in list(self.clients):
            try:
                await ws.send(text)
            except Exception:
                self.clients.discard(ws)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_evt.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        print("Sensing started")

    def stop(self):
        self.stop_evt.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self.engine.reset()
        print("Sensing stopped")

    def _run(self):
        cap = hands = None
        try:
            api = cv2.CAP_DSHOW if sys.platform == "win32" else 0
            cap = cv2.VideoCapture(self.camera, api)
            if not cap.isOpened():
                self.send({"gesture": "Camera error", "detail": "Could not open the camera. Close other apps using it."})
                return
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            hands = Tracker()
            self.engine.reset()
            frame_no, had_hand = 0, False
            while not self.stop_evt.is_set():
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.01)
                    continue
                frame = cv2.flip(frame, 1)
                now = time.time()
                P = hands.detect(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), now)
                frame_no += 1
                if P:
                    self.engine.update(P, now)
                    had_hand = True
                    if frame_no % 2 == 0:
                        self.send({"lm": P})
                    if self.preview:
                        draw_preview(frame, P)
                else:
                    self.engine.lost()
                    if had_hand:
                        self.send({"lm": None})
                        had_hand = False
                if self.preview:
                    cv2.imshow("AirControl preview (Esc to stop)", frame)
                    if cv2.waitKey(1) == 27:
                        break
        except Exception as e:  # report to dashboard instead of dying silently
            print("Tracking error:", e)
            self.send({"gesture": "Tracking error", "detail": str(e)[:120]})
        finally:
            self.engine.reset()
            if cap:
                cap.release()
            if hands:
                hands.close()
            cv2.destroyAllWindows()

    async def handler(self, ws):
        try:
            origin = ws.request.headers.get("Origin")
        except AttributeError:
            origin = ws.request_headers.get("Origin")
        if not origin_ok(origin):
            approved = await asyncio.get_running_loop().run_in_executor(None, ask_permission, origin)
            if not approved:
                await ws.close(1008, "origin not allowed")
                return
            remember_origin(origin)
        self.clients.add(ws)
        print("Dashboard connected")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                cmd = msg.get("cmd")
                if cmd in ("start", "settings"):
                    self.engine.configure(msg.get("settings") or {})
                if cmd == "start":
                    self.start()
                elif cmd == "stop":
                    self.stop()
        finally:
            self.clients.discard(ws)
            if not self.clients:
                self.stop()   # dashboard gone: never keep controlling the laptop
            print("Dashboard disconnected")

    async def serve(self, port):
        self.loop = asyncio.get_running_loop()
        async with websockets.serve(self.handler, "127.0.0.1", port):
            print(f"AirControl agent listening on ws://127.0.0.1:{port}")
            print("Open the dashboard and press Start sensing. Emergency stop: Ctrl+Alt+Q")
            await asyncio.Future()


def serve_dashboard(web_port, open_browser):
    """Serve the dashboard from this laptop so it can reach the agent (no deploy needed)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.path = "/aircontrol-ai.html"
            super().do_GET()

    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", web_port), functools.partial(Handler, directory=base))
    except OSError as e:
        print(f"Could not serve dashboard on port {web_port}: {e}")
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{web_port}/"
    print("Dashboard:", url)
    if open_browser:
        webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allow", default="", help="extra website domain allowed to control this agent, e.g. mysite.netlify.app")
    ap.add_argument("--web-port", type=int, default=8766)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--preview", action="store_true", help="show a camera window with the hand skeleton")
    a = ap.parse_args()
    if a.allow:
        os.environ["AIRCONTROL_ORIGINS"] = (os.environ.get("AIRCONTROL_ORIGINS", "") + "," + a.allow).strip(",")
    try:
        ensure_model()
    except Exception as e:
        print("WARNING:", e)
    agent = Agent(a.camera, a.preview)
    serve_dashboard(a.web_port, not a.no_browser)
    GlobalHotKeys({"<ctrl>+<alt>+q": agent.stop}).start()
    try:
        asyncio.run(agent.serve(a.port))
    except KeyboardInterrupt:
        agent.stop()


if __name__ == "__main__":
    main()
