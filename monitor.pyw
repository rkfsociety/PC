import subprocess
import subprocess as _sp

# Все дочерние процессы (nvidia-smi и др.) запускаем без консоли
_orig_popen = _sp.Popen.__init__
def _popen_no_console(self, *args, **kwargs):
    if 'startupinfo' not in kwargs:
        si = _sp.STARTUPINFO()
        si.dwFlags |= _sp.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs['startupinfo'] = si
    kwargs.setdefault('creationflags', 0)
    kwargs['creationflags'] |= 0x08000000  # CREATE_NO_WINDOW
    _orig_popen(self, *args, **kwargs)
_sp.Popen.__init__ = _popen_no_console

import os
import sys
import tkinter as tk
import psutil
import threading
import time
import ctypes
import winreg
import win32api
import win32con
import win32gui
from PIL import Image, ImageDraw, ImageTk
import pystray

try:
    import GPUtil
    HAS_GPUTIL = True
except Exception:
    HAS_GPUTIL = False

# ========= Конфиг / HUD-тема =========
UPDATE_MS  = 1000
WIDTH      = 460
PAD        = 12
CHAMFER    = 10
COL_GAP    = 10
TRANSPARENT = "#010001"
BG         = "#0a0f14"
BG_PANEL   = "#0d1520"
BAR_BG     = "#0a1a22"
GLASS_BG_ALPHA = 230
PANEL_FILL     = (14, 36, 56, 255)
BAR_STIPPLE    = "gray50"
CYAN       = "#00e5ff"
CYAN_DIM   = "#005f6b"
GREEN      = "#39ff14"
GREEN_DIM  = "#145208"
ORANGE     = "#ff8c00"
ORANGE_DIM = "#6b3a00"
RED        = "#ff4400"
WHITE      = "#e8f4ff"
COLORKEY_REF = 0x00010001
BAR_H      = 9
SEGMENTS   = 22
DRIVE_H    = 30

FONT_TITLE = ("Bahnschrift SemiBold", 12, "bold")
FONT_LABEL = ("Bahnschrift SemiBold", 8, "bold")
FONT_VALUE = ("Bahnschrift SemiBold", 9, "bold")
FONT_SMALL = ("Bahnschrift SemiBold", 8)
FONT_TEMP  = ("Bahnschrift SemiBold", 34, "bold")
FONT_TLBL  = ("Bahnschrift SemiBold", 9, "bold")
FONT_NET   = ("Bahnschrift SemiBold", 9, "bold")

AUTOSTART_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "PCMonitor"
POS_KEY        = r"Software\PCMonitor"
MUTEX_NAME     = "Global\\PCMonitor_SingleInstance"
_instance_mutex  = None

def acquire_single_instance():
    global _instance_mutex
    _instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.kernel32.CloseHandle(_instance_mutex)
        _instance_mutex = None
        return False
    return True

def release_single_instance():
    global _instance_mutex
    if _instance_mutex:
        ctypes.windll.kernel32.CloseHandle(_instance_mutex)
        _instance_mutex = None

def _launch_target():
    if getattr(sys, "frozen", False):
        return sys.executable, []
    base = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(base, "monitor.pyw")
    if not os.path.isfile(script):
        script = os.path.abspath(__file__)
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable
    return pythonw, [script]

def _autostart_command():
    exe, args = _launch_target()
    parts = [f'"{exe}"'] + [f'"{a}"' for a in args]
    return " ".join(parts)

def spawn_restart():
    release_single_instance()
    exe, args = _launch_target()
    subprocess.Popen([exe, *args])

def is_autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
            return True
    except OSError:
        return False

def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass

def _signed_dword(value):
    value = int(value)
    if value > 0x7FFFFFFF:
        value -= 0x100000000
    return value

def _valid_window_pos(x, y):
    return -500 <= int(x) <= 20000 and -500 <= int(y) <= 20000

def load_window_pos():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, POS_KEY, 0, winreg.KEY_READ) as key:
            x_raw = winreg.QueryValueEx(key, "x")[0]
            y_raw = winreg.QueryValueEx(key, "y")[0]
            if isinstance(x_raw, str):
                x, y = int(x_raw), int(y_raw)
            else:
                x, y = _signed_dword(x_raw), _signed_dword(y_raw)
            if not _valid_window_pos(x, y):
                return None
            return x, y
    except OSError:
        return None

def save_window_pos(x, y):
    if not _valid_window_pos(x, y):
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, POS_KEY) as key:
        winreg.SetValueEx(key, "x", 0, winreg.REG_SZ, str(int(x)))
        winreg.SetValueEx(key, "y", 0, winreg.REG_SZ, str(int(y)))
        winreg.SetValueEx(key, "placed", 0, winreg.REG_SZ, "1")

def metric_color(pct):
    if pct < 60:
        return GREEN
    if pct < 85:
        return ORANGE
    return RED

def _chamfer_points(x1, y1, x2, y2, cut):
    cut = max(2, min(cut, (x2 - x1) // 3, (y2 - y1) // 3))
    return [
        x1 + cut, y1,
        x2 - cut, y1,
        x2, y1 + cut,
        x2, y2 - cut,
        x2 - cut, y2,
        x1 + cut, y2,
        x1, y2 - cut,
        x1, y1 + cut,
    ]

def _chamfer_poly(x1, y1, x2, y2, cut):
    pts = _chamfer_points(x1, y1, x2, y2, cut)
    return [(pts[i], pts[i + 1]) for i in range(0, len(pts), 2)]

def _create_glass_image(w, h, net_y, net_h):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    shell = _chamfer_poly(1, 1, w - 1, h - 1, CHAMFER)
    draw.polygon(shell, fill=(*PANEL_FILL[:3], GLASS_BG_ALPHA))
    draw.polygon(shell, outline=(0, 229, 255, 255))
    glow = _chamfer_poly(0, 0, w, h, CHAMFER + 1)
    draw.polygon(glow, outline=(0, 122, 139, 120))
    net = _chamfer_poly(PAD, net_y, w - PAD, net_y + net_h, 6)
    draw.polygon(net, fill=PANEL_FILL)
    draw.polygon(net, outline=(0, 229, 255, 255))
    return img

def _draw_glass_stipple(c, w, h, net_y, net_h):
    ids = []
    shell = _chamfer_points(1, 1, w - 1, h - 1, CHAMFER)
    ids.append(c.create_polygon(shell, fill=BG, outline=CYAN, width=2))
    net = _chamfer_points(PAD, net_y, w - PAD, net_y + net_h, 6)
    ids.append(c.create_polygon(net, fill=BG, outline=CYAN, width=1))
    for i in ids:
        c.tag_lower(i)

def _place_glass_bg(c, w, h, net_y, net_h):
    try:
        photo = ImageTk.PhotoImage(_create_glass_image(w, h, net_y, net_h))
        bg_id = c.create_image(0, 0, anchor="nw", image=photo)
        c.tag_lower(bg_id)
        return photo
    except Exception:
        _draw_glass_stipple(c, w, h, net_y, net_h)
        return None

def _draw_chamfer(c, x1, y1, x2, y2, cut=CHAMFER, fill="", outline=CYAN, width=1, glow=False):
    ids = []
    if glow and outline:
        pts = _chamfer_points(x1, y1, x2, y2, cut)
        ids.append(c.create_polygon(pts, fill="", outline=CYAN_DIM, width=2))
    pts = _chamfer_points(x1, y1, x2, y2, cut)
    ids.append(c.create_polygon(pts, fill=fill, outline=outline, width=width))
    return ids

def _glow_text(c, x, y, text, font, color, anchor="nw"):
    dim = CYAN_DIM if color == CYAN else (ORANGE_DIM if color == ORANGE else GREEN_DIM)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1)):
        c.create_text(x + dx, y + dy, text=text, font=font, fill=dim, anchor=anchor)
    return c.create_text(x, y, text=text, font=font, fill=color, anchor=anchor)

def get_cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for entries in temps.values():
                if entries:
                    return entries[0].current
    except Exception:
        pass
    return None

def get_primary_monitor():
    for hMon, _, _ in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(hMon)
        if info["Flags"] & win32con.MONITORINFOF_PRIMARY:
            mx, my, mr, mb = info["Monitor"]
            return mx, my, mr - mx, mb - my
    hMon = win32api.EnumDisplayMonitors()[0][0]
    info = win32api.GetMonitorInfo(hMon)
    mx, my, mr, mb = info["Monitor"]
    return mx, my, mr - mx, mb - my

def make_tray_icon():
    img = Image.new("RGB", (64, 64), BG)
    d = ImageDraw.Draw(img)
    pts = [12, 8, 52, 8, 56, 12, 56, 52, 52, 56, 12, 56, 8, 52, 8, 12]
    d.polygon(pts, fill=BG_PANEL, outline=CYAN)
    d.rectangle([18, 38, 24, 48], fill=GREEN)
    d.rectangle([28, 30, 34, 48], fill=ORANGE)
    d.rectangle([38, 22, 44, 48], fill=CYAN)
    return img

# ========= HUD-виджеты =========

class HudMetric:
    def __init__(self, c, x, y, w, label):
        self.c = c
        self.x = x
        self.w = w
        self.bar_y = y + 15
        self.lbl_id = _glow_text(c, x, y, label, FONT_LABEL, CYAN, "nw")
        self.val_id = _glow_text(c, x + w, y, "", FONT_VALUE, GREEN, "ne")
        c.create_rectangle(x, self.bar_y, x + w, self.bar_y + BAR_H,
                           outline=CYAN, fill=BAR_BG, width=1, stipple=BAR_STIPPLE)
        self.fill_id = c.create_rectangle(
            x + 1, self.bar_y + 1, x + 2, self.bar_y + BAR_H - 1,
            fill=GREEN, outline="")

    def update(self, val_text, pct):
        clr = metric_color(pct)
        self.c.itemconfig(self.val_id, text=val_text, fill=clr)
        fill = max(2, int((self.w - 2) * pct / 100))
        self.c.itemconfig(self.fill_id, fill=clr)
        self.c.coords(self.fill_id,
                      self.x + 1, self.bar_y + 1,
                      self.x + 1 + fill, self.bar_y + BAR_H - 1)

    @property
    def bottom(self):
        return self.bar_y + BAR_H

class SegmentedBar:
    def __init__(self, c, x, y, w, n=SEGMENTS):
        self.c = c
        self.y = y
        self.h = BAR_H
        self.segs = []
        gap = 2
        sw = max(2, (w - gap * (n - 1)) // n)
        for i in range(n):
            sx = x + i * (sw + gap)
            c.create_rectangle(sx, y, sx + sw, y + self.h, outline=CYAN, fill=BAR_BG,
                               width=1, stipple=BAR_STIPPLE)
            fid = c.create_rectangle(sx + 1, y + 1, sx + sw - 1, y + self.h - 1,
                                       fill=GREEN, outline="")
            self.segs.append((fid, sx, sw))

    def update(self, pct):
        lit = int(len(self.segs) * pct / 100 + 0.5)
        clr = metric_color(pct)
        for i, (fid, sx, sw) in enumerate(self.segs):
            if i < lit:
                self.c.coords(fid, sx + 1, self.y + 1, sx + sw - 1, self.y + self.h - 1)
                self.c.itemconfig(fid, fill=clr, state="normal")
            else:
                self.c.itemconfig(fid, state="hidden")

    @property
    def bottom(self):
        return self.y + self.h

class DriveRow:
    def __init__(self, c, x, y, w, name):
        self.c = c
        self.x = x
        self.y = y
        self.w = w
        self.pct = 0
        self.frame = _draw_chamfer(c, x, y, x + w, y + DRIVE_H, 4,
                                   fill="", outline=CYAN, width=1)
        self.lbl_id = c.create_text(x + 6, y + 4, text=f"{name} DRIVE",
                                    font=FONT_SMALL, fill=CYAN, anchor="nw")
        self.val_id = c.create_text(x + w - 6, y + 4, text="", font=FONT_VALUE,
                                   fill=GREEN, anchor="ne")
        self.fill_id = c.create_rectangle(
            x + 6, y + 18, x + 7, y + DRIVE_H - 5, fill=GREEN, outline="")

    def set_hot(self, hot):
        outline = ORANGE if hot else CYAN
        for fid in self.frame:
            self.c.itemconfig(fid, outline=outline)

    def update(self, pct):
        self.pct = pct
        clr = metric_color(pct)
        self.c.itemconfig(self.val_id, text=f"{pct:.0f}%", fill=clr)
        fill = max(2, int((self.w - 12) * pct / 100))
        self.c.itemconfig(self.fill_id, fill=clr)
        self.c.coords(self.fill_id,
                      self.x + 6, self.y + 18,
                      self.x + 6 + fill, self.y + DRIVE_H - 5)

    @property
    def bottom(self):
        return self.y + DRIVE_H

class MonitorApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("PC Monitor")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=TRANSPARENT)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        # Прячем пока не разместим
        self.root.geometry("1x1+-9999+-9999")
        self.root.update_idletasks()

        self.canvas = tk.Canvas(self.root, bg=TRANSPARENT, highlightthickness=0,
                                width=WIDTH, height=400)
        self.canvas.pack()

        self._glass_photo = None

        # hwnd — получаем сразу, пока заголовок ещё есть
        self._hwnd = win32gui.FindWindow(None, "PC Monitor")
        self._move_mode = False
        self._drag_x    = 0
        self._drag_y    = 0
        self._move_border = None
        self._move_hint   = None
        self._layout_ready = False
        self._apply_clickthrough()

        # Метрики
        self._lock      = threading.Lock()
        self._cpu_pct   = 0.0
        self._cores     = []
        self._ram       = (0.0, 0.0, 0.0)
        self._net       = (0.0, 0.0)
        self._gpu       = None
        self._disk      = {}
        self._net_prev  = psutil.net_io_counters()
        self._net_time  = time.time()
        self._running   = True

        threading.Thread(target=self._metrics_loop, daemon=True).start()

        # Строим макет через 700 мс (дать метрикам первый цикл)
        self.root.after(700, self._build_layout)

    def is_move_mode(self):
        return self._move_mode

    def set_move_mode(self, enabled):
        def apply():
            if self._move_mode == enabled:
                return
            self._move_mode = enabled
            self._apply_move_mode()
        self.root.after(0, apply)

    def _show_move_chrome(self):
        self._hide_move_chrome()
        h = self.canvas.winfo_height()
        self._move_border = self.canvas.create_polygon(
            _chamfer_points(2, 2, WIDTH - 2, h - 2, CHAMFER),
            fill="", outline=CYAN, width=2)
        self._move_hint = self.canvas.create_text(
            WIDTH // 2, h - PAD, text="DRAG TO MOVE", anchor="s",
            font=FONT_SMALL, fill=CYAN)
        self.canvas.tag_raise(self._move_border)
        self.canvas.tag_raise(self._move_hint)

    def _hide_move_chrome(self):
        for item in (self._move_border, self._move_hint):
            if item is not None:
                self.canvas.delete(item)
        self._move_border = None
        self._move_hint = None
        if self._hwnd:
            win32gui.InvalidateRect(self._hwnd, None, True)
            win32gui.UpdateWindow(self._hwnd)

    def _ensure_topmost(self):
        hwnd = self._hwnd
        if not hwnd:
            return
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOPMOST,
            0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )

    def _apply_window_style(self):
        hwnd = self._hwnd
        if not hwnd:
            return
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        style |= win32con.WS_EX_LAYERED | win32con.WS_EX_TOPMOST
        if self._move_mode:
            style &= ~win32con.WS_EX_TRANSPARENT
        else:
            style |= win32con.WS_EX_TRANSPARENT
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)
        ctypes.windll.user32.SetLayeredWindowAttributes(
            hwnd, COLORKEY_REF, 0, win32con.LWA_COLORKEY)
        self._ensure_topmost()

    def _apply_move_mode(self):
        if self._move_mode:
            self._apply_window_style()
            self.canvas.configure(cursor="fleur")
            self.canvas.bind("<ButtonPress-1>", self._drag_start)
            self.canvas.bind("<B1-Motion>", self._drag_move)
            self.canvas.bind("<ButtonRelease-1>", self._drag_stop)
            self._show_move_chrome()
        else:
            self.canvas.configure(cursor="")
            self.canvas.unbind("<ButtonPress-1>")
            self.canvas.unbind("<B1-Motion>")
            self.canvas.unbind("<ButtonRelease-1>")
            self._hide_move_chrome()
            self._save_position()
            self._apply_window_style()
            self.root.update_idletasks()

    def _drag_start(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _drag_move(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")
        self._ensure_topmost()

    def _drag_stop(self, _event):
        self._save_position()

    def _window_pos(self):
        self.root.update_idletasks()
        hwnd = self._hwnd
        if hwnd:
            try:
                left, top, _, _ = win32gui.GetWindowRect(hwnd)
                return left, top
            except Exception:
                pass
        return self.root.winfo_rootx(), self.root.winfo_rooty()

    def _save_position(self):
        if not self._layout_ready:
            return
        save_window_pos(*self._window_pos())

    def _apply_clickthrough(self):
        self._apply_window_style()

    # ---- метрики ----
    def _metrics_loop(self):
        while self._running:
            try:
                cpu   = psutil.cpu_percent(interval=None)
                cores = psutil.cpu_percent(percpu=True, interval=None)
                vm    = psutil.virtual_memory()

                now = time.time()
                net = psutil.net_io_counters()
                dt  = max(now - self._net_time, 0.001)
                send = (net.bytes_sent - self._net_prev.bytes_sent) / dt / 1e6
                recv = (net.bytes_recv - self._net_prev.bytes_recv) / dt / 1e6

                disks = {}
                for p in psutil.disk_partitions(all=False):
                    try:
                        u = psutil.disk_usage(p.mountpoint)
                        disks[p.device[:2]] = (u.used/1e9, u.total/1e9, u.percent)
                    except Exception:
                        pass

                gpu = None
                if HAS_GPUTIL:
                    try:
                        gpus = GPUtil.getGPUs()
                        if gpus:
                            g = gpus[0]
                            mp = (g.memoryUsed / g.memoryTotal * 100) if g.memoryTotal else 0
                            gpu = (g.load * 100, mp, g.temperature)
                    except Exception:
                        pass

                with self._lock:
                    self._cpu_pct  = cpu
                    self._cores    = cores
                    self._ram      = (vm.used/1e9, vm.total/1e9, vm.percent)
                    self._net      = (send, recv)
                    self._net_prev = net
                    self._net_time = now
                    self._disk     = disks
                    self._gpu      = gpu

            except Exception:
                pass
            time.sleep(UPDATE_MS / 1000)

    # ---- построение макета (один раз) ----
    def _build_layout(self):
        c = self.canvas
        col_w = (WIDTH - PAD * 2 - COL_GAP) // 2
        lx = PAD
        rx = PAD + col_w + COL_GAP
        y = PAD + 4

        _glow_text(c, WIDTH // 2, y, "SYSTEM MONITORING", FONT_TITLE, WHITE, "n")
        y += 28

        ly = y
        self._r_cpu = HudMetric(c, lx, ly, col_w, "CPU USAGE")
        ly = self._r_cpu.bottom + 10
        self._r_ram = HudMetric(c, lx, ly, col_w, "RAM USAGE")
        ly = self._r_ram.bottom + 10
        self._r_vram = HudMetric(c, lx, ly, col_w, "VRAM USAGE")
        ly = self._r_vram.bottom + 16
        _glow_text(c, lx, ly, "TEMPERATURE", FONT_TLBL, CYAN, "nw")
        ly += 18
        self._txt_temp = _glow_text(c, lx + col_w // 2, ly + 20, "—°C",
                                      FONT_TEMP, GREEN, "center")

        ry = y
        _glow_text(c, rx, ry, "GPU LOAD", FONT_LABEL, CYAN, "nw")
        self._gpu_val = _glow_text(c, rx + col_w, ry, "", FONT_VALUE, GREEN, "ne")
        ry += 15
        self._gpu_bar = SegmentedBar(c, rx, ry, col_w)
        ry = self._gpu_bar.bottom + 12

        self._disk_rows = {}
        with self._lock:
            drives = sorted(self._disk.keys())
        for drv in drives[:4]:
            name = drv.rstrip(":").upper()
            row = DriveRow(c, rx, ry, col_w, name)
            self._disk_rows[drv] = row
            ry = row.bottom + 6

        net_y = max(ly + 56, ry) + 8
        net_h = 34
        net_lbl_x = PAD + 10
        _glow_text(c, net_lbl_x, net_y + 8, "NETWORK:", FONT_NET, CYAN, "nw")
        self._txt_net_up = c.create_text(
            net_lbl_x + 78, net_y + 8, text="↑ UP 0.00 MB/s",
            anchor="nw", font=FONT_NET, fill=GREEN)
        self._txt_net_down = c.create_text(
            WIDTH - PAD - 10, net_y + 8, text="↓ DOWN 0.00 MB/s",
            anchor="ne", font=FONT_NET, fill=GREEN)

        total_h = net_y + net_h + PAD
        self.canvas.config(height=total_h)
        self._glass_photo = _place_glass_bg(c, WIDTH, total_h, net_y, net_h)

        pos = load_window_pos()
        if pos:
            wx, wy = pos
        else:
            mx, my, mw, mh = get_primary_monitor()
            wx = mx + (mw - WIDTH) // 2
            wy = my + (mh - total_h) // 2
        self.root.geometry(f"{WIDTH}x{total_h}+{wx}+{wy}")
        self.root.update_idletasks()
        self._layout_ready = True
        if pos is None:
            save_window_pos(wx, wy)
        self._apply_clickthrough()

        # Запускаем цикл обновления
        self.root.after(UPDATE_MS, self._update)

    # ---- обновление значений (каждую секунду, без delete) ----
    def _update(self):
        if not self._running:
            return
        c = self.canvas

        with self._lock:
            cpu   = self._cpu_pct
            ram   = self._ram
            net   = self._net
            gpu   = self._gpu
            disk  = dict(self._disk)

        self._r_cpu.update(f"{cpu:.0f}%", cpu)

        used, total, pct = ram
        self._r_ram.update(f"{used:.1f} / {total:.0f} GB", pct)

        if gpu:
            g_pct, g_mem, g_temp = gpu
            self._gpu_bar.update(g_pct)
            c.itemconfig(self._gpu_val, text=f"{g_pct:.0f}%", fill=metric_color(g_pct))
            self._r_vram.update(f"{g_mem:.0f}%", g_mem)
            temp = g_temp
        else:
            self._gpu_bar.update(0)
            c.itemconfig(self._gpu_val, text="—", fill=GREEN)
            self._r_vram.update("—", 0)
            temp = get_cpu_temp()

        if temp is not None:
            t_clr = metric_color(min(temp, 100))
            c.itemconfig(self._txt_temp, text=f"{temp:.0f}°C", fill=t_clr)
        else:
            c.itemconfig(self._txt_temp, text="—°C", fill=GREEN)

        s, r = net
        c.itemconfig(self._txt_net_up, text=f"↑ UP {s:.2f} MB/s")
        c.itemconfig(self._txt_net_down, text=f"↓ DOWN {r:.2f} MB/s")

        hot_drv = None
        hot_pct = -1
        for drv, row in self._disk_rows.items():
            if drv in disk:
                pct = disk[drv][2]
                row.update(pct)
                if pct > hot_pct:
                    hot_pct = pct
                    hot_drv = drv
        for drv, row in self._disk_rows.items():
            row.set_hot(drv == hot_drv and hot_pct >= 70)

        self._ensure_topmost()
        self.root.after(UPDATE_MS, self._update)

    def stop(self):
        self._running = False
        self.root.quit()

    def run(self):
        self.root.mainloop()

def run_tray(app):
    def on_autostart(icon, item):
        set_autostart(not is_autostart_enabled())

    def on_move_mode(icon, item):
        want = not app.is_move_mode()
        app.set_move_mode(want)

    def on_restart(icon, item):
        try:
            app._save_position()
        except Exception:
            pass
        spawn_restart()
        icon.stop()
        app.stop()

    def on_quit(icon, item):
        icon.stop()
        app.stop()

    icon = pystray.Icon(
        "pc_monitor",
        make_tray_icon(),
        "PC Monitor",
        menu=pystray.Menu(
            pystray.MenuItem(
                "Запускать с Windows",
                on_autostart,
                checked=lambda item: is_autostart_enabled(),
            ),
            pystray.MenuItem(
                "Перемещение",
                on_move_mode,
                checked=lambda item: app.is_move_mode(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Перезапуск", on_restart),
            pystray.MenuItem("Выход", on_quit),
        ),
    )
    icon.run()

if __name__ == "__main__":
    if not acquire_single_instance():
        sys.exit(0)
    app = MonitorApp()
    threading.Thread(target=run_tray, args=(app,), daemon=True).start()
    app.run()
