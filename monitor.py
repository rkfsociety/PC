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
from PIL import Image, ImageDraw
import pystray

try:
    import GPUtil
    HAS_GPUTIL = True
except Exception:
    HAS_GPUTIL = False

# ========= Конфиг =========
UPDATE_MS  = 1000
WIDTH      = 300
ALPHA      = 0.94
PAD        = 14
BG         = "#12121a"
BORDER     = "#2d2d3f"
FG         = "#ececf4"
MUTED      = "#7a7a92"
ACCENT     = "#7b8cff"
ACCENT_OK  = "#34d399"
ACCENT_WARN= "#fbbf24"
ACCENT_HOT = "#f87171"
NET_UP     = "#60a5fa"
NET_DOWN   = "#34d399"
BAR_BG     = "#252532"
BAR_H      = 6
CORE_H     = 22
SHELL_R    = 12

FONT_TITLE = ("Segoe UI", 11, "bold")
FONT_SEC   = ("Segoe UI", 8, "bold")
FONT_LBL   = ("Segoe UI", 9)
FONT_VAL   = ("Segoe UI", 9, "bold")
FONT_SMALL = ("Segoe UI", 8)

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

def load_window_pos():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, POS_KEY, 0, winreg.KEY_READ) as key:
            x_raw = winreg.QueryValueEx(key, "x")[0]
            y_raw = winreg.QueryValueEx(key, "y")[0]
            if isinstance(x_raw, str):
                return int(x_raw), int(y_raw)
            return _signed_dword(x_raw), _signed_dword(y_raw)
    except OSError:
        return None

def save_window_pos(x, y):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, POS_KEY) as key:
        winreg.SetValueEx(key, "x", 0, winreg.REG_SZ, str(int(x)))
        winreg.SetValueEx(key, "y", 0, winreg.REG_SZ, str(int(y)))

def bar_color(pct):
    if pct < 60:
        return ACCENT_OK
    if pct < 85:
        return ACCENT_WARN
    return ACCENT_HOT

def _round_rect(c, x1, y1, x2, y2, r, **kw):
    r = max(1, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    fill = kw.get("fill", "")
    outline = kw.get("outline", "")
    width = kw.get("width", 0)
    style = dict(fill=fill, outline=outline, width=width)
    ids = [
        c.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90, style="pieslice", **style),
        c.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90, style="pieslice", **style),
        c.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90, style="pieslice", **style),
        c.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, style="pieslice", **style),
        c.create_rectangle(x1 + r, y1, x2 - r, y2, **style),
        c.create_rectangle(x1, y1 + r, x2, y2 - r, **style),
    ]
    return ids

def _draw_shell(c, w, h):
    for i in _round_rect(c, 0, 0, w, h, SHELL_R, fill=BG, outline=BORDER, width=1):
        c.tag_lower(i)
    c.create_line(PAD, 1, w - PAD, 1, fill=ACCENT, width=2, capstyle=tk.ROUND)

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
    d.rounded_rectangle([10, 10, 54, 54], radius=10, fill="#1a1a26", outline=BORDER, width=2)
    d.rounded_rectangle([18, 36, 26, 48], radius=2, fill=ACCENT_OK)
    d.rounded_rectangle([28, 28, 36, 48], radius=2, fill=ACCENT_WARN)
    d.rounded_rectangle([38, 18, 46, 48], radius=2, fill=ACCENT_HOT)
    return img

# ========= Строитель статичного макета =========
# Макет строится один раз; при обновлении меняются только coords/fill текстов и баров.

class Row:
    """Строка метрики: подпись, значение, скруглённый прогресс-бар."""
    def __init__(self, c, x0, y, bar_w, label_text):
        self.c = c
        self.bar_w = bar_w
        self.x0 = x0
        self.bar_y = y + 17
        self.bar_h = BAR_H

        self.lbl_id = c.create_text(
            x0, y, text=label_text, anchor="nw", font=FONT_LBL, fill=MUTED)
        self.val_id = c.create_text(
            x0 + bar_w, y, text="", anchor="ne", font=FONT_VAL, fill=FG)
        for i in _round_rect(
            c, x0, self.bar_y, x0 + bar_w, self.bar_y + self.bar_h,
            self.bar_h // 2, fill=BAR_BG, outline=""):
            pass
        self.bar_id = c.create_rectangle(
            x0, self.bar_y, x0 + 2, self.bar_y + self.bar_h,
            fill=ACCENT_OK, outline="")

    def update(self, val_text, pct):
        fill = max(self.bar_h, int(self.bar_w * pct / 100))
        clr = bar_color(pct)
        self.c.itemconfig(self.val_id, text=val_text, fill=clr if pct >= 55 else FG)
        self.c.itemconfig(self.bar_id, fill=clr)
        self.c.coords(
            self.bar_id,
            self.x0, self.bar_y,
            self.x0 + fill, self.bar_y + self.bar_h)

    @property
    def bottom(self):
        return self.bar_y + self.bar_h

class MonitorApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("PC Monitor")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        # Прячем пока не разместим
        self.root.geometry("1x1+-9999+-9999")
        self.root.update_idletasks()

        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0,
                                width=WIDTH, height=300)
        self.canvas.pack()

        # hwnd — получаем сразу, пока заголовок ещё есть
        self._hwnd = win32gui.FindWindow(None, "PC Monitor")
        self._move_mode = False
        self._drag_x    = 0
        self._drag_y    = 0
        self._move_border = None
        self._move_hint   = None
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
        self._move_border = self.canvas.create_rectangle(
            2, 2, WIDTH - 2, h - 2, outline=ACCENT, width=2)
        self._move_hint = self.canvas.create_text(
            WIDTH // 2, h - PAD, text="перетащите", anchor="s",
            font=FONT_SMALL, fill=ACCENT)
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
            hwnd, 0, int(ALPHA * 255), win32con.LWA_ALPHA)
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
        c  = self.canvas
        x0 = PAD
        y  = PAD
        bw = WIDTH - PAD * 2

        c.create_text(x0, y, text="PC Monitor", anchor="nw", font=FONT_TITLE, fill=FG)
        c.create_oval(WIDTH - PAD - 7, y + 5, WIDTH - PAD, y + 12,
                      fill=ACCENT_OK, outline="")
        y += 24
        c.create_line(x0, y, x0 + bw, y, fill=BORDER)
        y += 12

        def sec(title):
            nonlocal y
            c.create_text(x0, y, text=title.upper(), anchor="nw",
                          font=FONT_SEC, fill=MUTED)
            y += 16

        def row(label):
            nonlocal y
            r = Row(c, x0, y, bw, label)
            y = r.bottom + 10
            return r

        sec("Процессор")
        self._r_cpu = row("Нагрузка")

        with self._lock:
            ncores = max(len(self._cores), psutil.cpu_count())
        ncols = min(ncores, 16)
        gap = 3
        cw = max(2, (bw - (ncols - 1) * gap) // ncols)
        self._core_bars = []
        for i in range(ncols):
            cx = x0 + i * (cw + gap)
            c.create_rectangle(cx, y, cx + cw, y + CORE_H, fill=BAR_BG, outline="")
            fid = c.create_rectangle(cx, y + CORE_H - 2, cx + cw, y + CORE_H,
                                     fill=ACCENT_OK, outline="")
            self._core_bars.append((fid, CORE_H, cx, y, cw))
        y += CORE_H + 12

        sec("Память")
        self._r_ram = row("RAM")

        self._gpu_y_start = y
        sec("Видеокарта")
        self._r_gpu_load = row("Нагрузка")
        self._r_gpu_vram = row("VRAM")
        self._txt_gpu_temp = c.create_text(x0, y, text="", anchor="nw",
                                           font=FONT_SMALL, fill=ACCENT_OK)
        self._gpu_temp_y = y
        y += 14
        self._gpu_y_end = y

        sec("Сеть")
        self._txt_net_up = c.create_text(
            x0, y, text="↑ 0.00 МБ/с", anchor="nw", font=FONT_LBL, fill=NET_UP)
        self._txt_net_down = c.create_text(
            x0 + bw, y, text="↓ 0.00 МБ/с", anchor="ne", font=FONT_LBL, fill=NET_DOWN)
        y += 18

        sec("Диски")
        self._disk_rows = {}
        with self._lock:
            drives = sorted(self._disk.keys())
        for drv in drives[:4]:
            self._disk_rows[drv] = row(drv)

        total_h = y + PAD
        self.canvas.config(height=total_h)
        _draw_shell(c, WIDTH, total_h)

        pos = load_window_pos()
        if pos:
            wx, wy = pos
        else:
            mx, my, mw, mh = get_primary_monitor()
            wx = mx + (mw - WIDTH) // 2
            wy = my + (mh - total_h) // 2
        self.root.geometry(f"{WIDTH}x{total_h}+{wx}+{wy}")
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
            cores = list(self._cores)
            ram   = self._ram
            net   = self._net
            gpu   = self._gpu
            disk  = dict(self._disk)

        self._r_cpu.update(f"{cpu:.0f}%", cpu)

        for i, (fid, bh, cx, cy, cw) in enumerate(self._core_bars):
            cp = cores[i] if i < len(cores) else 0
            fh = max(2, int(bh * cp / 100))
            c.coords(fid, cx, cy + bh - fh, cx + cw, cy + bh)
            c.itemconfig(fid, fill=bar_color(cp))

        used, total, pct = ram
        self._r_ram.update(f"{used:.1f} / {total:.0f} ГБ", pct)

        if gpu:
            g_pct, g_mem, g_temp = gpu
            self._r_gpu_load.update(f"{g_pct:.0f}%", g_pct)
            self._r_gpu_vram.update(f"{g_mem:.0f}%", g_mem)
            t_clr = ACCENT_OK if g_temp < 75 else (ACCENT_WARN if g_temp < 90 else ACCENT_HOT)
            c.itemconfig(self._txt_gpu_temp,
                         text=f"Темп: {g_temp:.0f}°C", fill=t_clr)
        else:
            self._r_gpu_load.update("—", 0)
            self._r_gpu_vram.update("—", 0)
            c.itemconfig(self._txt_gpu_temp, text="")

        s, r = net
        c.itemconfig(self._txt_net_up,   text=f"↑ {s:.2f} МБ/с")
        c.itemconfig(self._txt_net_down,  text=f"↓ {r:.2f} МБ/с")

        for drv, row in self._disk_rows.items():
            if drv in disk:
                used, total, pct = disk[drv]
                row.update(f"{used:.0f}/{total:.0f}ГБ", pct)

        self._ensure_topmost()
        self.root.after(UPDATE_MS, self._update)

    def stop(self):
        try:
            self._save_position()
        except Exception:
            pass
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
