"""Keeps the list of downloads and saves it between runs."""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import List, Optional

from .downloader import DownloadTask


def default_data_file() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "tsdm", "downloads.json")


class DownloadManager:
    def __init__(self, data_file: Optional[str] = None):
        self.data_file = data_file or default_data_file()
        self._lock = threading.RLock()
        self._tasks: dict = {}
        self._load()

    # ----- persistence ------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.data_file, encoding="utf-8") as fh:
                records = json.load(fh)
        except (OSError, ValueError):
            return
        for record in records:
            try:
                task = DownloadTask.from_dict(record, on_change=self.save)
            except (KeyError, TypeError, ValueError):
                continue
            if task.state == "downloading":  # the app was closed mid-download
                task.state = "paused"
            self._tasks[task.id] = task

    def save(self, _task=None) -> None:
        with self._lock:
            records = [task.to_dict() for task in self._tasks.values()]
            try:
                os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
                tmp = self.data_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(records, fh, indent=2)
                os.replace(tmp, self.data_file)
            except OSError as exc:
                print(f"tsdm: could not save download list: {exc}", file=sys.stderr)

    # ----- download list ----------------------------------------------

    def add(self, url: str, dest_dir: str, max_segments: int = 8,
            start: bool = True) -> DownloadTask:
        task = DownloadTask(url, dest_dir, max_segments, on_change=self.save)
        with self._lock:
            self._tasks[task.id] = task
        self.save()
        if start:
            task.start()
        return task

    def get(self, task_id: str) -> Optional[DownloadTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> List[DownloadTask]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.added)

    def find_resumable(self, url: str, dest_dir: str) -> Optional[DownloadTask]:
        dest_dir = os.path.abspath(dest_dir)
        with self._lock:
            for task in self._tasks.values():
                if task.url == url and task.dest_dir == dest_dir and task.state != "completed":
                    return task
        return None

    def remove(self, task_id: str, delete_partial: bool = True) -> None:
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if task is None:
            return
        task.pause()
        task.join(5)
        if delete_partial and task.state != "completed" and task.part_path:
            try:
                os.remove(task.part_path)
            except OSError:
                pass
        self.save()

    def pause_all(self) -> None:
        tasks = self.all()
        for task in tasks:
            task.pause()
        for task in tasks:
            task.join(5)
        self.save()
