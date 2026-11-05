#!/usr/bin/env python3
"""DQ250 gear editor - open a 02E/DQ250 flash dump, locate the gear-tooth
table, show/edit teeth with live ratio calculation, save a patched copy.

Table layout (21 bytes, validated against 02E300011BN / 02E300057B /
02E300013 dumps):
    [0..5]  input shaft teeth, gears 1-6
    [6]     input shaft teeth, reverse
    [7]     separator 0x01 (never modified)
    [8..13] output shaft teeth, gears 1-6
    [14]    output shaft teeth, reverse
    [15]    diff pinion 1 (final drive for gears 1-4)
    [16]    diff pinion 2 (final drive for gears 5,6,R)
    [17]    ring gear teeth (stored twice)
    [18]    ring gear teeth (second copy)
    [19-20] trailing pair (reverse idler on some boxes) - shown, not edited

Ratios:  mesh = out/in ; FD1 = ring/p1 ; FD2 = ring/p2 ;
         total = mesh * FD1 (gears 1-4) or * FD2 (5,6,R).

The tooth table is the only ratio storage in every checked dataset
(PQ 02E300011-066, FXX-era, MQB 0D9): the TCU derives ratios from these
bytes at runtime; no separate 16-bit ratio table exists.

Usage:   py -3 dq250_gear_tool.py             (GUI)
         py -3 dq250_gear_tool.py --test FILE (headless decode check)
"""
import json
import math
import os
import re
import sys
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:                      # OS file drag & drop (optional dependency)
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _probe = TkinterDnD.Tk()      # never mapped - just proves tkdnd loads
    _probe.destroy()
    _BASE, DND_FILES_OK = TkinterDnD.Tk, True
except Exception:         # package missing or its tkdnd libs are not bundled
    _BASE, DND_FILES_OK = tk.Tk, False

GEAR_EDGE = "#c9d2dc"     # gear panel border: idle / highlighted
GEAR_HOT = "#2f7fd6"
DIFF_UP_BG = "#d7f0d7"    # field is higher than the same field in the other side
DIFF_DOWN_BG = "#f8dada"  # field is lower
FIELD_BG = "#ffffff"      # neutral field
DROP_DELAY = 250          # ms before the highlight clears after leaving
OVL_BG = "#10202f"        # translucent cover colour
OVL_ALPHA = 0.62          # alpha of the cover window

VERSION = "v0.1"
ABOUT_TITLE = f"DQ250 gear editor {VERSION}"
DEVELOPER = "Developed by MHHAUTO"
AUTHOR = "user lsdlsd88"
GITHUB_URL = "https://github.com/lsdlsd88/"
DONATE_URL = "https://ko-fi.com/lsdlsd88"      # the DONATE button opens this
MHHAUTO_URL = "https://mhhauto.com/User-lsdlsd88"   # author's MHHAUTO profile
ABOUT_MIN_MS = 5000       # the welcome card cannot be dismissed before this


TOOTH_PAT = re.compile(
    rb"([\x0a-\x32]{6})\x0e\x01([\x0a-\x32]{6})([\x0a-\x32])"
    rb"([\x0a-\x50]{2})([\x0a-\x50])\5([\x0a-\x50]{2})",
    re.DOTALL,
)
SW_PAT = re.compile(rb"02E3\d{5}[A-Z]{0,2}|0D9\d{6}[A-Z]{0,2}")
VCODE_PAT = re.compile(rb"v069[0-9A-Za-z]{2}[0-9]{4}e[ac]", re.I)

TABLE_LEN = 21
FIELDS = ([f"in{i}" for i in range(6)] + ["inR"]
          + [f"out{i}" for i in range(6)] + ["outR"]
          + ["p1", "p2", "ring"])
GEAR_KEYS = [("in0", "out0"), ("in1", "out1"), ("in2", "out2"),
             ("in3", "out3"), ("in4", "out4"), ("in5", "out5"),
             ("inR", "outR")]


def find_tables(data):
    """gear tables only: structural matches whose teeth decode plausibly.
    Erased-flash regions (0x0E fill -> fake 14/14 teeth, all ratios 1.0)
    are discarded, as are any other degenerate matches."""
    out = []
    for m in TOOTH_PAT.finditer(data):
        tin = list(m.group(1)) + [0x0E]
        tout = list(m.group(2)) + [m.group(3)[0]]
        p1, p2 = m.group(4)[0], m.group(4)[1]
        ring = m.group(5)[0]
        x, y = m.group(6)[0], m.group(6)[1]
        t = {"offset": m.start(), "tin": tin, "tout": tout,
             "p1": p1, "p2": p2, "ring": ring, "x": x, "y": y}
        if not plausible(t):
            continue
        out.append(t)
    out.sort(key=lambda t: t["offset"])
    return out


def ratios(t):
    """tooth dict -> (mesh[7], fd1, fd2, totals[7]); index 6 = reverse"""
    fd1 = t["ring"] / t["p1"]
    fd2 = t["ring"] / t["p2"]
    mesh = [t["tout"][i] / t["tin"][i] for i in range(7)]
    tot = [mesh[i] * (fd1 if i < 4 else fd2) for i in range(7)]
    return mesh, fd1, fd2, tot


def plausible(t):
    """a table is only trusted when the teeth vary and the ring is big"""
    vals = list(t["tin"]) + list(t["tout"]) + [t["p1"], t["p2"], t["ring"]]
    return len(set(vals)) >= 8 and t["ring"] >= 40


HEX_RE = re.compile(r"^[0-9a-f]{1,16}$")


def parse_offset(text):
    """hex by default (as displayed); '0x' optional, 'd' prefix = decimal"""
    s = text.strip().replace("_", "").replace(" ", "").lower()
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("d") and s[1:].isdigit():
        return int(s[1:], 10)
    if s.startswith("0x"):
        s = s[2:]
    if not HEX_RE.match(s):
        raise ValueError("not a hex offset")
    return int(s, 16)


def decode_at(data, off):
    """decode the fixed 21-byte tooth table at any offset (manual lookup)"""
    raw = data[off:off + TABLE_LEN]
    if off < 0 or len(raw) < TABLE_LEN:
        return None
    return {"offset": off,
            "tin": list(raw[0:6]) + [raw[6]],
            "tout": list(raw[8:14]) + [raw[14]],
            "p1": raw[15], "p2": raw[16], "ring": raw[17],
            "x": raw[19], "y": raw[20]}


def _nice_step(v):
    """1/2/5 x 10^n step that splits a 0..v axis into ~4 lines"""
    raw = max(v, 0.1) / 4.0
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def hexdump(data, center, before=48, after=48, row=16):
    """fixed 16-byte rows that start on addresses ending in 0; one address
    column, hex padded so the ascii gutter lines up on every row"""
    lo = max(0, (center - before) // row * row)
    hi = min(len(data), center + after)
    hi = min(len(data), -(-hi // row) * row)
    lines = []
    for i in range(lo, hi, row):
        chunk = data[i:i + row]
        hx = " ".join(f"{b:02X}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append((i, f"0x{i:06X}  {hx:<{row * 3 - 1}s}  {asc}"))
    return lines


def enable_dpi_awareness():
    """Windows virtualises coordinates unless the process opts in: Tk then
    reports logical pixels while the screen - and any screen capture - is in
    physical ones, so a captured rect is both misplaced and mis-scaled.
    Must run before the first window exists."""
    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    try:                      # per-monitor v2, the best Tk can cope with
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except Exception:
        pass
    try:                      # Windows 8.1+: per-monitor v1
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return True
    except Exception:
        pass
    try:                      # Vista+: system aware
        return bool(user32.SetProcessDPIAware())
    except Exception:
        return False


def _state_file():
    """small json with the last folders used, one per side"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "dq250_gear_tool.json")


def load_state():
    try:
        with open(_state_file(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        with open(_state_file(), "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
    except OSError:
        pass


class GearPane(ttk.Frame):
    """One flash dump: its own offset list, gear grid, differential, plot,
    hex view, drop target and save. The window stacks two of these (A / B)."""

    def __init__(self, master, tag):
        super().__init__(master)
        self.tag = tag
        self.data = None
        self.path = None
        self.tables = []
        self.current = None
        self.vars = {}
        self.entries = {}
        self.ratio_lbls = []
        self._offset_editor = None
        self._offset_edit_iid = None
        self._hide_job = None
        self._drop_hot = False
        self._ratios = None
        self._plot_pts = []
        self._hover_g = None
        self._ovl_tk = None
        self._hex_visible = True
        self._build()
        self.after_idle(self._build_overlay)   # translucent cover, at idle

    # ------------------------------------------------------------ UI
    def _build(self):
        # no drop box: the whole window is the drop target and the ratios
        # GUI fills it; a translucent cover appears only while dragging
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 3))
        ttk.Label(top, text=self.tag, width=2,
                  font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(top, text="Open...",
                   command=self.open_file).pack(side="left", padx=(6, 0))
        self.lbl_file = ttk.Label(top, text="no file")
        self.lbl_file.pack(side="left", padx=10)
        self.btn_about = ttk.Button(top, text="About", width=8)
        self.btn_about.pack(side="right")
        if not DND_FILES_OK:
            ttk.Label(top, text="(drag & drop needs: pip install tkinterdnd2)",
                      foreground="#777").pack(side="right")

        if DND_FILES_OK:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<DropEnter>>", lambda _e: self._overlay(True))
            self.dnd_bind("<<DropPosition>>", lambda _e: self._overlay(True))
            self.dnd_bind("<<DropLeave>>", lambda _e: self._overlay(False))
            self.dnd_bind("<<Drop>>", self._on_drop)

        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        # --- left: one editable offset cell per table, nothing else
        left = ttk.Frame(body)
        left.pack(side="left", fill="y", anchor="n")
        self.tv_matches = ttk.Treeview(left, columns=("off",), show="",
                                       height=1, selectmode="browse")
        self.tv_matches.column("off", width=84, stretch=False)
        self.tv_matches.pack(anchor="nw")
        self.tv_matches.bind("<<TreeviewSelect>>", self._pick_table)
        self.tv_matches.bind("<Double-1>", self._edit_offset)

        # --- right: editor
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        # everything showing the gears lives here; its border turns blue
        # while a file is dragged over the window
        self.gear_area = gear = tk.Frame(right, highlightthickness=2,
                                         highlightbackground=GEAR_EDGE)
        gear.pack(fill="x")
        grid = ttk.LabelFrame(gear, text="gear teeth (editable)")
        grid.pack(fill="x")   # natural height: rows neither stretch nor clip
        for c, h in enumerate(("gear", "input", "output", "mesh", "FD",
                               "total")):
            ttk.Label(grid, text=h, font=("", 9, "bold")).grid(
                row=0, column=c, padx=6)
        for g, (ik, ok) in enumerate(GEAR_KEYS):
            r = g + 1
            name = f"{g + 1}th" if g < 6 else "R"
            ttk.Label(grid, text=name).grid(row=r, column=0, sticky="e")
            self._entry(grid, ik, r, 1)
            self._entry(grid, ok, r, 2)
            ml = ttk.Label(grid, text="-")
            ml.grid(row=r, column=3, padx=6)
            fl = ttk.Label(grid, text="-")
            fl.grid(row=r, column=4, padx=6)
            tl = ttk.Label(grid, text="-")
            tl.grid(row=r, column=5, padx=6)
            self.ratio_lbls.append((ml, fl, tl, g))
        self.gear_grid = grid

        # right of the ratios: the differential parameters, stacked vertically
        side_diff = ttk.Frame(grid)
        side_diff.grid(row=0, column=6, rowspan=9, sticky="nsew",
                       padx=(18, 4), pady=2)
        diff = ttk.LabelFrame(side_diff, text="differential / final drive")
        diff.pack(fill="both", expand=True)   # full cell height: FD2 must fit
        for i, (key, text) in enumerate((("p1", "pinion 1 (gears 1-4)"),
                                         ("p2", "pinion 2 (gears 5,6,R)"),
                                         ("ring", "ring teeth"))):
            ttk.Label(diff, text=text).grid(row=i * 2, column=0, sticky="w")
            self._entry(diff, key, i * 2 + 1, 0)
        self.lbl_fd = ttk.Label(diff, text="FD1 = -\nFD2 = -")
        self.lbl_fd.grid(row=6, column=0, sticky="w", pady=(8, 2))

        # the plot fills the free space right of the differential panel
        self.cv_plot = tk.Canvas(grid, width=250, height=200, bg="#ffffff",
                                 highlightthickness=1,
                                 highlightbackground="#d6dde4")
        self.cv_plot.grid(row=0, column=7, rowspan=9, sticky="nsew",
                          padx=(16, 6), pady=2)
        grid.columnconfigure(7, weight=1)   # plot takes the rest of the width
        self.cv_plot.bind("<Configure>", lambda _e: self._draw_plot())
        self.cv_plot.bind("<Motion>", self._plot_hover)
        self.cv_plot.bind("<Leave>", self._plot_leave)

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="Reload from file",
                   command=self._reload).pack(side="left")
        ttk.Button(btns, text="Save patched as...",
                   command=self.save).pack(side="left", padx=6)
        self.btn_hex = ttk.Button(btns, text="Hide hex",
                                  command=self._toggle_hex)
        self.btn_hex.pack(side="left")
        self.btn_copy = ttk.Button(btns, text="copy", width=9,
                                   command=self._copy_to_sibling)
        self.btn_copy.pack(side="left", padx=6)
        self.lbl_status = ttk.Label(btns, text="")
        self.lbl_status.pack(side="left", padx=8)

        self.lbl_hex = ttk.Label(right, text="hex around table:")
        self.lbl_hex.pack(anchor="w")
        self.txt_hex = tk.Text(right, height=6, font=("Consolas", 9),
                               state="disabled")
        self.txt_hex.pack(fill="both", expand=True)
        self._toggle_hex()          # start collapsed: the grid needs the room

    def _entry(self, parent, key, row, col):
        var = tk.StringVar()
        var.trace_add("write", lambda *_: self._on_edit())
        # classic Entry on purpose: the Windows ttk theme ignores
        # fieldbackground, so a ttk field cannot be tinted
        e = tk.Entry(parent, textvariable=var, width=7, justify="center",
                     relief="solid", bd=1, bg=FIELD_BG, highlightthickness=0)
        e.grid(row=row, column=col, padx=4, pady=1)
        self.vars[key] = var
        self.entries[key] = e

    def set_diff(self, key, sign):
        """tint a field: +1 higher than the other side, -1 lower, 0 neutral"""
        e = self.entries.get(key)
        if e is None:
            return
        e.configure(bg={1: DIFF_UP_BG,
                        -1: DIFF_DOWN_BG}.get(sign, FIELD_BG))

    def _entry_labeled(self, parent, key, label, row, col):
        ttk.Label(parent, text=label).grid(row=row, column=col,
                                           sticky="e", padx=4)
        self._entry(parent, key, row, col + 1)

    # --------------------------------------------------------- actions
    def _default_dir(self):
        if getattr(sys, "frozen", False):
            return os.path.dirname(os.path.abspath(sys.executable))
        return os.path.dirname(os.path.abspath(__file__))

    def _start_dir(self):
        """last folder used on this side; if it never opened anything, the
        folder of the last file opened anywhere"""
        st = load_state()
        for key in (f"dir_{self.tag.lower()}", "dir_last"):
            d = st.get(key)
            if d and os.path.isdir(d):
                return d
        return self._default_dir()

    def _remember_dir(self, path):
        folder = os.path.dirname(os.path.abspath(path))
        st = load_state()
        st[f"dir_{self.tag.lower()}"] = folder
        st["dir_last"] = folder
        save_state(st)

    def open_file(self):
        p = filedialog.askopenfilename(
            title="open DQ250 flash dump",
            initialdir=self._start_dir(),
            filetypes=[("all files", "*.*"),
                       ("flash dumps", "*.bin *.ori *.mmf *.flash")])
        if not p:
            return
        self.load_path(p)

    # ------------------------------------------------------ drop cover
    def _panel_rect(self):
        """screen rect the cover has to fill, in physical pixels.

        The cover sits inside the panel's highlight border, so the rect is
        inset by that border - otherwise it lands a few pixels off."""
        ga = self.gear_area
        try:
            border = (int(ga.cget("highlightthickness"))
                      + int(ga.cget("bd")))
        except (tk.TclError, ValueError):
            border = 0
        if sys.platform == "win32":
            try:
                import ctypes

                class _Rect(ctypes.Structure):
                    _fields_ = [("left", ctypes.c_long),
                                ("top", ctypes.c_long),
                                ("right", ctypes.c_long),
                                ("bottom", ctypes.c_long)]

                rc = _Rect()
                if ctypes.windll.user32.GetWindowRect(ga.winfo_id(),
                                                      ctypes.byref(rc)):
                    return (rc.left + border, rc.top + border,
                            rc.right - border, rc.bottom - border)
            except Exception:
                pass
        x, y = ga.winfo_rootx(), ga.winfo_rooty()
        return (x + border, y + border,
                x + ga.winfo_width() - border, y + ga.winfo_height() - border)

    # ------------------------------------------------ translucent overlay
    def _build_overlay(self):
        """translucent cover, built at idle so no window is created while the
        OLE drag loop owns the message queue (that is what used to flicker)"""
        try:
            ovl = tk.Toplevel(self.winfo_toplevel())
            ovl.withdraw()
            ovl.overrideredirect(True)
            ovl.configure(bg=OVL_BG)
            try:
                ovl.attributes("-alpha", OVL_ALPHA)
            except tk.TclError:
                self._ovl_tk = None
                return
            tk.Label(ovl, text="drop here to open", bg=OVL_BG, fg="#ffffff",
                     font=("Segoe UI", 20, "bold")).pack(expand=True)
            ovl.update_idletasks()
            self._ovl_tk = ovl
            self._own_overlay(ovl)
            self._pass_drag_through(ovl)
        except tk.TclError:
            self._ovl_tk = None

    def _own_overlay(self, win):
        """make the cover an *owned* window of the app: it then stays above
        its owner, follows it when minimised and never shows up on its own
        after an alt-tab (a plain top-level drops behind).

        Tk's transient() is ignored for overrideredirect windows, so the
        Win32 owner is set directly (GWLP_HWNDPARENT) and verified with
        GetWindow(GW_OWNER)."""
        try:
            win.transient(self)
        except tk.TclError:
            pass
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            u32 = ctypes.WinDLL("user32")
            u32.GetParent.argtypes = (wintypes.HWND,)
            u32.GetParent.restype = wintypes.HWND
            u32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int,
                                              ctypes.c_void_p)
            u32.SetWindowLongPtrW.restype = ctypes.c_void_p
            child = u32.GetParent(win.winfo_id()) or win.winfo_id()
            top = self.winfo_toplevel()
            owner = u32.GetParent(top.winfo_id()) or top.winfo_id()
            u32.SetWindowLongPtrW(wintypes.HWND(child), -8,
                                  ctypes.c_void_p(owner))
        except Exception:
            pass

    def _pass_drag_through(self, win):
        """keep the OS drag on the app window: a window under the cursor with
        WS_EX_TRANSPARENT is skipped by hit-testing"""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            u32 = ctypes.WinDLL("user32")
            u32.GetParent.argtypes = (wintypes.HWND,)
            u32.GetParent.restype = wintypes.HWND
            ws = 0x08000000 | 0x00000020        # NOACTIVATE | TRANSPARENT
            hwnd = win.winfo_id()
            for h in {hwnd, u32.GetParent(hwnd)}:
                if h:
                    ex = u32.GetWindowLongW(wintypes.HWND(h), -20)
                    u32.SetWindowLongW(wintypes.HWND(h), -20, ex | ws)
        except Exception:
            pass

    def _show_layered(self):
        """show the real translucent cover over the gear panel"""
        ovl = getattr(self, "_ovl_tk", None)
        if ovl is None:
            return False
        try:
            if float(ovl.attributes("-alpha")) >= 0.999:
                return False                # no real alpha here
            x1, y1, x2, y2 = self._panel_rect()
            if not ovl.winfo_ismapped():
                self._own_overlay(ovl)   # owner only exists once we are mapped
                ovl.geometry(f"{x2 - x1}x{y2 - y1}+{x1}+{y1}")
                ovl.deiconify()
                self._pass_drag_through(ovl)
                tk.Misc.lift(ovl)
        except tk.TclError:
            return False
        return True

    def _hide_layered(self):
        ovl = getattr(self, "_ovl_tk", None)
        if ovl is not None:
            try:
                ovl.withdraw()
            except tk.TclError:
                pass

    def _overlay(self, show):
        """drag in/out: show the translucent cover, else highlight the panel.
        Un-highlighting is delayed so widget-to-widget churn does not blink."""
        if not DND_FILES_OK:
            return
        if show:
            if self._hide_job is not None:
                self.after_cancel(self._hide_job)
                self._hide_job = None
            if self._drop_hot:
                return
            self._drop_hot = True
            if not self._show_layered():
                self.gear_area.configure(highlightbackground=GEAR_HOT)
                self.lbl_status.configure(text="release to load this dump")
        elif self._drop_hot and self._hide_job is None:
            self._hide_job = self.after(DROP_DELAY, self._hide_overlay)

    def _pointer_inside(self):
        """is the cursor over this window right now? (works mid-drag, unlike
        the drag events, which stop arriving as soon as the mouse is still)"""
        try:
            x, y = self.winfo_pointerxy()
        except tk.TclError:
            return False
        return (self.winfo_rootx() <= x < self.winfo_rootx() + self.winfo_width()
                and self.winfo_rooty() <= y
                < self.winfo_rooty() + self.winfo_height())

    def _hide_overlay(self, force=False):
        self._hide_job = None
        if not force and self._pointer_inside():
            # held still over us: <<DropPosition>> stops firing, so keep the
            # cover up and check again instead of dropping it on a timer
            self._hide_job = self.after(DROP_DELAY, self._hide_overlay)
            return
        self._drop_hot = False
        self._hide_layered()
        self.gear_area.configure(highlightbackground=GEAR_EDGE)
        self.lbl_status.configure(text="")

    def _on_drop(self, event):
        """<<Drop>> from the OS: the first file goes into this side, a second
        one into the other side, so both can be filled in one drop"""
        if self._hide_job is not None:
            self.after_cancel(self._hide_job)
            self._hide_job = None
        self._hide_overlay(force=True)
        try:
            paths = [p for p in self.tk.splitlist(event.data) if p]
        except tk.TclError:
            paths = []
        if not paths:
            self.lbl_status.config(text="nothing usable was dropped")
            return
        self.load_path(paths[0])
        sibling = getattr(self, "sibling", None)
        if len(paths) > 1 and sibling is not None:
            sibling.load_path(paths[1])
            self.lbl_status.config(
                text=f"{os.path.basename(paths[1])} went to {sibling.tag}")

    def load_path(self, p):
        """open one flash dump - used by the dialog and by an OS drop"""
        if os.path.isdir(p):
            self.lbl_status.config(
                text=f"{os.path.basename(p)} is a folder - drop a file")
            return
        try:
            with open(p, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            self.lbl_status.config(
                text=f"cannot read {os.path.basename(p)}: {exc.strerror}")
            return
        self.data = data
        self.path = os.path.abspath(p)
        self._remember_dir(self.path)
        sw = SW_PAT.search(self.data)
        vc = VCODE_PAT.search(self.data)
        self.lbl_file.config(text=(
            self.path
            + f"  ({len(self.data):,} bytes)"
            + (f"   SW {sw.group(0).decode()}" if sw else "")
            + (f"   {vc.group(0).decode()}" if vc else "")))
        self.tables = find_tables(self.data)
        self._rebuild_matches()
        self.current = None
        for k in FIELDS:
            self.vars[k].set("")
        if self.tables:
            # auto-select the first table - no click needed
            self._select_table(self.tables[0]["offset"])
        else:
            self.lbl_status.config(text="no tooth table found in this file")

    def _pick_table(self, _ev=None):
        sel = self.tv_matches.selection()
        if not sel:
            return
        off = int(sel[0])
        t = next((x for x in self.tables if x["offset"] == off), None)
        if t is None:
            return
        self.current = t
        self._fill_from_table()
        self._show_hex()

    # ------------------------------------------------- offset navigation
    def _rebuild_matches(self):
        self.tv_matches.delete(*self.tv_matches.get_children())
        for t in self.tables:
            self.tv_matches.insert("", "end", iid=str(t["offset"]),
                                   values=(f"+0x{t['offset']:06X}",))
        # one table -> one cell, no empty rows below it
        self.tv_matches.configure(height=min(max(len(self.tables), 1), 8))

    def _select_table(self, off):
        iid = str(off)
        if not self.tv_matches.exists(iid):
            return False
        self.tv_matches.selection_set(iid)
        self.tv_matches.focus(iid)
        self.tv_matches.see(iid)
        self._pick_table()
        return True

    def _ensure_table(self, off):
        """table at off, decoding it manually when it is not in the list"""
        if self.data is None:
            self.lbl_status.config(text="open a file first")
            return None
        if off < 0 or off + TABLE_LEN > len(self.data):
            self.lbl_status.config(
                text=f"+0x{off:X} is outside the file ({len(self.data):,} bytes)")
            return None
        t = next((x for x in self.tables if x["offset"] == off), None)
        if t is None:
            t = decode_at(self.data, off)
            self.tables.append(t)
            self.tables.sort(key=lambda x: x["offset"])
            self._rebuild_matches()
        return t

    def _edit_offset(self, event=None):
        if event is not None:
            iid = self.tv_matches.identify_row(event.y)
            if not iid:
                return
            # only a click inside the offset cell starts an edit; compare
            # against its bbox because identify_column() can drift from it
            bx, _by, bw, _bh = self.tv_matches.bbox(iid, "#1")
            if not (bx <= event.x < bx + bw):
                return
        else:
            sel = self.tv_matches.selection()
            iid = sel[0] if sel else ""
        if not iid:
            return
        self._close_offset_editor()
        bbox = self.tv_matches.bbox(iid, "#1")
        if not bbox:
            return
        x, y, w, h = bbox
        ed = ttk.Entry(self.tv_matches, justify="center")
        ed.place(x=x, y=y, width=w, height=h)
        ed.insert(0, f"0x{int(iid):06X}")
        ed.select_range(0, "end")
        ed.focus_set()
        ed.bind("<Return>", lambda _e: self._commit_offset())
        ed.bind("<Escape>", self._cancel_offset_edit)
        ed.bind("<FocusOut>", lambda _e: self._commit_offset())
        self._offset_editor = ed
        self._offset_edit_iid = iid

    def _cancel_offset_edit(self, _ev=None):
        """Escape inside the inline offset editor only cancels it - the
        window-level Escape (quit) must not fire as well"""
        self._close_offset_editor()
        return "break"

    def _close_offset_editor(self):
        ed, self._offset_editor = self._offset_editor, None
        self._offset_edit_iid = None
        if ed is not None:
            try:
                ed.destroy()
            except tk.TclError:
                pass

    def _commit_offset(self):
        ed = self._offset_editor
        if ed is None:
            return
        iid, text = self._offset_edit_iid, ed.get()
        self._close_offset_editor()
        try:
            off = parse_offset(text)
        except ValueError:
            self.lbl_status.config(
                text=f"not a hex offset: {text.strip()!r} (e.g. 0x040609)")
            return
        if off != int(iid):
            self._set_table_offset(int(iid), off)

    def _set_table_offset(self, old_off, new_off):
        """retarget one row at a different offset and re-decode the bytes"""
        t = self._ensure_table(new_off)
        if t is None:
            return
        self.tables = [x for x in self.tables if x["offset"] != old_off]
        self._rebuild_matches()
        self._select_table(new_off)
        if plausible(t):
            self.lbl_status.config(
                text=f"table moved +0x{old_off:06X} -> +0x{new_off:06X}")
        else:
            self.lbl_status.config(
                text=f"moved to +0x{new_off:06X}, but it does not look like a "
                     f"table (ring={t['ring']})")

    # --------------------------------------------------------- editor
    def _fill_from_table(self):
        t = self.current
        for i in range(6):
            self.vars[f"in{i}"].set(str(t["tin"][i]))
            self.vars[f"out{i}"].set(str(t["tout"][i]))
        self.vars["inR"].set(str(t["tin"][6]))
        self.vars["outR"].set(str(t["tout"][6]))
        self.vars["p1"].set(str(t["p1"]))
        self.vars["p2"].set(str(t["p2"]))
        self.vars["ring"].set(str(t["ring"]))

    def _reload(self):
        if self.data and self.current:
            self._fill_from_table()

    def _gather(self):
        """entries -> (tooth dict, list of invalid field names)"""
        bad, vals = [], {}
        for k in FIELDS:
            try:
                v = int(self.vars[k].get())
                if not (1 <= v <= 255):
                    raise ValueError
            except (ValueError, tk.TclError):
                bad.append(k)
                v = None
            vals[k] = v
        if bad:
            return None, bad
        return {"tin": [vals[f"in{i}"] for i in range(6)] + [vals["inR"]],
                "tout": [vals[f"out{i}"] for i in range(6)] + [vals["outR"]],
                "p1": vals["p1"], "p2": vals["p2"], "ring": vals["ring"]}, []

    def _on_edit(self):
        t, bad = self._gather()
        if t:
            mesh, fd1, fd2, tot = ratios(t)
        for ml, fl, tl, g in self.ratio_lbls:
            if t:
                fd = fd1 if g < 4 else fd2
                ml.config(text=f"{mesh[g]:.4f}")
                fl.config(text=f"FD{1 if g < 4 else 2}")
                tl.config(text=f"{tot[g]:.4f}")
            else:
                ml.config(text="?")
                fl.config(text="?")
                tl.config(text="?")
        if t:
            self.lbl_fd.config(
                text=f"FD1 = {t['ring']}/{t['p1']} = {fd1:.4f}\n"
                     f"FD2 = {t['ring']}/{t['p2']} = {fd2:.4f}")
            self.lbl_status.config(text="")
        elif bad:
            self.lbl_status.config(text="invalid: " + ", ".join(bad))
        self._ratios = ((mesh, fd1, fd2, tot, t["tin"], t["tout"])
                        if t else None)
        self._draw_plot()
        top = self.winfo_toplevel()
        if hasattr(top, "_refresh_diff"):
            top._refresh_diff()

    # ------------------------------------------------------------- plot
    def _draw_plot(self):
        cv = getattr(self, "cv_plot", None)
        if cv is None:
            return
        cv.delete("all")
        self._plot_pts = []
        self._hover_g = None
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 60 or h < 40:
            return
        if not self._ratios:
            # empty plot doubles as the drop hint for this side
            hint = ("drag & drop a flash dump here"
                    if DND_FILES_OK else "open a flash dump")
            cv.create_text(w // 2, h // 2 - 10, text=hint, tags="hint",
                           fill="#8a97a4", font=("Segoe UI", 11))
            if DND_FILES_OK:
                cv.create_text(w // 2, h // 2 + 12,
                               text="or press Open...", tags="hint",
                               fill="#aab6c2", font=("Segoe UI", 9))
            return
        _mesh, _fd1, _fd2, tot = self._ratios[:4]
        vals = [tot[g] for g in range(6)]
        left, right, top, bottom = 26, 6, 6, 14
        pw, ph = max(w - left - right, 10), max(h - top - bottom, 10)
        step = _nice_step(max(vals))
        ymax = step * math.ceil(max(vals) / step)
        for i in range(int(round(ymax / step)) + 1):
            y = top + ph - ph * i * step / ymax
            cv.create_line(left, y, left + pw, y, fill="#e4e8ec")
            cv.create_text(left - 3, y, text=f"{i * step:g}", anchor="e",
                           fill="#8a97a4", font=("Segoe UI", 7))
        pts = []
        for g in range(6):
            x = left + pw * (g + 0.5) / 6
            y = top + ph - ph * vals[g] / ymax
            pts.append((x, y))
            self._plot_pts.append((g, x, y))
            cv.create_text(x, top + ph + 7, text=str(g + 1), fill="#8a97a4",
                           font=("Segoe UI", 7))
        cv.create_line([c for p in pts for c in p], fill="#1f4e79", width=2)
        for x, y in pts:
            cv.create_rectangle(x - 3, y - 3, x + 3, y + 3, fill="#1f4e79",
                                outline="")

    def _plot_hover(self, event):
        if not self._plot_pts:
            return
        g, x, y = min(self._plot_pts,
                      key=lambda p: (p[1] - event.x) ** 2
                      + (p[2] - event.y) ** 2)
        if (x - event.x) ** 2 + (y - event.y) ** 2 > 100:   # 10 px
            g = None
        if g == self._hover_g:
            return
        self._hover_g = g
        self._plot_tip(g)

    def _plot_leave(self, _ev=None):
        self._hover_g = None
        self._plot_tip(None)

    def _plot_tip(self, g):
        """value box next to one point, drawn inside the canvas"""
        cv = self.cv_plot
        cv.delete("tip")
        if g is None or not self._ratios:
            return
        mesh, fd1, fd2, tot, tin, tout = self._ratios
        name = f"{g + 1}th" if g < 6 else "R"
        fd = fd1 if g < 4 else fd2
        text = (f"{name}   in {tin[g]}  out {tout[g]}\n"
                f"mesh  {mesh[g]:.4f}\n"
                f"total {tot[g]:.4f}   FD{1 if g < 4 else 2} {fd:.4f}")
        px, py = self._plot_pts[g][1], self._plot_pts[g][2]
        w, h = cv.winfo_width(), cv.winfo_height()
        tx = px + 8
        if tx > w - 130:
            tx = max(px - 130, 2)
        ty = min(max(py - 24, 2), h - 46)
        item = cv.create_text(tx, ty, text=text, anchor="nw", tags="tip",
                              fill="#1b2733", font=("Consolas", 8))
        x1, y1, x2, y2 = cv.bbox(item)
        box = cv.create_rectangle(x1 - 4, y1 - 3, x2 + 4, y2 + 3,
                                  fill="#fffbe8", outline="#c9b98a",
                                  tags="tip")
        cv.tag_lower(box, item)

    def _copy_to_sibling(self):
        """push this pane's tooth table into the other pane's editor"""
        other = getattr(self, "sibling", None)
        if other is None:
            return
        if self.current is None:
            self.lbl_status.config(text="nothing to copy")
            return
        if other.current is None:
            self.lbl_status.config(
                text=f"{other.tag} has no table - open a dump there first")
            return
        for key in FIELDS:
            other.vars[key].set(self.vars[key].get())
        other.lbl_status.config(text=f"{self.tag} values copied in")
        self.lbl_status.config(text=f"copied to {other.tag}")

    def _toggle_hex(self):
        """collapse / expand the hex view"""
        self._hex_visible = not self._hex_visible
        if self._hex_visible:
            self.lbl_hex.pack(anchor="w")
            self.txt_hex.pack(fill="both", expand=True)
            self.btn_hex.configure(text="Hide hex")
        else:
            self.txt_hex.pack_forget()
            self.lbl_hex.pack_forget()
            self.btn_hex.configure(text="Show hex")
        top = self.winfo_toplevel()
        if hasattr(top, "_fit_height"):
            top.after_idle(top._fit_height)

    def _show_hex(self):
        self.txt_hex.config(state="normal")
        self.txt_hex.delete("1.0", "end")
        self.txt_hex.tag_configure("table", background="#fff2cc")
        if self.data and self.current:
            lo = self.current["offset"]
            hi = lo + TABLE_LEN
            for n, (off, line) in enumerate(hexdump(self.data, lo), start=1):
                self.txt_hex.insert("end", line + "\n")
                a, b = max(off, lo), min(off + 16, hi)
                if a < b:          # amber over exactly the table's bytes
                    c1 = 10 + 3 * (a - off)
                    c2 = 10 + 3 * (b - off) - 1
                    self.txt_hex.tag_add("table", f"{n}.{c1}", f"{n}.{c2}")
        self.txt_hex.config(state="disabled")

    def save(self):
        if not (self.data and self.current):
            messagebox.showinfo("DQ250 gear editor", "open a file first")
            return
        t, bad = self._gather()
        if bad:
            messagebox.showerror("DQ250 gear editor",
                                 "fix invalid fields: " + ", ".join(bad))
            return
        off = self.current["offset"]
        old = self.data[off:off + TABLE_LEN]
        new = (bytes(t["tin"][0:6]) + bytes([t["tin"][6]]) + old[7:8]
               + bytes(t["tout"][0:6]) + bytes([t["tout"][6]])
               + bytes([t["p1"], t["p2"], t["ring"], t["ring"]])
               + old[19:21])
        if new == old:
            messagebox.showinfo("DQ250 gear editor", "nothing changed")
            return
        root, ext = os.path.splitext(self.path)
        out = filedialog.asksaveasfilename(
            initialdir=os.path.dirname(os.path.abspath(self.path)),
            initialfile=os.path.basename(root) + "_gears_patched" + ext,
            parent=self)
        if not out:
            return
        patched = self.data[:off] + new + self.data[off + TABLE_LEN:]
        with open(out, "wb") as fh:
            fh.write(patched)
        changes = [f"+0x{off + i:06X}: {old[i]:3d} -> {new[i]:3d}"
                   for i in range(TABLE_LEN) if old[i] != new[i]]
        messagebox.showinfo(
            "DQ250 gear editor",
            f"saved:\n{out}\n\n"
            "CRC / CHECKSUM NOT CALCULATED - this file is not ready to "
            "flash. Recalculate it with your flashing tool first.\n\n"
            f"changed {len(changes)} bytes at +0x{off:06X}:\n"
            + "\n".join(changes))


class GearTool(_BASE):
    """the window: two independent dumps stacked, A over B"""

    def __init__(self, show_about=True):
        enable_dpi_awareness()
        super().__init__()
        self.title(f"{ABOUT_TITLE}  -  A / B")
        try:                          # size point-based fonts for this display
            self.tk.call("tk", "scaling",
                         max(1.0, self.winfo_fpixels("1i") / 72.0))
        except tk.TclError:
            pass
        self.geometry("1220x1020")
        self._splash = show_about      # the private build skips the wait too
        self.a = GearPane(self, "A")
        self.a.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=6)
        self.b = GearPane(self, "B")
        self.b.pack(fill="x", padx=6, pady=(0, 6))
        self.a.sibling, self.b.sibling = self.b, self.a
        self.a.btn_about.configure(command=self._show_about)
        self.b.btn_about.pack_forget()      # one About button, top right
        for pane, other in ((self.a, self.b), (self.b, self.a)):
            pane.btn_copy.configure(
                text=f"Transfer {pane.tag} \u2192 {other.tag}", width=17)
        self.bind("<Control-o>", lambda _e: self.a.open_file())
        self.bind("<Control-Shift-o>", lambda _e: self.b.open_file())
        self.bind("<Escape>", lambda _e: self._quit())   # ESC quits
        self.after_idle(self._fit_height)
        if show_about:
            self.after(250, self._show_about)

    def _show_about(self):
        """start-up card: who made it, that it is free, and a donate button"""
        old = getattr(self, "_about_win", None)
        if old is not None and old.winfo_exists():
            old.lift()
            return old
        win = tk.Toplevel(self)
        win.title(ABOUT_TITLE)
        win.transient(self)
        win.resizable(False, False)
        win.configure(bg="#eef2f7")
        box = tk.Frame(win, bg="#eef2f7", padx=90, pady=52)
        box.pack(fill="both", expand=True)
        tk.Label(box, text=ABOUT_TITLE, bg="#eef2f7", fg="#22303f",
                 font=("Segoe UI", 26, "bold")).pack()
        tk.Label(box, text=DEVELOPER, bg="#eef2f7", fg="#22303f",
                 font=("Segoe UI", 18)).pack(pady=(26, 0))
        author = tk.Label(box, text=AUTHOR, bg="#eef2f7", fg="#1f6feb",
                          cursor="hand2",
                          font=("Segoe UI", 16, "underline"))
        author.pack()
        author.bind("<Button-1>",
                    lambda _e: webbrowser.open(MHHAUTO_URL or GITHUB_URL))
        tk.Label(box, text="Distributed for FREE.", bg="#eef2f7",
                 fg="#1f7a3d", font=("Segoe UI", 16, "bold")).pack(pady=(26, 0))
        tk.Label(box,
                 text="Please DONATE if you would like to remove this screen.",
                 bg="#eef2f7", fg="#22303f",
                 font=("Segoe UI", 14)).pack(pady=(10, 22))

        def link(url):
            lbl = tk.Label(box, text=url, bg="#eef2f7", fg="#1f6feb",
                           cursor="hand2",
                           font=("Segoe UI", 12, "underline"))
            lbl.pack(pady=3)
            lbl.bind("<Button-1>", lambda _e: webbrowser.open(url))
            return lbl

        link(GITHUB_URL)
        link(DONATE_URL)

        style = ttk.Style(self)
        style.configure("Card.TButton", font=("Segoe UI", 13, "bold"))
        row = tk.Frame(box, bg="#eef2f7")
        row.pack(pady=(26, 0))
        donate = ttk.Button(row, text="DONATE", command=self._donate,
                            width=16, style="Card.TButton")
        donate.pack(side="left", padx=10)
        cont = ttk.Button(row, text="Continue", command=win.destroy, width=16,
                          style="Card.TButton")
        cont.pack(side="left", padx=10)
        wait = ABOUT_MIN_MS if getattr(self, "_splash", True) else 0
        cont.state(["disabled"]) if wait > 0 else None
        cnt = tk.Label(row, text="", bg="#eef2f7", fg="#5a6b7b",
                       font=("Segoe UI", 12, "bold"))
        cnt.pack(side="left", padx=(12, 0))

        if wait > 0:
            secs = max(1, int(round(wait / 1000.0)))
            step = max(50, wait // secs)

            def tick():
                nonlocal secs
                secs -= 1
                if secs > 0:
                    cnt.configure(text=f"{secs}s")
                    win.after(step, tick)
                else:
                    cnt.configure(text="")
                    cont.state(["!disabled"])

            cnt.configure(text=f"{secs}s")     # visible while it counts down
            win.after(step, tick)
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.bind("<Escape>", lambda _e: "break")   # ESC must not dismiss it
        # no grab_set: the card must not block the editor while it counts down
        donate.focus_set()
        self._about_win = win
        return win

    def _donate(self):
        if DONATE_URL:
            webbrowser.open(DONATE_URL)
        else:
            messagebox.showinfo(
                "DONATE", "Thank you!\n\nContact " + AUTHOR +
                " for the donation link.", parent=self)
    def _quit(self):
        """Escape on the window closes the program"""
        self.destroy()

    def _refresh_diff(self):
        """when both sides hold a table, tint every field that differs:
        green where it is higher than the other side, red where it is lower"""
        a, b = getattr(self, "a", None), getattr(self, "b", None)
        if a is None or b is None:
            return
        both = a.current is not None and b.current is not None
        for key in FIELDS:
            try:
                ia = int(a.vars[key].get().strip())
                ib = int(b.vars[key].get().strip())
            except (ValueError, KeyError):
                ia = ib = None
            if not both or ia is None or ib is None or ia == ib:
                a.set_diff(key, 0)
                b.set_diff(key, 0)
                continue
            a.set_diff(key, 1 if ia > ib else -1)
            b.set_diff(key, -1 if ia > ib else 1)

    def _fit_height(self):
        """shrink the window to what the two panes actually need, so B starts
        directly under A instead of at a half-way line"""
        try:
            self.update_idletasks()
            w = max(self.winfo_width(), self.winfo_reqwidth(), 1180)
            self.geometry(f"{w}x{self.winfo_reqheight()}")
        except tk.TclError:
            pass


def selftest(path):
    data = open(path, "rb").read()
    sw = SW_PAT.search(data)
    vc = VCODE_PAT.search(data)
    print(f"file: {path} ({len(data):,} bytes)"
          + (f"  SW {sw.group(0).decode()}" if sw else "")
          + (f"  {vc.group(0).decode()}" if vc else ""))
    tables = find_tables(data)
    if not tables:
        print("no tooth table found")
        return 1
    for t in tables:
        mesh, fd1, fd2, tot = ratios(t)
        print(f"\n[GEAR TABLE] +0x{t['offset']:06X} (A{0xA0000000 + t['offset']:07X})")
        print(f"  in  1-6,R: {t['tin']}")
        print(f"  out 1-6,R: {t['tout']}")
        print(f"  diff p1={t['p1']} p2={t['p2']} ring={t['ring']} tail={t['x']}&{t['y']}")
        print(f"  mesh: {'  '.join(f'{m:.4f}' for m in mesh)}")
        print(f"  FD1 {fd1:.4f}  FD2 {fd2:.4f}")
        print(f"  tot : {'  '.join(f'{x:.4f}' for x in tot)}")
        same = decode_at(data, t["offset"])
        keys = ("tin", "tout", "p1", "p2", "ring")
        ok = same is not None and all(same[k] == t[k] for k in keys)
        print(f"  re-decode at same offset: {'OK' if ok else 'MISMATCH ' + repr(same)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--test":
        sys.exit(selftest(sys.argv[2]))
    app = GearTool()
    app.mainloop()
