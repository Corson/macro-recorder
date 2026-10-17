"""
Macro Recorder / Auto Clicker (Windows)
---------------------------------------
Wymagania:  Python 3.9+   i   pip install pynput

Funkcje:
 - nagrywanie klikniec myszy (z pozycja i numerem monitora), przewijania i klawiszy
 - odtwarzanie z zadana predkoscia, liczba powtorzen lub bez limitu
 - globalne skroty F1-F12 (start/stop nagrywania, start/stop odtwarzania)
 - zapis / odczyt makra do pliku JSON

Uwaga: jesli chcesz sterowac oknami uruchomionymi jako administrator
(albo niektorymi grami), uruchom ten program jako administrator.
"""
import ctypes
import json
import threading
import time
from ctypes import wintypes

# DPI awareness musi byc ustawione przed utworzeniem okna i listenerow,
# inaczej wspolrzedne na monitorach ze skalowaniem beda przesuniete.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from pynput import keyboard, mouse

FKEYS = [f"F{i}" for i in range(1, 13)]
DEFAULT_HOTKEYS = {
    "rec_start": "F6",
    "rec_stop": "F7",
    "play_start": "F8",
    "play_stop": "F9",
}
HOTKEY_LABELS = {
    "rec_start": "Rozpocznij nagrywanie",
    "rec_stop": "Zatrzymaj nagrywanie",
    "play_start": "Rozpocznij odtwarzanie",
    "play_stop": "Zatrzymaj odtwarzanie",
}

user32 = ctypes.windll.user32


# ----------------------------------------------------------------- monitory
def get_monitors():
    """Zwraca liste (left, top, right, bottom) posortowana od lewej do prawej."""
    mons = []
    proc_type = ctypes.WINFUNCTYPE(
        ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )

    def cb(hmon, hdc, rect, lparam):
        r = rect.contents
        mons.append((r.left, r.top, r.right, r.bottom))
        return 1

    try:
        user32.EnumDisplayMonitors(0, 0, proc_type(cb), 0)
    except Exception:
        pass
    if not mons:
        mons.append((0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)))
    mons.sort(key=lambda m: (m[0], m[1]))
    return mons


def monitor_index(mons, x, y):
    for i, (l, t, r, b) in enumerate(mons):
        if l <= x < r and t <= y < b:
            return i
    # poza monitorami - wybierz najblizszy
    best, best_d = 0, None
    for i, (l, t, r, b) in enumerate(mons):
        dx = max(l - x, 0, x - r + 1)
        dy = max(t - y, 0, y - b + 1)
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


# ------------------------------------------------------------ serializacja
def key_to_dict(k):
    if isinstance(k, keyboard.Key):
        return {"s": k.name}
    return {"vk": getattr(k, "vk", None), "c": getattr(k, "char", None)}


def dict_to_key(d):
    if "s" in d:
        return getattr(keyboard.Key, d["s"])
    if d.get("vk") is not None:
        return keyboard.KeyCode.from_vk(d["vk"])
    return keyboard.KeyCode.from_char(d["c"])


def key_id(d):
    return json.dumps(d, sort_keys=True)


def is_fkey(k):
    return (isinstance(k, keyboard.Key) and k.name.startswith("f")
            and k.name[1:].isdigit())


# ------------------------------------------------------------------ aplikacja
class MacroApp:
    def __init__(self, root):
        self.root = root
        root.title("Macro Recorder / Auto Clicker")

        self.events = []
        self.monitors = get_monitors()
        self.recording = False
        self.playing = False
        self.rec_t0 = 0.0
        self.rec_lock = threading.Lock()
        self.rec_pressed = set()
        self.mouse_listener = None
        self.stop_play = threading.Event()
        self.play_thread = None
        self.win_rect = (0, 0, 0, 0)
        self.hotkeys = dict(DEFAULT_HOTKEYS)

        self.mouse_ctl = mouse.Controller()
        self.kb_ctl = keyboard.Controller()

        self._build_ui()
        self._fix_window_size()
        root.bind("<Configure>", self._on_configure)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.kb_listener = keyboard.Listener(on_press=self._on_key_press,
                                             on_release=self._on_key_release)
        self.kb_listener.daemon = True
        self.kb_listener.start()
        self._update_win_rect()
        self.set_status("Gotowy.")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        mon_txt = ", ".join(
            f"M{i + 1}: {r - l}x{b - t} @({l},{t})"
            for i, (l, t, r, b) in enumerate(self.monitors))
        ttk.Label(self.root, text=f"Wykryte monitory: {mon_txt}",
                  foreground="#555", wraplength=600).pack(anchor="w", **pad)

        # nagrywanie
        f_rec = ttk.LabelFrame(self.root, text="Nagrywanie")
        f_rec.pack(fill="x", **pad)
        self.btn_rec = ttk.Button(f_rec, text="● Rozpocznij nagrywanie",
                                  command=self.start_recording)
        self.btn_rec.pack(side="left", padx=6, pady=6)
        self.btn_rec_stop = ttk.Button(f_rec, text="■ Zatrzymaj nagrywanie",
                                       command=self.stop_recording,
                                       state="disabled")
        self.btn_rec_stop.pack(side="left", padx=6, pady=6)
        f_chk = ttk.Frame(f_rec)
        f_chk.pack(side="left", padx=10, pady=4)
        self.var_mouse_scroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(f_chk, text="Nagrywaj przewijanie",
                        variable=self.var_mouse_scroll).pack(anchor="w")
        self.var_keys = tk.BooleanVar(value=True)
        ttk.Checkbutton(f_chk, text="Nagrywaj klawisze",
                        variable=self.var_keys).pack(anchor="w")

        # odtwarzanie
        f_play = ttk.LabelFrame(self.root, text="Odtwarzanie")
        f_play.pack(fill="x", **pad)

        row1 = ttk.Frame(f_play)
        row1.pack(fill="x", padx=6, pady=4)
        ttk.Label(row1, text="Prędkość (x):").pack(side="left")
        self.var_speed = tk.StringVar(value="1.0")
        ttk.Spinbox(row1, from_=0.1, to=20, increment=0.1, width=6,
                    textvariable=self.var_speed).pack(side="left", padx=4)
        ttk.Label(row1, text="Powtórzenia:").pack(side="left", padx=(14, 0))
        self.var_reps = tk.StringVar(value="1")
        self.spin_reps = ttk.Spinbox(row1, from_=1, to=1000000, width=8,
                                     textvariable=self.var_reps)
        self.spin_reps.pack(side="left", padx=4)
        self.var_inf = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="Bez limitu", variable=self.var_inf,
                        command=self._toggle_inf).pack(side="left", padx=6)

        row2 = ttk.Frame(f_play)
        row2.pack(fill="x", padx=6, pady=4)
        ttk.Label(row2, text="Przerwa między powtórzeniami (s):").pack(side="left")
        self.var_gap = tk.StringVar(value="0.0")
        ttk.Spinbox(row2, from_=0, to=3600, increment=0.1, width=6,
                    textvariable=self.var_gap).pack(side="left", padx=4)
        ttk.Label(row2, text="Opóźnienie startu (s):").pack(side="left", padx=(14, 0))
        self.var_start_delay = tk.StringVar(value="1.0")
        ttk.Spinbox(row2, from_=0, to=600, increment=0.5, width=6,
                    textvariable=self.var_start_delay).pack(side="left", padx=4)

        row3 = ttk.Frame(f_play)
        row3.pack(fill="x", padx=6, pady=4)
        self.var_rel = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            row3,
            text="Zapamiętaj pozycje względem monitora",
            variable=self.var_rel).pack(side="left")

        row4 = ttk.Frame(f_play)
        row4.pack(fill="x", padx=6, pady=6)
        self.btn_play = ttk.Button(row4, text="▶ Rozpocznij odtwarzanie",
                                   command=self.start_playback)
        self.btn_play.pack(side="left", padx=2)
        self.btn_play_stop = ttk.Button(row4, text="■ Zatrzymaj odtwarzanie",
                                        command=self.stop_playback,
                                        state="disabled")
        self.btn_play_stop.pack(side="left", padx=6)

        # skroty
        f_hk = ttk.LabelFrame(self.root, text="Skróty klawiszowe (F1–F12)")
        f_hk.pack(fill="x", **pad)
        self.hk_vars = {}
        for i, (name, label) in enumerate(HOTKEY_LABELS.items()):
            ttk.Label(f_hk, text=label + ":").grid(
                row=i // 2, column=(i % 2) * 2, sticky="e", padx=6, pady=4)
            var = tk.StringVar(value=self.hotkeys[name])
            cb = ttk.Combobox(f_hk, values=FKEYS, textvariable=var,
                              width=5, state="readonly")
            cb.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=4)
            cb.bind("<<ComboboxSelected>>", lambda e: self.apply_hotkeys())
            self.hk_vars[name] = var

        # lista zdarzen
        f_list = ttk.LabelFrame(self.root, text="Nagrane zdarzenia")
        f_list.pack(fill="both", expand=True, **pad)
        cols = ("nr", "t", "typ", "szczegoly")
        self.tree = ttk.Treeview(f_list, columns=cols, show="headings", height=8)
        for c, txt, w in (("nr", "#", 45), ("t", "Czas [s]", 80),
                          ("typ", "Typ", 120), ("szczegoly", "Szczegóły", 340)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(f_list, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        f_btn = ttk.Frame(self.root)
        f_btn.pack(fill="x", **pad)
        ttk.Button(f_btn, text="Zapisz...", command=self.save_macro).pack(side="left")
        ttk.Button(f_btn, text="Wczytaj...", command=self.load_macro).pack(side="left", padx=6)
        ttk.Button(f_btn, text="Wyczyść", command=self.clear_macro).pack(side="left")

        self.status = tk.StringVar()
        ttk.Label(self.root, textvariable=self.status, relief="sunken",
                  anchor="w").pack(fill="x", side="bottom")

    def _fix_window_size(self):
        """Stały rozmiar okna, niezależny od monitora i jego skalowania DPI."""
        self.root.update_idletasks()
        self.fixed_w = max(self.root.winfo_reqwidth(), 640)
        self.fixed_h = self.root.winfo_reqheight()
        self._resizing = False
        self.root.geometry(f"{self.fixed_w}x{self.fixed_h}")
        self.root.resizable(False, False)

    def _on_configure(self, event):
        if event.widget is not self.root:
            return
        self._update_win_rect()
        # Windows potrafi zmienić rozmiar okna przy przeciągnięciu na monitor
        # o innym skalowaniu - przywracamy stały rozmiar.
        if ((event.width != self.fixed_w or event.height != self.fixed_h)
                and not self._resizing):
            self._resizing = True
            self.root.after(20, self._reapply_size)

    def _reapply_size(self):
        self.root.geometry(f"{self.fixed_w}x{self.fixed_h}")
        self._resizing = False

    def _toggle_inf(self):
        self.spin_reps.configure(state="disabled" if self.var_inf.get() else "normal")

    def set_status(self, text):
        self.status.set(" " + text)

    def _update_win_rect(self, event=None):
        try:
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            # lekko poszerzone o ramke okna
            self.win_rect = (x - 12, y - 40, x + w + 12, y + h + 12)
        except tk.TclError:
            pass

    def _point_in_window(self, x, y):
        l, t, r, b = self.win_rect
        return l <= x <= r and t <= y <= b

    # ---------------------------------------------------------- skroty
    def apply_hotkeys(self):
        new = {n: v.get() for n, v in self.hk_vars.items()}
        if len(set(new.values())) != len(new):
            messagebox.showwarning("Skróty", "Każda akcja musi mieć inny klawisz.")
            for n, v in self.hk_vars.items():
                v.set(self.hotkeys[n])
            return
        self.hotkeys = new
        self.set_status("Zmieniono skróty: " + ", ".join(
            f"{HOTKEY_LABELS[n]}={v}" for n, v in new.items()))

    def _hotkey_action(self, key):
        if not is_fkey(key):
            return None
        name = key.name.upper()
        for action, fk in self.hotkeys.items():
            if fk == name:
                return action
        return None

    def _on_key_press(self, key):
        action = self._hotkey_action(key)
        if action:
            fn = {"rec_start": self.start_recording,
                  "rec_stop": self.stop_recording,
                  "play_start": self.start_playback,
                  "play_stop": self.stop_playback}[action]
            self.root.after(0, fn)
            return
        if self.recording and self.var_keys.get():
            d = key_to_dict(key)
            kid = key_id(d)
            if kid in self.rec_pressed:      # auto-repeat
                return
            self.rec_pressed.add(kid)
            self._add_event({"type": "key_press", "key": d})

    def _on_key_release(self, key):
        if self._hotkey_action(key):
            return
        if self.recording and self.var_keys.get():
            d = key_to_dict(key)
            self.rec_pressed.discard(key_id(d))
            self._add_event({"type": "key_release", "key": d})

    # -------------------------------------------------------- nagrywanie
    def _add_event(self, ev):
        with self.rec_lock:
            ev["t"] = round(time.perf_counter() - self.rec_t0, 4)
            self.events.append(ev)

    def _mouse_pos_info(self, x, y):
        mi = monitor_index(self.monitors, x, y)
        l, t, _, _ = self.monitors[mi]
        return {"x": int(x), "y": int(y), "mon": mi, "rx": int(x - l), "ry": int(y - t)}

    def _on_click(self, x, y, button, pressed):
        if self._point_in_window(x, y):
            return
        ev = {"type": "mouse_press" if pressed else "mouse_release",
              "button": button.name}
        ev.update(self._mouse_pos_info(x, y))
        self._add_event(ev)

    def _on_scroll(self, x, y, dx, dy):
        if not self.var_mouse_scroll.get() or self._point_in_window(x, y):
            return
        ev = {"type": "scroll", "dx": dx, "dy": dy}
        ev.update(self._mouse_pos_info(x, y))
        self._add_event(ev)

    def start_recording(self):
        if self.recording or self.playing:
            return
        self.monitors = get_monitors()
        self.events = []
        self.rec_pressed.clear()
        self._refresh_tree()
        self._update_win_rect()
        self.rec_t0 = time.perf_counter()
        self.recording = True
        self.mouse_listener = mouse.Listener(on_click=self._on_click,
                                             on_scroll=self._on_scroll)
        self.mouse_listener.daemon = True
        self.mouse_listener.start()
        self.btn_rec.configure(state="disabled")
        self.btn_rec_stop.configure(state="normal")
        self.btn_play.configure(state="disabled")
        self._tick_recording()

    def _tick_recording(self):
        if self.recording:
            self.set_status(
                f"NAGRYWANIE... zdarzeń: {len(self.events)}   "
                f"(zakończ: {self.hotkeys['rec_stop']})")
            self.root.after(250, self._tick_recording)

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        if self.mouse_listener:
            self.mouse_listener.stop()
            self.mouse_listener = None
        with self.rec_lock:
            if self.events:
                t0 = self.events[0]["t"]       # usun poczatkowy bezczynny czas
                for ev in self.events:
                    ev["t"] = round(ev["t"] - t0, 4)
        self.btn_rec.configure(state="normal")
        self.btn_rec_stop.configure(state="disabled")
        self.btn_play.configure(state="normal")
        self._refresh_tree()
        self.set_status(f"Zakończono nagrywanie. Zdarzeń: {len(self.events)}")

    # ------------------------------------------------------- odtwarzanie
    def start_playback(self):
        if self.playing or self.recording:
            return
        if not self.events:
            self.set_status("Brak nagrania do odtworzenia.")
            return
        try:
            speed = max(0.05, float(self.var_speed.get().replace(",", ".")))
            gap = max(0.0, float(self.var_gap.get().replace(",", ".")))
            delay = max(0.0, float(self.var_start_delay.get().replace(",", ".")))
            reps = max(1, int(float(self.var_reps.get())))
        except ValueError:
            messagebox.showerror("Błąd", "Niepoprawne wartości parametrów odtwarzania.")
            return
        infinite = self.var_inf.get()
        use_rel = self.var_rel.get()

        self.monitors = get_monitors()
        self.playing = True
        self.stop_play.clear()
        self.btn_play.configure(state="disabled")
        self.btn_rec.configure(state="disabled")
        self.btn_play_stop.configure(state="normal")
        self.play_thread = threading.Thread(
            target=self._play_worker,
            args=(list(self.events), speed, reps, infinite, gap, delay, use_rel),
            daemon=True)
        self.play_thread.start()

    def stop_playback(self):
        if self.playing:
            self.stop_play.set()

    def _ui(self, fn, *a):
        self.root.after(0, lambda: fn(*a))

    def _play_worker(self, events, speed, reps, infinite, gap, delay, use_rel):
        held_keys, held_btns = [], set()
        stop = self.stop_play
        try:
            if delay > 0:
                steps = int(delay * 10)
                for i in range(steps):
                    if stop.wait(0.1):
                        return
                    if i % 10 == 0:
                        self._ui(self.set_status,
                                 f"Start za {delay - i / 10:.0f} s...")
            run = 0
            while not stop.is_set():
                run += 1
                total = "∞" if infinite else str(reps)
                self._ui(self.set_status,
                         f"ODTWARZANIE... powtórzenie {run}/{total}   "
                         f"(zatrzymaj: {self.hotkeys['play_stop']})")
                prev = 0.0
                for ev in events:
                    wait = (ev["t"] - prev) / speed
                    prev = ev["t"]
                    if wait > 0 and stop.wait(wait):
                        break
                    if stop.is_set():
                        break
                    self._exec_event(ev, use_rel, held_keys, held_btns)
                if stop.is_set():
                    break
                if not infinite and run >= reps:
                    break
                if gap > 0 and stop.wait(gap):
                    break
        finally:
            # zwolnij wszystko co zostalo wcisniete, zeby nic sie nie zablokowalo
            for k in held_keys:
                try:
                    self.kb_ctl.release(k)
                except Exception:
                    pass
            for b in held_btns:
                try:
                    self.mouse_ctl.release(getattr(mouse.Button, b))
                except Exception:
                    pass
            stopped = stop.is_set()
            self._ui(self._playback_done, stopped)

    def _move_to(self, ev, use_rel):
        x, y = ev["x"], ev["y"]
        if use_rel and ev.get("mon") is not None and ev["mon"] < len(self.monitors):
            l, t, _, _ = self.monitors[ev["mon"]]
            x, y = l + ev["rx"], t + ev["ry"]
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.003)

    def _exec_event(self, ev, use_rel, held_keys, held_btns):
        typ = ev["type"]
        try:
            if typ == "mouse_press":
                self._move_to(ev, use_rel)
                self.mouse_ctl.press(getattr(mouse.Button, ev["button"]))
                held_btns.add(ev["button"])
            elif typ == "mouse_release":
                self._move_to(ev, use_rel)
                self.mouse_ctl.release(getattr(mouse.Button, ev["button"]))
                held_btns.discard(ev["button"])
            elif typ == "scroll":
                self._move_to(ev, use_rel)
                self.mouse_ctl.scroll(ev["dx"], ev["dy"])
            elif typ == "key_press":
                k = dict_to_key(ev["key"])
                self.kb_ctl.press(k)
                held_keys.append(k)
            elif typ == "key_release":
                k = dict_to_key(ev["key"])
                self.kb_ctl.release(k)
                if k in held_keys:
                    held_keys.remove(k)
        except Exception as exc:                      # pojedynczy blad nie przerywa makra
            print("Blad zdarzenia:", ev, exc)

    def _playback_done(self, stopped):
        self.playing = False
        self.btn_play.configure(state="normal")
        self.btn_rec.configure(state="normal")
        self.btn_play_stop.configure(state="disabled")
        self.set_status("Odtwarzanie przerwane." if stopped
                        else "Odtwarzanie zakończone.")

    # ------------------------------------------------------ lista / plik
    def _describe(self, ev):
        t = ev["type"]
        if t in ("mouse_press", "mouse_release"):
            return (f"{'Wciśnięcie' if t == 'mouse_press' else 'Zwolnienie'} "
                    f"{ev['button']}", f"M{ev['mon'] + 1}  ({ev['rx']}, {ev['ry']})"
                    f"   abs ({ev['x']}, {ev['y']})")
        if t == "scroll":
            return ("Przewijanie", f"M{ev['mon'] + 1}  dx={ev['dx']} dy={ev['dy']}")
        k = ev["key"]
        name = k.get("s") or k.get("c") or f"vk={k.get('vk')}"
        return ("Klawisz ↓" if t == "key_press" else "Klawisz ↑", str(name))

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, ev in enumerate(self.events, 1):
            typ, det = self._describe(ev)
            self.tree.insert("", "end", values=(i, f"{ev['t']:.3f}", typ, det))
        if self.events:
            self.tree.yview_moveto(1)

    def clear_macro(self):
        if self.recording or self.playing:
            return
        self.events = []
        self._refresh_tree()
        self.set_status("Wyczyszczono nagranie.")

    def save_macro(self):
        if not self.events:
            self.set_status("Brak danych do zapisania.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("Makro JSON", "*.json")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"monitors": self.monitors, "events": self.events},
                          f, indent=1)
            self.set_status(f"Zapisano: {path}")

    def load_macro(self):
        if self.recording or self.playing:
            return
        path = filedialog.askopenfilename(filetypes=[("Makro JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.events = data["events"]
        except Exception as exc:
            messagebox.showerror("Błąd", f"Nie można wczytać pliku:\n{exc}")
            return
        self._refresh_tree()
        self.set_status(f"Wczytano {len(self.events)} zdarzeń z {path}")

    # -------------------------------------------------------------- koniec
    def on_close(self):
        self.stop_play.set()
        self.recording = False
        for lst in (self.mouse_listener, self.kb_listener):
            try:
                if lst:
                    lst.stop()
            except Exception:
                pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    MacroApp(root)
    root.mainloop()