"""Segmented, resumable HTTP download engine (standard library only).

A download is split into several byte ranges that are fetched in parallel
threads and written into one pre-allocated ``.part`` file. Segment progress is
stored with the task, so a paused or interrupted download resumes where it
stopped, even after the application restarts.
"""

from __future__ import annotations

import http.client
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) tsdm/1.0"
CHUNK_SIZE = 64 * 1024
MIN_SEGMENT_SIZE = 1024 * 1024
MAX_SEGMENTS = 32
MAX_RETRIES = 5
TIMEOUT = 20.0
PART_SUFFIX = ".part"


class DownloadError(Exception):
    """A failure that should be reported to the user."""


@dataclass
class ProbeResult:
    url: str  # final URL after redirects
    size: Optional[int]
    ranges: bool  # server honours Range requests (and size is known)
    filename: str


_CD_STAR = re.compile(r"filename\*\s*=\s*([\w-]*)'[^']*'([^;]+)", re.IGNORECASE)
_CD_PLAIN = re.compile(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)', re.IGNORECASE)


def _open(url: str, headers: Optional[Dict[str, str]] = None):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def sanitize_filename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .")
    return name[:200] or "download"


def filename_from_headers(headers) -> Optional[str]:
    disposition = headers.get("Content-Disposition") or ""
    match = _CD_STAR.search(disposition)
    if match:
        value = match.group(2).strip()
        try:
            return urllib.parse.unquote(value, encoding=match.group(1) or "utf-8",
                                        errors="replace")
        except LookupError:
            return urllib.parse.unquote(value)
    match = _CD_PLAIN.search(disposition)
    if match:
        return (match.group(1) or match.group(2)).strip()
    return None


def filename_from_url(url: str) -> Optional[str]:
    path = urllib.parse.urlsplit(url).path
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    return sanitize_filename(name) if name else None


def probe(url: str) -> ProbeResult:
    """Learn size, range support and file name with a single request."""
    try:
        resp = _open(url, {"Range": "bytes=0-0"})
    except urllib.error.HTTPError as exc:
        if exc.code == 416:  # zero-byte file: no range can be satisfied
            return ProbeResult(url, 0, False, filename_from_url(url) or "download")
        raise DownloadError(f"server answered HTTP {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise DownloadError(f"cannot reach server: {exc}") from exc

    with resp:  # body is deliberately not read; closing drops the connection
        headers = resp.headers
        final_url = resp.geturl()
        size: Optional[int] = None
        ranges = False
        if resp.status == 206:
            ranges = True
            match = re.search(r"/(\d+)\s*$", headers.get("Content-Range", ""))
            if match:
                size = int(match.group(1))
        else:
            length = headers.get("Content-Length", "")
            if length.isdigit():
                size = int(length)

    name = filename_from_headers(headers) or filename_from_url(final_url) or "download"
    return ProbeResult(final_url, size, ranges and size is not None, sanitize_filename(name))


def unique_filename(dest_dir: str, name: str) -> str:
    """Return ``name`` or ``name (n).ext`` so nothing existing is overwritten."""
    base, ext = os.path.splitext(name)
    candidate, counter = name, 1
    while (os.path.exists(os.path.join(dest_dir, candidate))
           or os.path.exists(os.path.join(dest_dir, candidate + PART_SUFFIX))):
        candidate = f"{base} ({counter}){ext}"
        counter += 1
    return candidate


def plan_segments(size: Optional[int], ranges: bool, max_segments: int) -> List[Dict[str, Any]]:
    """Split a file into byte ranges. ``end`` is inclusive; ``None`` means unknown."""
    if size is None:
        return [{"start": 0, "end": None, "done": 0}]
    if size == 0:
        return [{"start": 0, "end": -1, "done": 0}]
    if not ranges:
        return [{"start": 0, "end": size - 1, "done": 0}]
    count = max(1, min(max_segments, size // MIN_SEGMENT_SIZE))
    part = size // count
    segments = []
    for i in range(count):
        start = i * part
        end = size - 1 if i == count - 1 else start + part - 1
        segments.append({"start": start, "end": end, "done": 0})
    return segments


def _segment_done(seg: Dict[str, Any]) -> bool:
    if seg.get("finished"):
        return True
    return seg["end"] is not None and seg["start"] + seg["done"] > seg["end"]


def human_size(num: Optional[float]) -> str:
    if num is None:
        return "?"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def human_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class DownloadTask:
    """One download: probe, split into segments, fetch in parallel, finalize.

    States: queued, downloading, paused, completed, error.
    """

    def __init__(self, url: str, dest_dir: str, max_segments: int = 8, *,
                 task_id: Optional[str] = None, state: str = "queued",
                 filename: Optional[str] = None, size: Optional[int] = None,
                 ranges: bool = False, final_url: Optional[str] = None,
                 segments_state: Optional[List[Dict[str, Any]]] = None,
                 added: Optional[float] = None, error: Optional[str] = None,
                 on_change=None):
        self.id = task_id or uuid.uuid4().hex[:12]
        self.url = url
        self.final_url = final_url or url
        self.dest_dir = os.path.abspath(dest_dir)
        self.max_segments = max(1, min(int(max_segments), MAX_SEGMENTS))
        self.filename = filename
        self.size = size
        self.ranges = ranges
        self.segments: List[Dict[str, Any]] = segments_state or []
        self.state = state
        self.error = error
        self.added = added or time.time()
        self.on_change = on_change
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ----- public API -------------------------------------------------

    @property
    def path(self) -> Optional[str]:
        return os.path.join(self.dest_dir, self.filename) if self.filename else None

    @property
    def part_path(self) -> Optional[str]:
        return self.path + PART_SUFFIX if self.filename else None

    def downloaded(self) -> int:
        with self._lock:
            return sum(seg["done"] for seg in self.segments)

    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_active() or self.state == "completed":
                return
            self._stop = threading.Event()
            self.state = "downloading"
            self.error = None
            self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True)
            self._thread.start()

    def pause(self) -> None:
        self._stop.set()

    def join(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "url": self.url,
                "final_url": self.final_url,
                "dest_dir": self.dest_dir,
                "max_segments": self.max_segments,
                "filename": self.filename,
                "size": self.size,
                "ranges": self.ranges,
                "state": self.state,
                "error": self.error,
                "added": self.added,
                "segments": [dict(seg) for seg in self.segments],
            }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], on_change=None) -> "DownloadTask":
        return cls(
            data["url"], data["dest_dir"], data.get("max_segments", 8),
            task_id=data["id"], state=data.get("state", "paused"),
            filename=data.get("filename"), size=data.get("size"),
            ranges=data.get("ranges", False), final_url=data.get("final_url"),
            segments_state=data.get("segments") or [], added=data.get("added"),
            error=data.get("error"), on_change=on_change,
        )

    # ----- internals --------------------------------------------------

    def _run(self, stop: threading.Event) -> None:
        try:
            if self.filename is None:
                info = probe(self.url)
                self.final_url = info.url
                self.size = info.size
                self.ranges = info.ranges
                self.filename = unique_filename(self.dest_dir, info.filename)
            self._prepare_file()

            workers = [
                threading.Thread(target=self._worker, args=(seg, stop), daemon=True)
                for seg in self.segments if not _segment_done(seg)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            if self.error:
                self.state = "error"
            elif all(_segment_done(seg) for seg in self.segments):
                os.replace(self.part_path, self.path)
                self.state = "completed"
            elif stop.is_set():
                self.state = "paused"
            else:
                self.state = "error"
                self.error = "download stopped unexpectedly"
        except DownloadError as exc:
            self.state = "error"
            self.error = str(exc)
        except Exception as exc:  # keep the thread from dying silently
            self.state = "error"
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            if self.on_change is not None:
                self.on_change(self)

    def _prepare_file(self) -> None:
        os.makedirs(self.dest_dir, exist_ok=True)
        reusable = bool(
            self.segments
            and os.path.exists(self.part_path)
            and (self.size is None or os.path.getsize(self.part_path) == self.size)
        )
        if not reusable:
            self.segments = plan_segments(self.size, self.ranges, self.max_segments)
            with open(self.part_path, "wb") as fh:
                if self.size:
                    fh.truncate(self.size)
        elif not self.ranges:
            # Without Range support a partial segment cannot be continued.
            for seg in self.segments:
                seg["done"] = 0
                seg.pop("finished", None)

    def _worker(self, seg: Dict[str, Any], stop: threading.Event) -> None:
        retries = 0
        while not stop.is_set() and not _segment_done(seg):
            progressed = False
            headers: Dict[str, str] = {}
            if self.ranges:
                pos = seg["start"] + seg["done"]
                end = "" if seg["end"] is None else str(seg["end"])
                headers["Range"] = f"bytes={pos}-{end}"
            try:
                with _open(self.final_url, headers) as resp:
                    if self.ranges and resp.status != 206:
                        raise DownloadError("server ignored the Range request")
                    with open(self.part_path, "r+b") as fh:
                        while not stop.is_set():
                            chunk = resp.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            with self._lock:
                                pos = seg["start"] + seg["done"]
                            if seg["end"] is not None:
                                need = seg["end"] - pos + 1
                                if need <= 0:
                                    break
                                chunk = chunk[:need]
                            fh.seek(pos)
                            fh.write(chunk)
                            with self._lock:
                                seg["done"] += len(chunk)
                            progressed = True

                if stop.is_set():
                    return
                if seg["end"] is None:  # unknown length: EOF means finished
                    with self._lock:
                        seg["finished"] = True
                    return
                if _segment_done(seg):
                    return
                raise DownloadError("connection closed before the segment finished")

            except urllib.error.HTTPError as exc:
                if exc.code < 500 and exc.code not in (408, 429):
                    self.error = f"server answered HTTP {exc.code}"
                    stop.set()
                    return
                err: Exception = exc
            except (OSError, http.client.HTTPException, DownloadError) as exc:
                err = exc

            if stop.is_set():
                return
            if not self.ranges:
                with self._lock:
                    seg["done"] = 0
            retries = 0 if (progressed and self.ranges) else retries + 1
            if retries > MAX_RETRIES:
                self.error = f"network error: {err}"
                stop.set()
                return
            stop.wait(min(2 ** retries, 30))
