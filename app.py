"""Clip Maker: a small Mac app that saves part of a YouTube video as MP3 or MP4.

GUI only. All the downloading/cutting logic lives in core.py.
"""
from __future__ import annotations

import io
import os
import queue
import sys
import threading
import webbrowser
from tkinter import filedialog

import customtkinter as ctk

import core

APP_TITLE = "Clip Maker"
ACCENT = "#E5484D"          # YouTube-ish red
ACCENT_HOVER = "#C83A3F"
COOKIE_BROWSERS = ["None", "Safari", "Chrome", "Firefox", "Brave", "Edge", "Opera"]

ctk.set_appearance_mode("system")      # follows macOS light/dark
ctk.set_default_color_theme("blue")


def _font(size=13, weight="normal"):
    return ctk.CTkFont(size=size, weight=weight)


class TimeField(ctk.CTkFrame):
    """Label + HH:MM:SS entry + slider, kept in sync."""

    def __init__(self, master, label, on_change):
        super().__init__(master, fg_color="transparent")
        self.on_change = on_change
        self.max = 1.0
        self._lock = False
        self.columnconfigure(2, weight=1)
        ctk.CTkLabel(self, text=label, width=44, anchor="w", font=_font(13, "bold")).grid(
            row=0, column=0, padx=(0, 8))
        self.var = ctk.StringVar(value="00:00:00")
        self.entry = ctk.CTkEntry(self, textvariable=self.var, width=96, justify="center",
                                  font=ctk.CTkFont(family="Menlo", size=13))
        self.entry.grid(row=0, column=1, padx=(0, 12))
        self.entry.bind("<Return>", lambda e: self._from_entry())
        self.entry.bind("<FocusOut>", lambda e: self._from_entry())
        self.slider = ctk.CTkSlider(self, from_=0, to=1, command=self._from_slider,
                                    button_color=ACCENT, button_hover_color=ACCENT_HOVER,
                                    progress_color=ACCENT)
        self.slider.grid(row=0, column=2, sticky="ew")

    def configure_range(self, maximum: float, value: float):
        self.max = max(1.0, float(maximum))
        steps = int(self.max) if self.max <= 20000 else None
        self.slider.configure(from_=0, to=self.max, number_of_steps=steps)
        self.set(value)

    def set(self, seconds: float):
        self._lock = True
        seconds = max(0.0, min(self.max, seconds))
        self.slider.set(seconds)
        self.var.set(core.format_time(seconds))
        self._lock = False

    def get(self) -> float:
        return core.parse_time(self.var.get())

    def _from_slider(self, v):
        if self._lock:
            return
        self.var.set(core.format_time(v))
        self.entry.configure(border_color=("#979DA2", "#565B5E"))
        self.on_change(self)

    def _from_entry(self):
        try:
            v = self.get()
            self.entry.configure(border_color=("#979DA2", "#565B5E"))
            self._lock = True
            self.slider.set(min(v, self.max))
            self._lock = False
        except ValueError:
            self.entry.configure(border_color=ACCENT)
        self.on_change(self)

    def set_enabled(self, on: bool):
        state = "normal" if on else "disabled"
        self.entry.configure(state=state)
        self.slider.configure(state=state)


class ClipMakerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg = core.load_config()
        self.info: core.VideoInfo | None = None
        self.job: core.ClipJob | None = None
        self.last_output: str | None = None
        self.events: queue.Queue = queue.Queue()
        self._thumb_img = None

        self.title(APP_TITLE)
        self.geometry("720x760")
        self.minsize(640, 700)
        self.grid_columnconfigure(0, weight=1)

        self._build_header()
        self._build_url_card()
        self._build_video_card()
        self._build_clip_card()
        self._build_output_card()
        self._build_action_area()
        self._build_footer()

        self._set_video_enabled(False)
        self.after(100, self._poll_events)
        self.after(300, self._check_tools)
        self.bind_all("<Command-Return>", lambda e: self._make_clip())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _card(self, row, pady=(0, 12)):
        f = ctk.CTkFrame(self, corner_radius=14)
        f.grid(row=row, column=0, sticky="ew", padx=20, pady=pady)
        f.grid_columnconfigure(0, weight=1)
        return f

    def _build_header(self):
        h = ctk.CTkFrame(self, fg_color="transparent")
        h.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 10))
        ctk.CTkLabel(h, text="✂  Clip Maker", font=_font(24, "bold")).pack(side="left")
        ctk.CTkLabel(h, text="Save part of a YouTube video as MP3 or MP4",
                     text_color=("gray40", "gray65"), font=_font(13)).pack(side="left", padx=12, pady=(6, 0))

    def _build_url_card(self):
        c = self._card(1)
        c.grid_columnconfigure(0, weight=1)
        self.url_var = ctk.StringVar()
        self.url_entry = ctk.CTkEntry(c, textvariable=self.url_var, height=38, font=_font(14),
                                      placeholder_text="Paste a YouTube link…  (⌘V)")
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(14, 8), pady=14)
        self.url_entry.bind("<Return>", lambda e: self._fetch())
        ctk.CTkButton(c, text="Paste", width=70, height=38, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self._paste).grid(row=0, column=1, padx=(0, 8))
        self.fetch_btn = ctk.CTkButton(c, text="Get video", width=110, height=38, font=_font(14, "bold"),
                                       fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._fetch)
        self.fetch_btn.grid(row=0, column=2, padx=(0, 14))

    def _build_video_card(self):
        c = self._card(2)
        c.grid_columnconfigure(0, weight=0)
        c.grid_columnconfigure(1, weight=1)
        self.thumb = ctk.CTkLabel(c, text="No video yet", width=192, height=108, corner_radius=10,
                                  fg_color=("gray85", "gray20"), text_color=("gray45", "gray60"))
        self.thumb.grid(row=0, column=0, rowspan=3, padx=14, pady=14)
        self.title_lbl = ctk.CTkLabel(c, text="Paste a link above and click “Get video”.",
                                      font=_font(15, "bold"), anchor="w", justify="left", wraplength=420)
        self.title_lbl.grid(row=0, column=1, sticky="sw", padx=(0, 14), pady=(18, 2))
        self.meta_lbl = ctk.CTkLabel(c, text="", anchor="w", text_color=("gray40", "gray65"))
        self.meta_lbl.grid(row=1, column=1, sticky="nw", padx=(0, 14))
        self.preview_btn = ctk.CTkButton(c, text="▶  Watch from start time", width=180, height=28,
                                         fg_color="transparent", border_width=1,
                                         text_color=("gray10", "gray90"), command=self._preview)
        self.preview_btn.grid(row=2, column=1, sticky="nw", padx=(0, 14), pady=(4, 14))

    def _build_clip_card(self):
        c = self._card(3)
        ctk.CTkLabel(c, text="Clip", font=_font(13, "bold"), text_color=("gray40", "gray65")).grid(
            row=0, column=0, sticky="w", padx=16, pady=(12, 4))
        self.start_f = TimeField(c, "Start", self._on_time_change)
        self.start_f.grid(row=1, column=0, sticky="ew", padx=16, pady=4)
        self.end_f = TimeField(c, "End", self._on_time_change)
        self.end_f.grid(row=2, column=0, sticky="ew", padx=16, pady=4)
        self.len_lbl = ctk.CTkLabel(c, text="Clip length: –", anchor="w", text_color=("gray40", "gray65"))
        self.len_lbl.grid(row=3, column=0, sticky="w", padx=16, pady=(2, 8))

        fr = ctk.CTkFrame(c, fg_color="transparent")
        fr.grid(row=4, column=0, sticky="ew", padx=16, pady=(4, 14))
        ctk.CTkLabel(fr, text="Format", font=_font(13, "bold")).pack(side="left", padx=(0, 10))
        self.kind_var = ctk.StringVar(value=self.cfg.get("kind", "MP4"))
        self.kind_seg = ctk.CTkSegmentedButton(fr, values=["MP4", "MP3"], variable=self.kind_var,
                                               command=self._on_kind, selected_color=ACCENT,
                                               selected_hover_color=ACCENT_HOVER, width=140)
        self.kind_seg.pack(side="left")
        ctk.CTkLabel(fr, text="Quality", font=_font(13, "bold")).pack(side="left", padx=(22, 10))
        self.quality_var = ctk.StringVar()
        self.quality_menu = ctk.CTkOptionMenu(fr, variable=self.quality_var, width=150,
                                              command=lambda _: self._save_prefs())
        self.quality_menu.pack(side="left")
        self._on_kind(self.kind_var.get(), save=False)

    def _build_output_card(self):
        c = self._card(4)
        c.grid_columnconfigure(0, weight=0)
        c.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(c, text="Save to", font=_font(13, "bold")).grid(row=0, column=0, padx=(16, 10), pady=12)
        self.out_lbl = ctk.CTkLabel(c, text=self._pretty_path(self.cfg["output_dir"]), anchor="w")
        self.out_lbl.grid(row=0, column=1, sticky="ew")
        ctk.CTkButton(c, text="Change…", width=90, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self._choose_dir).grid(row=0, column=2, padx=14)

    def _build_action_area(self):
        a = ctk.CTkFrame(self, fg_color="transparent")
        a.grid(row=5, column=0, sticky="ew", padx=20, pady=(2, 6))
        a.grid_columnconfigure(0, weight=1)
        btns = ctk.CTkFrame(a, fg_color="transparent")
        btns.grid(row=0, column=0, sticky="ew")
        btns.grid_columnconfigure(0, weight=1)
        self.make_btn = ctk.CTkButton(btns, text="✂  Make clip", height=46, font=_font(16, "bold"),
                                      fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._make_clip)
        self.make_btn.grid(row=0, column=0, sticky="ew")
        self.cancel_btn = ctk.CTkButton(btns, text="Cancel", width=100, height=46, fg_color="gray45",
                                        hover_color="gray35", command=self._cancel)
        self.reveal_btn = ctk.CTkButton(btns, text="Show in Finder", width=150, height=46,
                                        font=_font(14, "bold"), command=self._reveal)
        self.progress = ctk.CTkProgressBar(a, height=10, progress_color=ACCENT)
        self.progress.set(0)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(12, 4))
        self.status_lbl = ctk.CTkLabel(a, text="Ready.", anchor="w", justify="left", wraplength=660)
        self.status_lbl.grid(row=2, column=0, sticky="ew")

    def _build_footer(self):
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.grid(row=6, column=0, sticky="sew", padx=20, pady=(4, 14))
        self.grid_rowconfigure(6, weight=1)
        ctk.CTkLabel(f, text="Use cookies from", text_color=("gray40", "gray65")).pack(side="left")
        self.cookie_var = ctk.StringVar(value=self.cfg.get("cookies_browser", "None"))
        ctk.CTkOptionMenu(f, values=COOKIE_BROWSERS, variable=self.cookie_var, width=100,
                          command=lambda _: self._save_prefs()).pack(side="left", padx=8)
        self.update_btn = ctk.CTkButton(f, text="Update downloader", width=150, fg_color="transparent",
                                        border_width=1, text_color=("gray10", "gray90"),
                                        command=self._update_downloader)
        self.update_btn.pack(side="right")
        self.ver_lbl = ctk.CTkLabel(f, text="", text_color=("gray40", "gray65"))
        self.ver_lbl.pack(side="right", padx=10)
        threading.Thread(target=lambda: self.events.put(("version", core.ytdlp_version())),
                         daemon=True).start()

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _pretty_path(p):
        home = os.path.expanduser("~")
        return "~" + p[len(home):] if p.startswith(home) else p

    def _status(self, text, kind="info"):
        colors = {"info": ("gray10", "gray90"), "error": ("#C62828", "#FF8A80"),
                  "ok": ("#2E7D32", "#81C784")}
        self.status_lbl.configure(text=text, text_color=colors.get(kind, colors["info"]))

    def _save_prefs(self):
        self.cfg.update(kind=self.kind_var.get(), cookies_browser=self.cookie_var.get())
        if self.kind_var.get() == "MP3":
            self.cfg["audio_quality"] = self.quality_var.get().split()[0]
        else:
            self.cfg["video_quality"] = self.quality_var.get()
        core.save_config(self.cfg)

    def _set_video_enabled(self, on):
        self.start_f.set_enabled(on)
        self.end_f.set_enabled(on)
        self.preview_btn.configure(state="normal" if on else "disabled")
        self.make_btn.configure(state="normal" if on else "disabled")

    def _set_busy(self, busy):
        self.fetch_btn.configure(state="disabled" if busy else "normal")
        self.update_btn.configure(state="disabled" if busy else "normal")
        self.kind_seg.configure(state="disabled" if busy else "normal")
        self.quality_menu.configure(state="disabled" if busy else "normal")
        self._set_video_enabled(not busy and self.info is not None)
        if busy:
            self.reveal_btn.grid_forget()
            self.cancel_btn.grid(row=0, column=1, padx=(10, 0))
        else:
            self.cancel_btn.grid_forget()

    # ------------------------------------------------------------- actions
    def _check_tools(self):
        t = core.check_tools()
        if not t["ffmpeg"]:
            self._status("ffmpeg isn't installed. Open Terminal and run:  brew install ffmpeg  "
                         "(or run “Clip Maker.command” again).", "error")
        elif not t["js"]:
            self._status("Tip: install Deno for more reliable YouTube downloads:  brew install deno")

    def _paste(self):
        try:
            self.url_var.set(self.clipboard_get().strip())
            self._fetch()
        except Exception:
            pass

    def _on_kind(self, kind, save=True):
        if kind == "MP3":
            vals = [f"{q} kbps" for q in core.AUDIO_QUALITIES]
            cur = f"{self.cfg.get('audio_quality', '192')} kbps"
        else:
            vals = core.VIDEO_QUALITIES
            cur = self.cfg.get("video_quality", "1080p")
        self.quality_menu.configure(values=vals)
        self.quality_var.set(cur if cur in vals else vals[1])
        if save:
            self._save_prefs()

    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.cfg["output_dir"], title="Save clips to…")
        if d:
            self.cfg["output_dir"] = d
            self.out_lbl.configure(text=self._pretty_path(d))
            core.save_config(self.cfg)

    def _fetch(self):
        url = self.url_var.get().strip()
        try:
            url = core.normalize_url(url)
        except core.ClipError as e:
            self._status(str(e), "error")
            return
        self.info = None
        self._set_busy(True)
        self._set_video_enabled(False)
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self._status("Looking up the video…")
        cookies = self.cookie_var.get()

        def work():
            try:
                info = core.fetch_info(url, cookies)
                self.events.put(("info", info))
                self.events.put(("thumb", core.fetch_thumbnail(info)))
            except core.ClipError as e:
                self.events.put(("fetch_error", str(e)))
            except Exception as e:
                self.events.put(("fetch_error", core.friendly_error(str(e))))
        threading.Thread(target=work, daemon=True).start()

    def _show_info(self, info: core.VideoInfo):
        self.info = info
        self.title_lbl.configure(text=info.title)
        dur = core.format_time(info.duration) if info.duration else "unknown length"
        self.meta_lbl.configure(text=f"{info.uploader}  ·  {dur}" if info.uploader else dur)
        total = info.duration or 3600
        self.start_f.configure_range(total, 0)
        self.end_f.configure_range(total, min(total, 30))
        self._on_time_change(None)
        self.thumb.configure(image=None, text="Loading…")

    def _show_thumb(self, data):
        if not data:
            self.thumb.configure(image=None, text="(no preview)")
            return
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((192, 108))
            self._thumb_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
            self.thumb.configure(image=self._thumb_img, text="")
        except Exception:
            self.thumb.configure(text="(no preview)")

    def _times(self):
        s, e = self.start_f.get(), self.end_f.get()
        core.validate_range(s, e, self.info.duration if self.info else None)
        return s, e

    def _on_time_change(self, field):
        if not self.info:
            return
        # keep start < end when dragging sliders
        try:
            s, e = self.start_f.get(), self.end_f.get()
        except ValueError:
            self.len_lbl.configure(text="Use times like 1:23 or 00:01:23", text_color=ACCENT)
            return
        if field is self.start_f and s >= e and self.info.duration:
            self.end_f.set(min(self.info.duration, s + 1))
        elif field is self.end_f and e <= s:
            self.start_f.set(max(0, e - 1))
        try:
            s, e = self._times()
            self.len_lbl.configure(text=f"Clip length: {core.format_time(e - s)}",
                                   text_color=("gray40", "gray65"))
        except core.ClipError as err:
            self.len_lbl.configure(text=str(err), text_color=ACCENT)

    def _preview(self):
        if self.info:
            try:
                t = int(self.start_f.get())
            except ValueError:
                t = 0
            sep = "&" if "?" in self.info.url else "?"
            webbrowser.open(f"{self.info.url}{sep}t={t}s")

    def _make_clip(self):
        if not self.info or self.job:
            return
        try:
            s, e = self._times()
        except (ValueError, core.ClipError) as err:
            self._status(str(err) if isinstance(err, core.ClipError)
                         else "Use times like 1:23 or 00:01:23.", "error")
            return
        kind = self.kind_var.get()
        q = self.quality_var.get().split()[0]
        self._save_prefs()
        self.job = core.ClipJob(self.info, s, e, kind, q, self.cfg["output_dir"],
                                cookies_browser=self.cookie_var.get(),
                                on_progress=lambda f, t: self.events.put(("progress", (f, t))))
        self._set_busy(True)
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self._status("Starting…")
        job = self.job

        def work():
            try:
                self.events.put(("done", job.run()))
            except core.Cancelled:
                self.events.put(("cancelled", None))
            except core.ClipError as err:
                self.events.put(("clip_error", str(err)))
            except Exception as err:
                self.events.put(("clip_error", core.friendly_error(str(err))))
        threading.Thread(target=work, daemon=True).start()

    def _cancel(self):
        if self.job:
            self._status("Cancelling…")
            threading.Thread(target=self.job.cancel, daemon=True).start()

    def _reveal(self):
        if self.last_output:
            core.reveal_in_finder(self.last_output)

    def _update_downloader(self):
        self.update_btn.configure(state="disabled", text="Updating…")
        self._status("Updating the downloader (yt-dlp)…")

        def work():
            self.events.put(("updated", core.update_downloader()))
        threading.Thread(target=work, daemon=True).start()

    def _on_close(self):
        if self.job:
            self.job.cancel()
        self.destroy()

    # ------------------------------------------------------- event pump
    def _poll_events(self):
        try:
            while True:
                kind, val = self.events.get_nowait()
                self._handle(kind, val)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _stop_indeterminate(self):
        self.progress.stop()
        self.progress.configure(mode="determinate")

    def _handle(self, kind, val):
        if kind == "info":
            self._stop_indeterminate()
            self.progress.set(0)
            self._show_info(val)      # sets self.info first...
            self._set_busy(False)     # ...so this can enable the time controls
            self._status("Pick the start and end, then click “Make clip”.")
        elif kind == "thumb":
            self._show_thumb(val)
        elif kind == "fetch_error":
            self._stop_indeterminate()
            self.progress.set(0)
            self._set_busy(False)
            self._status(val, "error")
        elif kind == "progress":
            frac, text = val
            if frac is None:
                if self.progress.cget("mode") != "indeterminate":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start()
            else:
                if self.progress.cget("mode") == "indeterminate":
                    self._stop_indeterminate()
                self.progress.set(frac)
            self._status(text)
        elif kind == "done":
            self.job = None
            self._stop_indeterminate()
            self.progress.set(1)
            self.last_output = val
            self._set_busy(False)
            self.reveal_btn.grid(row=0, column=1, padx=(10, 0))
            self._status(f"Saved: {os.path.basename(val)}", "ok")
        elif kind == "cancelled":
            self.job = None
            self._stop_indeterminate()
            self.progress.set(0)
            self._set_busy(False)
            self._status("Cancelled.")
        elif kind == "clip_error":
            self.job = None
            self._stop_indeterminate()
            self.progress.set(0)
            self._set_busy(False)
            self._status(val, "error")
        elif kind == "version":
            self.ver_lbl.configure(text=f"yt-dlp {val}")
        elif kind == "updated":
            ok, msg = val
            self.update_btn.configure(state="normal", text="Update downloader")
            self._status(msg, "ok" if ok else "error")
            threading.Thread(target=lambda: self.events.put(("version", core.ytdlp_version())),
                             daemon=True).start()


def main():
    app = ClipMakerApp()
    if len(sys.argv) > 1 and sys.argv[1].startswith("http"):
        app.url_var.set(sys.argv[1])
        app.after(400, app._fetch)
    app.mainloop()


if __name__ == "__main__":
    main()
