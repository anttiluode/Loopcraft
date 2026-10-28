"""LoopCraft — YouTube / local video to animated GIF desktop editor.

Run:
    py -3.13 -m pip install yt-dlp pillow imageio-ffmpeg
    py -3.13 loopcraft.py

Supports Python 3.10+; requires tkinter (included in standard Windows Python).
GIFs are created by FFmpeg's two-pass palettegen/paletteuse pipeline.
"""

from __future__ import annotations

import io
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

try:
    from PIL import Image, ImageTk
except ImportError as exc:
    raise SystemExit("Missing Pillow. Install with: py -m pip install pillow") from exc

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


# ------ Theme ----------------------------------------------------------------
BG = "#0A1020"
PANEL = "#141D30"
PANEL_ALT = "#10192A"
BORDER = "#2B3954"
TEXT = "#EFF5FF"
MUTED = "#95A8C4"
PURPLE = "#9A75FA"
PURPLE_DARK = "#6D4BC2"
AQUA = "#5DE3DA"
YELLOW = "#F7C777"
BAD = "#FE8E9C"
CANVAS_BG = "#0C1424"
PREVIEW_SIZE = (688, 376)
TIMELINE_WIDTH = 688
TIMELINE_HEIGHT = 106
THUMB_COUNT = 11
FRAME_FPS = 12


def popen_options() -> dict:
    """Avoid a flashing console for FFmpeg on Windows."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def get_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "FFmpeg not found. Install with: py -m pip install imageio-ffmpeg"
        ) from exc


def readable_time(value: float, with_tenths: bool = True) -> str:
    value = max(0.0, float(value))
    hh = int(value // 3600)
    mm = int(value // 60) % 60
    ss = value % 60
    secs = f"{ss:04.1f}" if with_tenths else f"{int(ss):02d}"
    return f"{hh:02d}:{mm:02d}:{secs}" if hh else f"{mm:02d}:{secs}"


def parse_time(value: str) -> float:
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3 or any(not x.strip() for x in parts):
        raise ValueError("Time must be seconds, MM:SS, or HH:MM:SS.")
    try:
        numbers = [float(item) for item in parts]
    except ValueError as exc:
        raise ValueError("Time must be seconds, MM:SS, or HH:MM:SS.") from exc
    if any(not math.isfinite(n) or n < 0 for n in numbers):
        raise ValueError("Time cannot be negative or infinite.")
    if len(numbers) > 1 and any(n >= 60 for n in numbers[1:]):
        raise ValueError("Minutes/seconds after a colon must be less than 60.")
    result = 0.0
    for number in numbers:
        result = result * 60 + number
    return result


def file_safe_name(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" ._")
    return (text[:75] or "loopcraft_output")


def probe_video(ffmpeg: str, filepath: Path) -> tuple[float, int, int]:
    """Get duration and video dimensions; works without a separate ffprobe binary."""
    cmd = [ffmpeg, "-hide_banner", "-i", str(filepath)]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            errors="replace", timeout=25, **popen_options())
    info = result.stderr + result.stdout
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", info)
    video_line = next((line for line in info.splitlines() if "Video:" in line), "")
    size_match = re.search(r"\b(\d{2,5})x(\d{2,5})\b", video_line)
    if not duration_match or not size_match:
        raise RuntimeError("Couldn't read video metadata. Try a different file or URL.")
    h, m, s = duration_match.groups()
    duration = 3600 * int(h) + 60 * int(m) + float(s)
    if duration <= 0:
        raise RuntimeError("Video duration must be greater than zero.")
    return duration, int(size_match.group(1)), int(size_match.group(2))


def size_filter(width: int, height: int | None, mode: str) -> str:
    if height is None:
        return f"scale={width}:-2:flags=lanczos"
    if mode == "Fill (crop)":
        return (f"scale={width}:{height}:force_original_aspect_ratio=increase:"
                f"flags=lanczos,crop={width}:{height}")
    if mode == "Stretch":
        return f"scale={width}:{height}:flags=lanczos"
    return (f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
            f"flags=lanczos,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x0A1020")


@dataclass(frozen=True)
class ExportJob:
    source: Path
    output: Path
    start: float
    end: float
    width: int
    height: int | None
    sizing: str
    fps: int
    speed: float
    dither: str
    infinite_loop: bool

    @property
    def output_duration(self) -> float:
        return (self.end - self.start) / self.speed

    def make_filters(self) -> str:
        # setpts first means output FPS always denotes frames per OUTPUT second.
        return (f"setpts=(PTS-STARTPTS)/{self.speed:.5f},"
                f"fps={self.fps},{size_filter(self.width, self.height, self.sizing)}")


# ------ Application ----------------------------------------------------------
class LoopCraftApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("LoopCraft  |  YouTube to GIF Studio")
        root.geometry("1140x950")
        root.minsize(1115, 930)
        root.configure(bg=BG)

        self.ffmpeg = get_ffmpeg()
        self.tmp = tempfile.TemporaryDirectory(prefix="loopcraft_")
        self.events: queue.Queue = queue.Queue()
        self.frames: queue.Queue = queue.Queue(maxsize=3)
        self.cancel = threading.Event()
        self.busy: str | None = None
        self.source_generation = 0
        self.still_generation = 0
        self.play_generation = 0
        self.play_stop = threading.Event()
        self.process_lock = threading.Lock()
        self.active_process: subprocess.Popen | None = None
        self.preview_process: subprocess.Popen | None = None
        self.playing = False
        self.source: Path | None = None
        self.video_title = ""
        self.duration = 0.0
        self.source_width = 0
        self.source_height = 0
        self.in_point = 0.0
        self.out_point = 0.0
        self.head = 0.0
        self.dragging: str | None = None
        self.still_after: str | None = None
        self.thumb_photos: list[ImageTk.PhotoImage | None] = [None] * THUMB_COUNT
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.recent_output: Path | None = None

        self.url_var = tk.StringVar()
        self.in_var = tk.StringVar(value="00:00.0")
        self.out_var = tk.StringVar(value="00:06.0")
        self.position_var = tk.StringVar(value="00:00.0 / 00:00.0")
        self.title_var = tk.StringVar(value="No video loaded")
        self.meta_var = tk.StringVar(value="OPEN A VIDEO TO START")
        self.status_var = tk.StringVar(value="Ready · Paste a YouTube URL or open a video file")
        self.res_var = tk.StringVar(value="480")
        self.width_var = tk.StringVar(value="480")
        self.height_var = tk.StringVar(value="")
        self.sizing_var = tk.StringVar(value="Fit (pad)")
        self.fps_var = tk.StringVar(value="12")
        self.speed_var = tk.DoubleVar(value=1.0)
        self.dither_var = tk.StringVar(value="Balanced")
        self.loop_var = tk.BooleanVar(value=True)
        self.output_var = tk.StringVar(value=str(Path.home() / "Videos" / "loopcraft.gif"))
        self.selection_var = tk.StringVar(value="Select a video to begin")

        self._configure_style()
        self._build_ui()
        self._draw_timeline()
        self._draw_placeholder()
        self._render_settings_summary()
        root.after(45, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.bind("<Control-o>", lambda _: self.load_local())
        root.bind("<Control-Return>", lambda _: self.export())
        root.bind("<space>", self._space_key)
        root.bind("<Left>", lambda e: self._keyboard_seek(-5 if e.state & 1 else -0.5))
        root.bind("<Right>", lambda e: self._keyboard_seek(5 if e.state & 1 else 0.5))

    def _configure_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Dark.TCombobox", foreground=TEXT, fieldbackground=PANEL_ALT,
                        background=PANEL_ALT, selectbackground=PURPLE_DARK,
                        selectforeground=TEXT, padding=8, borderwidth=1)
        style.map("Dark.TCombobox", fieldbackground=[("readonly", PANEL_ALT)],
                  foreground=[("readonly", TEXT)])
        style.configure("Dark.Horizontal.TProgressbar", troughcolor=PANEL_ALT,
                        background=PURPLE, borderwidth=0, thickness=9)
        style.configure("Dark.TSpinbox", fieldbackground=PANEL_ALT,
                        foreground=TEXT, background=PANEL_ALT, arrowsize=12)

    def _label(self, parent, text, size=10, color=TEXT, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kwargs)

    def _button(self, parent, text, command, accent=False, width=None):
        color = PURPLE if accent else BORDER
        hover = PURPLE_DARK if accent else "#384961"
        btn = tk.Button(parent, text=text, command=command, bg=color, fg=TEXT,
                        activebackground=hover, activeforeground=TEXT,
                        highlightthickness=0, bd=0, relief="flat", cursor="hand2",
                        font=("Segoe UI", 10, "bold"), padx=13, pady=10, width=width)
        btn.bind("<Enter>", lambda _: btn.configure(bg=hover) if str(btn["state"]) != "disabled" else None)
        btn.bind("<Leave>", lambda _: btn.configure(bg=color) if str(btn["state"]) != "disabled" else None)
        return btn

    def _entry(self, parent, variable, width=None):
        return tk.Entry(parent, textvariable=variable, bg=PANEL_ALT, fg=TEXT,
                        insertbackground=AQUA, selectbackground=PURPLE_DARK,
                        relief="flat", highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=PURPLE,
                        font=("Segoe UI", 10), width=width or 12, bd=5)

    def _panel(self, parent):
        return tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)

    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=25, pady=(17, 9))
        self._label(header, "◈", 23, AQUA, bold=True).pack(side="left", padx=(0, 9))
        self._label(header, "LOOPCRAFT", 20, TEXT, bold=True).pack(side="left")
        self._label(header, "VIDEO  →  GIF STUDIO", 10, MUTED, bold=True).pack(side="left", padx=18, pady=7)
        self._label(header, "LOCAL PROCESSING  •  NO WATERMARK", 9, AQUA).pack(side="right")

        source_box = self._panel(self.root)
        source_box.pack(fill="x", padx=25, pady=(0, 12))
        source_inner = tk.Frame(source_box, bg=PANEL)
        source_inner.pack(fill="x", padx=15, pady=12)
        self._label(source_inner, "SOURCE", 9, MUTED, True).pack(side="left", padx=(0, 10))
        self.url_entry = self._entry(source_inner, self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 9))
        self.url_entry.insert(0, "")
        self.url_entry.bind("<Return>", lambda _: self.load_url())
        self.local_button = self._button(source_inner, "Open file", self.load_local)
        self.local_button.pack(side="right", padx=(8, 0))
        self.load_button = self._button(source_inner, "Load URL", self.load_url, accent=True)
        self.load_button.pack(side="right")

        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=25, pady=(0, 11))
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=0)
        main.grid_rowconfigure(0, weight=1)

        left = self._panel(main)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = self._panel(main)
        right.configure(width=340)
        right.grid(row=0, column=1, sticky="ns")
        right.grid_propagate(False)

        p_heading = tk.Frame(left, bg=PANEL)
        p_heading.pack(fill="x", padx=15, pady=(14, 8))
        self._label(p_heading, "PREVIEW", 10, AQUA, True).pack(side="left")
        self._label(p_heading, "drag IN / OUT handles below", 9, MUTED).pack(side="right")
        self.preview_canvas = tk.Canvas(left, width=PREVIEW_SIZE[0], height=PREVIEW_SIZE[1],
                                        bg=CANVAS_BG, bd=0, highlightthickness=0)
        self.preview_canvas.pack(padx=13, fill="x")
        self.preview_canvas.bind("<Button-1>", lambda _: self.toggle_play())

        title_row = tk.Frame(left, bg=PANEL)
        title_row.pack(fill="x", padx=17, pady=(10, 2))
        self._label(title_row, "", 11, TEXT, True, textvariable=self.title_var,
                    anchor="w").pack(side="left", fill="x", expand=True)
        self._label(title_row, "", 9, MUTED, textvariable=self.meta_var,
                    anchor="e").pack(side="right")

        controls = tk.Frame(left, bg=PANEL)
        controls.pack(fill="x", padx=15, pady=(10, 11))
        self.play_button = self._button(controls, "▶  Play selection", self.toggle_play, accent=True)
        self.play_button.pack(side="left", padx=(0, 9))
        self._button(controls, "|◀  IN", lambda: self.seek(self.in_point)).pack(side="left", padx=(0, 6))
        self._button(controls, "OUT  ▶|", lambda: self.seek(max(self.in_point, self.out_point - 0.05))).pack(side="left")
        self._label(controls, "", 10, MUTED, textvariable=self.position_var,
                    anchor="e").pack(side="right", fill="x", expand=True)

        self.timeline = tk.Canvas(left, height=TIMELINE_HEIGHT, width=TIMELINE_WIDTH,
                                  bg=PANEL_ALT, bd=0, highlightthickness=0,
                                  cursor="crosshair")
        self.timeline.pack(padx=13, fill="x", pady=(0, 10))
        self.timeline.bind("<Button-1>", self._on_timeline_press)
        self.timeline.bind("<B1-Motion>", self._on_timeline_drag)
        self.timeline.bind("<ButtonRelease-1>", self._on_timeline_release)
        self.timeline.bind("<Configure>", lambda _: self._draw_timeline())

        cut = tk.Frame(left, bg=PANEL)
        cut.pack(fill="x", padx=17, pady=(0, 12))
        c1 = tk.Frame(cut, bg=PANEL)
        c1.pack(side="left", padx=(0, 15))
        self._label(c1, "IN", 9, AQUA, True).pack(side="left", padx=(0, 8))
        in_entry = self._entry(c1, self.in_var, width=10)
        in_entry.pack(side="left")
        in_entry.bind("<Return>", lambda _: self.apply_times())
        in_entry.bind("<FocusOut>", lambda _: self.apply_times(silent=True))
        c2 = tk.Frame(cut, bg=PANEL)
        c2.pack(side="left")
        self._label(c2, "OUT", 9, PURPLE, True).pack(side="left", padx=(0, 8))
        out_entry = self._entry(c2, self.out_var, width=10)
        out_entry.pack(side="left")
        out_entry.bind("<Return>", lambda _: self.apply_times())
        out_entry.bind("<FocusOut>", lambda _: self.apply_times(silent=True))
        self._label(cut, "", 9, MUTED, textvariable=self.selection_var).pack(side="right")

        # Right-hand export control rack
        inner = tk.Frame(right, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=16, pady=(12, 10))
        self._label(inner, "EXPORT SETTINGS", 11, AQUA, True).pack(anchor="w", pady=(0, 10))
        self._label(inner, "RESOLUTION  ·  OUTPUT WIDTH", 9, MUTED, True).pack(anchor="w")
        preset = ttk.Combobox(inner, state="readonly", textvariable=self.res_var,
                              values=("240", "320", "480", "640", "720", "1080", "Custom"),
                              style="Dark.TCombobox", width=22)
        preset.pack(fill="x", pady=(4, 8))
        preset.bind("<<ComboboxSelected>>", self._preset_changed)
        sizes = tk.Frame(inner, bg=PANEL)
        sizes.pack(fill="x")
        wc = tk.Frame(sizes, bg=PANEL)
        wc.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._label(wc, "WIDTH (px)", 8, MUTED).pack(anchor="w", pady=(0, 5))
        self._entry(wc, self.width_var).pack(fill="x", ipady=5)
        hc = tk.Frame(sizes, bg=PANEL)
        hc.pack(side="left", fill="x", expand=True)
        self._label(hc, "HEIGHT (blank = auto)", 8, MUTED).pack(anchor="w", pady=(0, 5))
        self._entry(hc, self.height_var).pack(fill="x", ipady=5)
        self._label(inner, "ASPECT HANDLING", 9, MUTED, True).pack(anchor="w", pady=(8, 3))
        ttk.Combobox(inner, state="readonly", textvariable=self.sizing_var,
                     values=("Fit (pad)", "Fill (crop)", "Stretch"),
                     style="Dark.TCombobox").pack(fill="x")

        sep = tk.Frame(inner, bg=BORDER, height=1)
        sep.pack(fill="x", pady=9)
        self._label(inner, "FRAME RATE", 9, MUTED, True).pack(anchor="w")
        fps_row = tk.Frame(inner, bg=PANEL)
        fps_row.pack(fill="x", pady=(4, 8))
        fps_spin = ttk.Spinbox(fps_row, from_=2, to=60, textvariable=self.fps_var,
                               width=7, style="Dark.TSpinbox")
        fps_spin.pack(side="left")
        self._label(fps_row, "frames / second", 9, MUTED).pack(side="left", padx=10)
        self._label(inner, "PLAYBACK SPEED", 9, MUTED, True).pack(anchor="w")
        speed_row = tk.Frame(inner, bg=PANEL)
        speed_row.pack(fill="x")
        self.speed_label = self._label(speed_row, "1.00×", 12, TEXT, True)
        self.speed_label.pack(side="right")
        tk.Scale(speed_row, from_=0.25, to=4.0, resolution=0.25, orient="horizontal",
                 variable=self.speed_var, command=lambda _: self._speed_changed(),
                 bg=PANEL, fg=MUTED, troughcolor=PANEL_ALT, activebackground=PURPLE,
                 highlightthickness=0, bd=0, showvalue=False, sliderlength=16,
                 length=230).pack(side="left", fill="x", expand=True)
        self._label(inner, "0.25× slow motion     •     4× fast", 8, MUTED).pack(anchor="w", pady=(0, 8))

        self._label(inner, "GIF COLOR PROFILE", 9, MUTED, True).pack(anchor="w")
        ttk.Combobox(inner, state="readonly", textvariable=self.dither_var,
                     values=("Balanced", "Smooth gradients", "Smaller file"),
                     style="Dark.TCombobox").pack(fill="x", pady=(5, 9))
        tk.Checkbutton(inner, text="Loop forever", variable=self.loop_var,
                       bg=PANEL, fg=TEXT, activebackground=PANEL,
                       activeforeground=TEXT, selectcolor=PANEL_ALT,
                       font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 5))

        self._label(inner, "SAVE AS", 9, MUTED, True).pack(anchor="w")
        path_row = tk.Frame(inner, bg=PANEL)
        path_row.pack(fill="x", pady=(6, 9))
        self._entry(path_row, self.output_var).pack(side="left", fill="x", expand=True,
                                                    ipady=5, padx=(0, 5))
        self._button(path_row, "…", self.choose_save).pack(side="right")
        self.export_button = self._button(inner, "✦   CREATE GIF", self.export, accent=True)
        self.export_button.pack(fill="x", pady=(2, 7), ipady=5)
        self._label(inner, "", 9, MUTED, textvariable=self.selection_var,
                    wraplength=292).pack(anchor="center")
        self.open_button = self._button(inner, "Open last GIF", self.open_last)
        self.open_button.pack(fill="x", pady=(12, 0))
        self.open_button.configure(state="disabled")

        footer = tk.Frame(self.root, bg=BG)
        footer.pack(side="bottom", fill="x", padx=25, pady=(0, 13))
        status_row = tk.Frame(footer, bg=BG)
        status_row.pack(fill="x", pady=(0, 5))
        self._label(status_row, "", 9, MUTED, textvariable=self.status_var,
                    anchor="w").pack(side="left", fill="x", expand=True)
        self.cancel_button = self._button(status_row, "Cancel task", self.cancel_task)
        self.cancel_button.pack(side="right")
        self.cancel_button.configure(state="disabled")
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=100,
                                        style="Dark.Horizontal.TProgressbar")
        self.progress.pack(fill="x")
        # Reserve the status/progress strip before allocating expandable editor space.
        main.pack_forget()
        main.pack(fill="both", expand=True, padx=25, pady=(0, 11))

        for v in (self.width_var, self.height_var, self.fps_var,
                  self.sizing_var, self.dither_var):
            v.trace_add("write", lambda *_: self._render_settings_summary())

    def _draw_placeholder(self):
        c = self.preview_canvas
        c.delete("all")
        w, h = PREVIEW_SIZE
        c.create_oval(w / 2 - 52, h / 2 - 80, w / 2 + 52, h / 2 + 24,
                      outline=BORDER, width=2)
        c.create_text(w / 2, h / 2 - 28, text="▶", fill=AQUA,
                      font=("Segoe UI Symbol", 29))
        c.create_text(w / 2, h / 2 + 57, text="YOUR NEXT PERFECT LOOP STARTS HERE",
                      fill=TEXT, font=("Segoe UI", 13, "bold"))
        c.create_text(w / 2, h / 2 + 82, text="Import any local video or paste a YouTube link",
                      fill=MUTED, font=("Segoe UI", 10))

    def _set_preview(self, img: Image.Image):
        img = img.convert("RGB")
        if img.size != PREVIEW_SIZE:
            img = img.resize(PREVIEW_SIZE, Image.Resampling.BILINEAR)
        self.preview_photo = ImageTk.PhotoImage(img)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(PREVIEW_SIZE[0] / 2, PREVIEW_SIZE[1] / 2,
                                         image=self.preview_photo, anchor="center")

    # ------ Loading -----------------------------------------------------------
    def load_url(self):
        if self.busy:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Paste a YouTube video URL first.")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            messagebox.showwarning("Invalid URL", "Enter a full https:// YouTube URL.")
            return
        if yt_dlp is None:
            messagebox.showerror("Install yt-dlp", "Install yt-dlp with:\n\npy -m pip install -U yt-dlp")
            return
        self._begin_source()
        threading.Thread(target=self._youtube_worker,
                         args=(url, self.source_generation), daemon=True).start()

    def load_local(self):
        if self.busy:
            return
        file = filedialog.askopenfilename(title="Open a video", filetypes=[
            ("Video files", "*.mp4 *.mkv *.mov *.webm *.avi *.m4v *.flv *.wmv"),
            ("All files", "*.*")])
        if not file:
            return
        self._begin_source()
        threading.Thread(target=self._local_worker,
                         args=(Path(file), self.source_generation), daemon=True).start()

    def _begin_source(self):
        self._stop_playback()
        self.source_generation += 1
        self.source = None
        self.thumb_photos = [None] * THUMB_COUNT
        self.duration = 0
        self.busy = "load"
        self.cancel.clear()
        self._set_busy(True)
        self.progress.configure(mode="indeterminate")
        self.progress.start(14)
        self.status_var.set("Reading source…")

    def _local_worker(self, path: Path, generation: int):
        try:
            duration, width, height = probe_video(self.ffmpeg, path)
            if self.cancel.is_set():
                self.events.put(("cancelled", generation))
            else:
                self.events.put(("loaded", generation, path, path.stem, duration, width, height))
        except Exception as exc:
            self.events.put(("error", generation, str(exc)))

    def _youtube_worker(self, url: str, generation: int):
        def hook(progress):
            if self.cancel.is_set():
                raise RuntimeError("Download cancelled")
            if progress.get("status") == "downloading":
                total = progress.get("total_bytes") or progress.get("total_bytes_estimate")
                got = progress.get("downloaded_bytes") or 0
                if total:
                    self.events.put(("download", generation, min(100, got * 100 / total)))
            elif progress.get("status") == "finished":
                self.events.put(("status", generation, "Downloaded · preparing preview…"))

        try:
            staging = Path(self.tmp.name) / f"video_{generation}"
            staging.mkdir(exist_ok=True)
            opts = {
                # GIFs have no audio, so download a video-only stream, avoiding merging.
                "format": "bestvideo[height<=1080]/best[height<=1080]/bestvideo/best",
                "outtmpl": str(staging / "source.%(ext)s"),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [hook],
                "retries": 5,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                metadata = ydl.extract_info(url, download=False)
                if metadata.get("is_live") or metadata.get("live_status") == "is_live":
                    raise RuntimeError("Live streams are not supported. Use a recorded video.")
                if self.cancel.is_set():
                    self.events.put(("cancelled", generation))
                    return
                title = metadata.get("title") or "YouTube video"
                self.events.put(("status", generation, f"Downloading: {title[:65]}"))
                ydl.download([url])

            if self.cancel.is_set():
                self.events.put(("cancelled", generation))
                return
            files = [p for p in staging.iterdir() if p.is_file() and not p.name.endswith(
                (".part", ".ytdl", ".temp", ".json"))]
            if not files:
                raise RuntimeError("Download finished but the video file was not found.")
            path = max(files, key=lambda x: x.stat().st_size)
            duration, width, height = probe_video(self.ffmpeg, path)
            if self.cancel.is_set():
                self.events.put(("cancelled", generation))
            else:
                self.events.put(("loaded", generation, path, title, duration, width, height))
        except Exception as exc:
            self.events.put(("error", generation, str(exc)))

    def _source_ready(self, path: Path, title: str, duration: float, width: int, height: int):
        self.source = path
        self.video_title = title
        self.duration = duration
        self.source_width, self.source_height = width, height
        self.in_point = 0
        self.out_point = min(6.0, duration)
        self.head = 0
        self._update_time_fields()
        self.title_var.set(title[:56] + ("…" if len(title) > 56 else ""))
        self.meta_var.set(f"{width} × {height}  ·  {readable_time(duration, False)}")
        suggested = Path.home() / "Videos"
        if not suggested.is_dir():
            suggested = Path.home()
        self.output_var.set(str(suggested / f"{file_safe_name(title)}.gif"))
        self._finish_busy()
        self.status_var.set("Ready · Drag the trim handles, adjust settings, then Create GIF")
        self.thumb_photos = [None] * THUMB_COUNT
        self._draw_timeline()
        self.seek(0)
        threading.Thread(target=self._thumbnails_worker,
                         args=(path, self.source_generation), daemon=True).start()

    # ------ Preview and timeline ---------------------------------------------
    def _thumbnail_command(self, source: Path, pos: float, target_width: int, target_height: int):
        return [self.ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0, pos):.3f}", "-i", str(source),
                "-frames:v", "1", "-vf",
                (f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                 f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=0x0A1020"),
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]

    def _thumbnails_worker(self, source: Path, generation: int):
        # Video does not change while loading this generation.
        for idx in range(THUMB_COUNT):
            if generation != self.source_generation or self.cancel.is_set() or self.source != source:
                return
            pos = self.duration * (idx + 0.5) / THUMB_COUNT
            command = self._thumbnail_command(source, pos, 120, 77)
            try:
                out = subprocess.run(command, capture_output=True, timeout=20,
                                     **popen_options())
                if out.returncode == 0 and out.stdout:
                    self.events.put(("thumb", generation, idx, out.stdout))
            except (subprocess.SubprocessError, OSError):
                pass

    def _schedule_still(self, sec: float):
        self.still_generation += 1
        token = self.still_generation
        if self.still_after:
            self.root.after_cancel(self.still_after)
        source = self.source
        self.still_after = self.root.after(
            130, lambda: threading.Thread(target=self._still_worker,
                                          args=(source, sec, token, self.source_generation),
                                          daemon=True).start())

    def _still_worker(self, source: Path | None, sec: float, token: int, source_generation: int):
        if source is None or source_generation != self.source_generation:
            return
        try:
            run = subprocess.run(self._thumbnail_command(
                source, min(sec, max(0, self.duration - 0.06)), *PREVIEW_SIZE),
                capture_output=True, timeout=25, **popen_options())
            if run.returncode == 0 and run.stdout:
                self.events.put(("still", source_generation, token, run.stdout))
        except (subprocess.SubprocessError, OSError):
            pass

    def seek(self, sec: float):
        if not self.source:
            return
        self._stop_playback()
        self.head = max(0, min(self.duration, sec))
        self._update_time_fields()
        self._draw_timeline()
        self._schedule_still(self.head)

    def _play_worker(self, token: int, stop: threading.Event, begin: float,
                     duration: float, speed: float, source: Path):
        w, h = PREVIEW_SIZE
        vf = (f"setpts=(PTS-STARTPTS)/{speed:.5f},fps={FRAME_FPS},"
              f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0A1020")
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
               "-ss", f"{begin:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
               "-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0,
                                    **popen_options())
            with self.process_lock:
                if stop.is_set():
                    proc.kill()
                    return
                self.preview_process = proc
            framebytes = w * h * 3
            start_clock = time.monotonic()
            idx = 0
            while not stop.is_set():
                if idx / FRAME_FPS >= duration / speed + 1 / FRAME_FPS:
                    break
                raw = bytearray(framebytes)
                view = memoryview(raw)
                offset = 0
                while offset < framebytes and not stop.is_set():
                    chunk = proc.stdout.read(framebytes - offset)
                    if not chunk:
                        break
                    view[offset:offset + len(chunk)] = chunk
                    offset += len(chunk)
                if offset < framebytes:
                    break
                target = start_clock + idx / FRAME_FPS
                delay = target - time.monotonic()
                if delay > 0 and stop.wait(delay):
                    break
                frame_event = ("frame", token, begin + idx * speed / FRAME_FPS, raw)
                try:
                    self.frames.put_nowait(frame_event)
                except queue.Full:
                    try:
                        self.frames.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.frames.put_nowait(frame_event)
                    except queue.Full:
                        pass
                idx += 1
        except (OSError, ValueError):
            pass
        finally:
            if proc:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.SubprocessError:
                    pass
                with self.process_lock:
                    if self.preview_process is proc:
                        self.preview_process = None
            self.events.put(("play_end", token))

    def toggle_play(self):
        if not self.source or self.busy:
            return
        if self.playing:
            self._stop_playback()
            return
        self.still_generation += 1  # ignore any stale seek still frames
        self.play_generation += 1
        token = self.play_generation
        self.play_stop = threading.Event()
        self.playing = True
        self.play_button.configure(text="❚❚  Pause preview")
        start = self.head if self.in_point <= self.head < self.out_point - 0.15 else self.in_point
        self.head = start
        threading.Thread(target=self._play_worker,
                         args=(token, self.play_stop, start,
                               self.out_point - start, self.speed_var.get(), self.source),
                         daemon=True).start()

    def _stop_playback(self):
        self.play_generation += 1
        self.play_stop.set()
        self.playing = False
        if hasattr(self, "play_button"):
            self.play_button.configure(text="▶  Play selection")
        with self.process_lock:
            if self.preview_process:
                try:
                    self.preview_process.kill()
                except OSError:
                    pass

    def _space_key(self, event):
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox, ttk.Combobox)):
            return
        self.toggle_play()

    def _keyboard_seek(self, delta: float):
        if self.root.focus_get() and isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry, ttk.Spinbox)):
            return
        self.seek(self.head + delta)

    def _coords(self):
        width = max(100, int(self.timeline.winfo_width()))
        return 10, width - 10

    def _time_to_x(self, value: float) -> float:
        x0, x1 = self._coords()
        return x0 + (x1 - x0) * value / max(0.001, self.duration)

    def _x_to_time(self, x: float) -> float:
        x0, x1 = self._coords()
        return max(0, min(self.duration, (x - x0) / max(1, x1 - x0) * self.duration))

    def _draw_timeline(self):
        if not hasattr(self, "timeline"):
            return
        c = self.timeline
        c.delete("all")
        x0, x1 = self._coords()
        width = x1 - x0
        for i in range(THUMB_COUNT):
            a = x0 + width * i / THUMB_COUNT
            b = x0 + width * (i + 1) / THUMB_COUNT
            c.create_rectangle(a, 8, b, 77, fill=CANVAS_BG, outline=BORDER)
            if self.thumb_photos[i]:
                c.create_image(a, 8, image=self.thumb_photos[i], anchor="nw")
        if not self.source or self.duration <= 0:
            c.create_text((x0 + x1) / 2, 40, text="Your timeline will appear here",
                          fill=MUTED, font=("Segoe UI", 10))
            return
        a, b = self._time_to_x(self.in_point), self._time_to_x(self.out_point)
        c.create_rectangle(x0, 8, a, 77, fill="#050A13", stipple="gray50", outline="")
        c.create_rectangle(b, 8, x1, 77, fill="#050A13", stipple="gray50", outline="")
        c.create_rectangle(a, 7, b, 78, outline=AQUA, width=2)
        for x, col in ((a, AQUA), (b, PURPLE)):
            c.create_rectangle(x - 5, 6, x + 5, 79, fill=col, outline="")
            c.create_line(x - 1, 34, x - 1, 52, fill=PANEL, width=1)
            c.create_line(x + 2, 34, x + 2, 52, fill=PANEL, width=1)
        h = self._time_to_x(self.head)
        c.create_line(h, 4, h, 80, fill=YELLOW, width=2)
        c.create_polygon(h - 5, 0, h + 5, 0, h, 7, fill=YELLOW, outline="")
        for i in range(5):
            sec = self.duration * i / 4
            x = self._time_to_x(sec)
            c.create_text(x, 94, text=readable_time(sec, False), fill=MUTED,
                          anchor="w" if i == 0 else "e" if i == 4 else "center",
                          font=("Segoe UI", 9))

    def _on_timeline_press(self, event):
        if not self.source:
            return
        x = event.x
        if abs(x - self._time_to_x(self.in_point)) < 13:
            self.dragging = "in"
        elif abs(x - self._time_to_x(self.out_point)) < 13:
            self.dragging = "out"
        else:
            self.dragging = "head"
            self.seek(self._x_to_time(x))

    def _on_timeline_drag(self, event):
        if not self.source or not self.dragging:
            return
        sec = self._x_to_time(event.x)
        if self.dragging == "in":
            self.in_point = min(max(0, sec), self.out_point - 0.1)
            self.head = self.in_point
        elif self.dragging == "out":
            self.out_point = max(min(self.duration, sec), self.in_point + 0.1)
            self.head = min(self.out_point, self.head)
        else:
            self.head = sec
        self._update_time_fields()
        self._draw_timeline()

    def _on_timeline_release(self, event):
        if not self.dragging:
            return
        self.dragging = None
        self.seek(self.head)

    def _update_time_fields(self):
        self.in_var.set(readable_time(self.in_point))
        self.out_var.set(readable_time(self.out_point))
        self.position_var.set(f"{readable_time(self.head)} / {readable_time(self.duration)}")
        self._render_settings_summary()

    def apply_times(self, silent: bool = False) -> bool:
        if not self.source:
            return False
        try:
            a, b = parse_time(self.in_var.get()), parse_time(self.out_var.get())
            if a < 0 or b > self.duration + 0.05 or b - a < 0.1:
                raise ValueError("IN must be before OUT and within the video (min. 0.1s).")
            self.in_point = a
            self.out_point = min(self.duration, b)
            self.head = self.in_point
            self.seek(self.head)
            return True
        except ValueError as exc:
            if not silent:
                messagebox.showerror("Invalid trim points", str(exc))
            self._update_time_fields()
            return False

    def _preset_changed(self, event=None):
        if self.res_var.get() != "Custom":
            self.width_var.set(self.res_var.get())
            self.height_var.set("")
        self._render_settings_summary()

    def _speed_changed(self):
        if self.playing:
            self._stop_playback()
        self.speed_label.configure(text=f"{self.speed_var.get():.2f}×")
        self._render_settings_summary()

    def _render_settings_summary(self):
        if not hasattr(self, "selection_var"):
            return
        if not self.duration:
            self.selection_var.set("Select a video to begin")
        else:
            duration = max(0, (self.out_point - self.in_point) / self.speed_var.get())
            try:
                fps = int(self.fps_var.get())
            except ValueError:
                fps = 0
            self.selection_var.set(f"{duration:.1f}s GIF   ·   ~{math.ceil(duration * fps)} frames")

    # ------ Export ------------------------------------------------------------
    def choose_save(self):
        name = filedialog.asksaveasfilename(title="Save animated GIF",
                 initialdir=str(Path(self.output_var.get()).parent),
                 initialfile=Path(self.output_var.get()).name,
                 defaultextension=".gif", filetypes=[("Animated GIF", "*.gif")])
        if name:
            self.output_var.set(name)

    def _job_from_ui(self) -> ExportJob:
        if not self.source:
            raise ValueError("Load a video before exporting.")
        if not self.apply_times(silent=True):
            raise ValueError("Invalid IN/OUT times. Use MM:SS and keep OUT after IN.")
        width = int(self.width_var.get())
        height = int(self.height_var.get()) if self.height_var.get().strip() else None
        fps = int(self.fps_var.get())
        if not 32 <= width <= 1920 or (height is not None and not 32 <= height <= 1920):
            raise ValueError("Width and height must be between 32 and 1920 pixels.")
        if not 2 <= fps <= 60:
            raise ValueError("FPS must be between 2 and 60.")
        if self.out_point - self.in_point < 0.1:
            raise ValueError("Select at least 0.1 seconds of footage.")
        out = Path(self.output_var.get().strip()).expanduser()
        if not out.name:
            raise ValueError("Choose a destination for your GIF.")
        if out.suffix.lower() != ".gif":
            out = out.with_suffix(".gif")
        if not out.parent.is_dir():
            raise ValueError(f"Output folder doesn't exist: {out.parent}")
        return ExportJob(self.source, out, self.in_point, self.out_point,
                         width, height, self.sizing_var.get(), fps,
                         self.speed_var.get(), self.dither_var.get(),
                         self.loop_var.get())

    def export(self):
        if self.busy:
            return
        try:
            job = self._job_from_ui()
        except ValueError as exc:
            messagebox.showerror("Check settings", str(exc))
            return
        if job.output.exists() and not messagebox.askyesno(
                "Replace GIF?", f"This file already exists:\n{job.output}\n\nReplace it?"):
            return
        if job.output_duration > 60 or job.output_duration * job.fps > 1200:
            if not messagebox.askyesno("Large GIF warning",
                    f"This will create about {math.ceil(job.output_duration * job.fps)} frames.\n"
                    "GIF files can become very large. Continue?"):
                return
        self._stop_playback()
        self.busy = "export"
        self.cancel.clear()
        self._set_busy(True)
        self.status_var.set("Pass 1/2 · Analyzing colors…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        threading.Thread(target=self._export_worker, args=(job,), daemon=True).start()

    def _run_ffmpeg(self, command: list[str], stage: int, expected_duration: float):
        errors = []
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   errors="replace", bufsize=1, **popen_options())
        with self.process_lock:
            self.active_process = process
            if self.cancel.is_set():
                process.kill()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if self.cancel.is_set():
                    process.kill()
                    break
                line = line.strip()
                if line.startswith("out_time="):
                    try:
                        elapsed = parse_time(line.partition("=")[2])
                        percent = max(0, min(100, 100 * elapsed / max(0.001, expected_duration)))
                        self.events.put(("encode_progress", stage, percent))
                    except ValueError:
                        pass
                elif line and not line.startswith(("frame=", "fps=", "stream_", "bitrate=",
                                                    "total_size=", "out_time_", "speed=", "progress=")):
                    errors.append(line)
                    errors = errors[-15:]
            rc = process.wait()
            if self.cancel.is_set():
                raise RuntimeError("Export cancelled")
            if rc != 0:
                raise RuntimeError("FFmpeg failed:\n" + "\n".join(errors[-7:]))
        finally:
            with self.process_lock:
                if self.active_process is process:
                    self.active_process = None

    def _export_worker(self, job: ExportJob):
        palette_path = Path(self.tmp.name) / "palette.png"
        staged_output = job.output.parent / (job.output.stem + ".loopcraft-part.gif")
        filters = job.make_filters()
        dither = {"Balanced": "sierra2_4a", "Smooth gradients": "floyd_steinberg",
                  "Smaller file": "bayer:bayer_scale=3"}.get(job.dither, "sierra2_4a")
        try:
            # Two passes prevent large buffer spikes from split+palettegen graphs.
            palette_cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                           "-ss", f"{job.start:.3f}", "-t", f"{job.end - job.start:.3f}",
                           "-i", str(job.source), "-vf",
                           f"{filters},palettegen=stats_mode=diff", "-frames:v", "1",
                           "-progress", "pipe:1", "-nostats", str(palette_path)]
            self._run_ffmpeg(palette_cmd, 1, job.output_duration)
            self.events.put(("status_export", "Pass 2/2 · Rendering GIF with optimized palette…"))
            second_cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                          "-ss", f"{job.start:.3f}", "-t", f"{job.end - job.start:.3f}",
                          "-i", str(job.source), "-i", str(palette_path),
                          "-filter_complex",
                          (f"[0:v]{filters}[v];[v][1:v]"
                           f"paletteuse=dither={dither}:diff_mode=rectangle[out]"),
                          "-map", "[out]", "-loop", "0" if job.infinite_loop else "-1",
                          "-progress", "pipe:1", "-nostats", str(staged_output)]
            self._run_ffmpeg(second_cmd, 2, job.output_duration)
            if self.cancel.is_set():
                raise RuntimeError("Export cancelled")
            staged_output.replace(job.output)
            size_mib = job.output.stat().st_size / (1024 * 1024)
            self.events.put(("export_done", job.output, size_mib))
        except Exception as exc:
            self.events.put(("export_error", str(exc)))
        finally:
            palette_path.unlink(missing_ok=True)
            staged_output.unlink(missing_ok=True)

    def cancel_task(self):
        if not self.busy:
            return
        self.cancel.set()
        self.status_var.set("Cancelling…")
        with self.process_lock:
            if self.active_process:
                try:
                    self.active_process.kill()
                except OSError:
                    pass
        # yt-dlp checks cancel on every download progress hook.
        # Load may stop between network requests; release UI after worker exits.

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for w in (self.load_button, self.local_button, self.export_button):
            w.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def _finish_busy(self):
        self.busy = None
        self.cancel.clear()
        self._set_busy(False)
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress["value"] = 0

    def _poll(self):
        try:
            for _ in range(65):
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                kind = event[0]
                if kind == "cancelled" and event[1] == self.source_generation and self.busy == "load":
                    self._finish_busy()
                    self.status_var.set("Load cancelled")
                elif kind == "loaded" and event[1] == self.source_generation:
                    if self.cancel.is_set():
                        self._finish_busy()
                        self.status_var.set("Load cancelled")
                    else:
                        self._source_ready(*event[2:])
                elif kind == "error" and event[1] == self.source_generation:
                    cancelled = self.cancel.is_set()
                    self._finish_busy()
                    self.status_var.set("Load cancelled" if cancelled else "Loading failed")
                    if not cancelled:
                        messagebox.showerror("Couldn't load video", event[2])
                elif kind == "download" and event[1] == self.source_generation and self.busy == "load":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = event[2]
                elif kind == "status" and event[1] == self.source_generation:
                    self.status_var.set(event[2])
                elif kind == "thumb" and event[1] == self.source_generation and self.source:
                    try:
                        img = Image.open(io.BytesIO(event[3])).convert("RGB")
                        x0, x1 = self._coords()
                        width = int((x1 - x0) / THUMB_COUNT)
                        img = img.resize((width, 69), Image.Resampling.BILINEAR)
                        self.thumb_photos[event[2]] = ImageTk.PhotoImage(img)
                        self._draw_timeline()
                    except (OSError, ValueError):
                        pass
                elif kind == "still" and event[1] == self.source_generation and event[2] == self.still_generation and not self.playing:
                    try:
                        self._set_preview(Image.open(io.BytesIO(event[3])))
                    except OSError:
                        pass
                elif kind == "play_end" and event[1] == self.play_generation and self.playing:
                    self._stop_playback()
                    self.head = self.in_point
                    self._update_time_fields()
                    self._draw_timeline()
                elif kind == "encode_progress" and self.busy == "export":
                    if event[1] == 2:
                        self.progress["value"] = max(0, min(100, event[2]))
                elif kind == "status_export" and self.busy == "export":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = 0
                    self.status_var.set(event[1])
                elif kind == "export_done" and self.busy == "export":
                    self.recent_output = event[1]
                    self._finish_busy()
                    self.progress["value"] = 100
                    self.open_button.configure(state="normal")
                    self.status_var.set(f"GIF saved · {event[2]:.2f} MiB · {event[1]}")
                    messagebox.showinfo("GIF created!", f"Saved successfully!\n\n{event[1]}\n\nSize: {event[2]:.2f} MiB")
                elif kind == "export_error" and self.busy == "export":
                    cancelled = self.cancel.is_set()
                    self._finish_busy()
                    self.status_var.set("Export cancelled" if cancelled else "Export failed")
                    if not cancelled:
                        messagebox.showerror("Export error", event[1])
        except tk.TclError:
            return

        # Only the newest frame matters. Discard late frames if GUI is busy.
        latest = None
        while True:
            try:
                latest = self.frames.get_nowait()
            except queue.Empty:
                break
        if latest and latest[1] == self.play_generation and self.playing:
            try:
                self._set_preview(Image.frombytes("RGB", PREVIEW_SIZE, latest[3]))
                self.head = min(latest[2], self.duration)
                self.position_var.set(f"{readable_time(self.head)} / {readable_time(self.duration)}")
                self._draw_timeline()
            except (ValueError, tk.TclError):
                pass
        self.root.after(45, self._poll)

    def open_last(self):
        if not self.recent_output or not self.recent_output.exists():
            return
        path = str(self.recent_output)
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _close(self):
        self.cancel.set()
        self._stop_playback()
        with self.process_lock:
            if self.active_process:
                try:
                    self.active_process.kill()
                except OSError:
                    pass
        # Worker threads are daemon threads; don't delete their temp files before
        # they terminate. The OS will clean temporary files on normal close.
        self.root.destroy()


def main():
    try:
        app_root = tk.Tk()
        LoopCraftApp(app_root)
    except RuntimeError as exc:
        try:
            messagebox.showerror("LoopCraft setup", str(exc))
        except tk.TclError:
            print(exc, file=sys.stderr)
        return 1
    app_root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
