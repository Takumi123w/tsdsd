"""Tkinter front end for tsdm."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import tkinter as tk
import urllib.parse
from tkinter import filedialog, messagebox, ttk

from .downloader import human_eta, human_size
from .manager import DownloadManager

STATE_LABELS = {
    "queued": "Queued",
    "downloading": "Downloading",
    "paused": "Paused",
    "completed": "Completed",
    "error": "Error",
}
COLUMNS = (
    ("name", "File", 340),
    ("size", "Size", 90),
    ("progress", "Done", 80),
    ("speed", "Speed", 100),
    ("eta", "ETA", 90),
    ("status", "Status", 200),
)
POLL_MS = 500


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("tsdm - Download Manager")
        self.geometry("1040x560")
        self.minsize(760, 400)

        self.manager = DownloadManager()
        self.url_var = tk.StringVar()
        self.dir_var = tk.StringVar(value=os.path.expanduser("~/Downloads"))
        self.segments_var = tk.StringVar(value="8")
        self.status_var = tk.StringVar(value="Ready")

        self._samples: dict = {}      # task id -> (bytes, monotonic time)
        self._last_state: dict = {}   # task id -> last seen state

        self._build_ui()
        for task in self.manager.all():
            self._last_state[task.id] = task.state

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick()

    # ----- layout -----------------------------------------------------

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Address (URL):").grid(row=0, column=0, sticky="w")
        url_entry = ttk.Entry(top, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, sticky="ew", padx=6)
        url_entry.bind("<Return>", lambda _e: self.add_download())
        ttk.Button(top, text="Paste", command=self.paste_url).grid(row=0, column=2)

        ttk.Label(top, text="Save to:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.dir_var).grid(row=1, column=1, sticky="ew",
                                                       padx=6, pady=(6, 0))
        ttk.Button(top, text="Browse...", command=self.browse_dir).grid(row=1, column=2,
                                                                        pady=(6, 0))

        ttk.Label(top, text="Connections:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(top, from_=1, to=16, width=5,
                    textvariable=self.segments_var).grid(row=2, column=1, sticky="w",
                                                         padx=6, pady=(6, 0))
        ttk.Button(top, text="Add download", command=self.add_download).grid(
            row=2, column=2, pady=(6, 0))
        top.columnconfigure(1, weight=1)

        middle = ttk.Frame(self, padding=(10, 0))
        middle.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(middle, columns=[c[0] for c in COLUMNS],
                                 show="headings", selectmode="browse")
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "name"))
        scroll = ttk.Scrollbar(middle, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        buttons = (
            ("Resume", self.resume_selected),
            ("Pause", self.pause_selected),
            ("Remove", self.remove_selected),
            ("Open folder", self.open_folder_selected),
            ("Copy link", self.copy_link_selected),
        )
        for text, command in buttons:
            ttk.Button(bottom, text=text, command=command).pack(side="left", padx=(0, 6))
        ttk.Label(bottom, textvariable=self.status_var).pack(side="right")

    # ----- actions ----------------------------------------------------

    def paste_url(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            text = ""
        if text:
            self.url_var.set(text)

    def browse_dir(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self, initialdir=self.dir_var.get() or os.path.expanduser("~"))
        if chosen:
            self.dir_var.set(chosen)

    def add_download(self) -> None:
        url = self.url_var.get().strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            messagebox.showerror("Invalid address",
                                 "Enter a full http:// or https:// address.", parent=self)
            return
        dest = os.path.expanduser(self.dir_var.get().strip() or "~/Downloads")
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Folder error", str(exc), parent=self)
            return
        try:
            segments = max(1, min(16, int(self.segments_var.get())))
        except ValueError:
            segments = 8

        task = self.manager.add(url, dest, segments)
        self._last_state[task.id] = "downloading"
        self.url_var.set("")
        self.status_var.set("Download added")

    def _selected_task(self):
        selection = self.tree.selection()
        if not selection:
            self.status_var.set("Select a download first.")
            return None
        return self.manager.get(selection[0])

    def resume_selected(self) -> None:
        task = self._selected_task()
        if task:
            task.start()
            self.status_var.set("Resuming...")

    def pause_selected(self) -> None:
        task = self._selected_task()
        if task:
            task.pause()
            self.status_var.set("Pausing...")

    def remove_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        name = task.filename or task.url
        if task.state != "completed" and not messagebox.askyesno(
                "Remove download",
                f"Remove '{name}' from the list?\nThe partial file will be deleted.",
                parent=self):
            return
        self.manager.remove(task.id, delete_partial=True)
        if self.tree.exists(task.id):
            self.tree.delete(task.id)
        self._samples.pop(task.id, None)
        self._last_state.pop(task.id, None)
        self.status_var.set("Removed")

    def open_folder_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        opener = shutil.which("xdg-open")
        if not opener:
            messagebox.showinfo("Open folder", f"Folder: {task.dest_dir}", parent=self)
            return
        subprocess.Popen([opener, task.dest_dir])

    def copy_link_selected(self) -> None:
        task = self._selected_task()
        if task:
            self.clipboard_clear()
            self.clipboard_append(task.url)
            self.status_var.set("Link copied")

    def _on_close(self) -> None:
        self.status_var.set("Pausing downloads...")
        self.update_idletasks()
        self.manager.pause_all()
        self.destroy()

    # ----- periodic refresh -------------------------------------------

    def _tick(self) -> None:
        now = time.monotonic()
        for task in self.manager.all():
            done = task.downloaded()
            previous = self._samples.get(task.id)
            speed = 0.0
            if previous and task.is_active() and now > previous[1]:
                speed = max(0.0, (done - previous[0]) / (now - previous[1]))
            self._samples[task.id] = (done, now)

            self._update_row(task, done, speed)

            if task.state == "completed" and self._last_state.get(task.id) != "completed":
                self._notify(task)
            self._last_state[task.id] = task.state
        self.after(POLL_MS, self._tick)

    def _update_row(self, task, done: int, speed: float) -> None:
        size = task.size
        if size:
            progress = f"{min(100.0, done * 100 / size):.1f}%"
        else:
            progress = "100%" if task.state == "completed" else ""
        active = task.is_active()
        speed_text = f"{human_size(speed)}/s" if active and speed > 0 else ""
        eta = human_eta((size - done) / speed) if (active and size and speed > 0) else ""
        status = STATE_LABELS.get(task.state, task.state)
        if task.state == "error" and task.error:
            status = f"Error: {task.error}"[:90]
        values = (task.filename or task.url, human_size(size), progress,
                  speed_text, eta, status)
        if self.tree.exists(task.id):
            self.tree.item(task.id, values=values)
        else:
            self.tree.insert("", "end", iid=task.id, values=values)

    def _notify(self, task) -> None:
        name = task.filename or task.url
        self.status_var.set(f"Completed: {name}")
        notifier = shutil.which("notify-send")
        if notifier:
            try:
                subprocess.Popen([notifier, "Download complete", name])
            except OSError:
                pass


def run() -> int:
    App().mainloop()
    return 0
