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
WIDTH      = 270
ALPHA      = 0.88
BG         = "#0d0d0d"
FG         = "#e0e0e0"
DIM        = "#606060"
ACCENT_OK  = "#29d97a"
ACCENT_WARN= "#f0c040"
ACCENT_HOT = "#f04040"
BAR_BG     = "#1e1e1e"
MARGIN     = 10
FONT_VAL   = ("Consolas", 9)
FONT_LBL   = ("Consolas", 8)
FONT_SEC   = ("Consolas", 8, "bold")

AUTOSTART_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "PCMonitor"

def _autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    base = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(base, "monitor.pyw")
    if not os.path.isfile(script):
        script = os.path.abspath(__file__)
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{script}"'

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

def bar_color(pct):
    if pct < 60:   return ACCENT_OK
    if pct < 85:   return ACCENT_WARN
    return ACCENT_HOT

def get_secondary_monitor():
    for hMon, _, _ in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(hMon)
        if not info["Flags"]:  # Flags==1 → главный
            mx, my, mr, mb = info["Monitor"]
            return (mx, my, mr - mx, mb - my)
    hMon = win32api.EnumDisplayMonitors()[0][0]
    info = win32api.GetMonitorInfo(hMon)
    mx, my, mr, mb = info["Monitor"]
    return (mx, my, mr - mx, mb - my)

def make_tray_icon():
    img = Image.new("RGB", (64, 64), "#1a1a1a")
    d = ImageDraw.Draw(img)
    d.rectangle([8,  40, 18, 56], fill=ACCENT_OK)
    d.rectangle([22, 28, 32, 56], fill=ACCENT_WARN)
    d.rectangle([36, 16, 46, 56], fill=ACCENT_HOT)
    return img

# ========= Строитель статичного макета =========
# Макет строится один раз; при обновлении меняются только coords/fill текстов и баров.

class Row:
    """Одна строка: label слева, value справа, бар под ними."""
    def __init__(self, c, x0, y, bar_w, bar_h, label_text):
        self.c = c
        self.bar_w = bar_w
        self.bar_h = bar_h
        self.x0 = x0
        self.bar_y = y + 13

        self.lbl_id = c.create_text(x0, y, text=label_text,
                                    anchor="nw", font=FONT_LBL, fill=DIM)
        self.val_id = c.create_text(WIDTH - x0, y, text="",
                                    anchor="ne", font=FONT_LBL, fill=FG)
        # фон бара (статичный)
        c.create_rectangle(x0, self.bar_y, x0 + bar_w, self.bar_y + bar_h,
                           fill=BAR_BG, outline="")
        # заполнение бара (динамичный)
        self.bar_id = c.create_rectangle(x0, self.bar_y, x0 + 2, self.bar_y + bar_h,
                                         fill=ACCENT_OK, outline="")

    def update(self, val_text, pct):
        fill = max(2, int(self.bar_w * pct / 100))
        self.c.itemconfig(self.val_id, text=val_text)
        self.c.itemconfig(self.bar_id, fill=bar_color(pct))
        self.c.coords(self.bar_id,
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

    def _apply_clickthrough(self):
        hwnd = self._hwnd
        if not hwnd:
            return
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        style |= win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)
        ctypes.windll.user32.SetLayeredWindowAttributes(
            hwnd, 0, int(ALPHA * 255), win32con.LWA_ALPHA)

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
        x0 = 10
        y  = 8
        bw = WIDTH - x0 * 2
        bh = 6

        def sec(title):
            nonlocal y
            c.create_text(x0, y, text=title, anchor="nw",
                          font=FONT_SEC, fill="#777")
            y += 14

        def row(label):
            nonlocal y
            r = Row(c, x0, y, bw, bh, label)
            y = r.bottom + 8
            return r

        # CPU
        sec("CPU")
        self._r_cpu = row("Нагрузка")

        # мини-бары ядер
        with self._lock:
            ncores = max(len(self._cores), psutil.cpu_count())
        ncols = min(ncores, 16)
        cw = (bw - (ncols - 1) * 2) // ncols
        self._core_bars = []   # (bg_id, fill_id, bar_h, cx, cy)
        for i in range(ncols):
            cx = x0 + i * (cw + 2)
            c.create_rectangle(cx, y, cx + cw, y + bh, fill=BAR_BG, outline="")
            fid = c.create_rectangle(cx, y + bh - 1, cx + cw, y + bh,
                                     fill=ACCENT_OK, outline="")
            self._core_bars.append((fid, bh, cx, y))
        y += bh + 10

        # RAM
        sec("ПАМЯТЬ")
        self._r_ram = row("RAM")

        # GPU (секция всегда в макете; скрываем если нет GPU)
        self._gpu_y_start = y
        sec("GPU")
        self._r_gpu_load = row("Нагрузка")
        self._r_gpu_vram = row("VRAM")
        self._txt_gpu_temp = c.create_text(x0, y, text="", anchor="nw",
                                           font=FONT_LBL, fill=ACCENT_OK)
        self._gpu_temp_y = y
        y += 14
        self._gpu_y_end = y

        # Сеть
        sec("СЕТЬ")
        self._txt_net_up   = c.create_text(x0,        y, text="↑ 0.00 МБ/с",
                                           anchor="nw", font=FONT_LBL, fill=DIM)
        self._txt_net_down = c.create_text(WIDTH - x0, y, text="↓ 0.00 МБ/с",
                                           anchor="ne", font=FONT_LBL, fill=FG)
        y += 14

        # Диски (до 4)
        sec("ДИСКИ")
        self._disk_rows = {}
        with self._lock:
            drives = sorted(self._disk.keys())
        for drv in drives[:4]:
            self._disk_rows[drv] = row(drv)

        total_h = y + 6
        self.canvas.config(height=total_h)
        mx, my, mw, mh = get_secondary_monitor()
        wx = mx + mw - WIDTH - MARGIN
        wy = my + mh - total_h - MARGIN
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

        x0 = 10
        bw = WIDTH - x0 * 2
        for i, (fid, bh, cx, cy) in enumerate(self._core_bars):
            cp = cores[i] if i < len(cores) else 0
            fh = max(1, int(bh * cp / 100))
            c.coords(fid, cx, cy + bh - fh, cx + (bw // len(self._core_bars)), cy + bh)
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

        self.root.after(UPDATE_MS, self._update)

    def stop(self):
        self._running = False
        self.root.quit()

    def run(self):
        self.root.mainloop()

def run_tray(app):
    def on_autostart(icon, item):
        set_autostart(not is_autostart_enabled())

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
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", on_quit),
        ),
    )
    icon.run()

if __name__ == "__main__":
    app = MonitorApp()
    threading.Thread(target=run_tray, args=(app,), daemon=True).start()
    app.run()
