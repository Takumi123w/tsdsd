"""Command-line entry point: ``python3 -m tsdm [URL] [-o DIR] [-n N]``.

Without a URL the graphical window opens. With a URL the file is downloaded in
the terminal. Both use the same saved download list, so an interrupted
command-line download can be resumed later from the window (or by running the
same command again).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__
from .downloader import human_size
from .manager import DownloadManager


def _cli_download(url: str, out_dir: str, segments: int) -> int:
    out_dir = os.path.abspath(out_dir)
    manager = DownloadManager()
    task = manager.find_resumable(url, out_dir)
    if task is None:
        task = manager.add(url, out_dir, segments, start=False)
    else:
        print(f"Resuming earlier download of {task.filename or url}")
    task.start()

    try:
        while task.is_active():
            time.sleep(0.5)
            done = task.downloaded()
            name = task.filename or "probing server..."
            pct = f"{done * 100 / task.size:5.1f}%" if task.size else "  ?  "
            sys.stdout.write(
                f"\r{name}  {pct}  {human_size(done)} / {human_size(task.size)}      ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        task.pause()
        task.join(10)
        manager.save()
        print("\nPaused. Run the same command again to resume.")
        return 130

    task.join()
    print()
    if task.state == "completed":
        print(f"Saved: {task.path}")
        return 0
    print(f"Failed ({task.state}): {task.error or 'unknown error'}", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tsdm", description="Segmented, resumable download manager.")
    parser.add_argument("url", nargs="?",
                        help="download this URL in the terminal instead of opening the window")
    parser.add_argument("-o", "--output-dir", default=os.path.expanduser("~/Downloads"),
                        help="folder to save into (default: ~/Downloads)")
    parser.add_argument("-n", "--segments", type=int, default=8,
                        help="parallel connections per file, 1-32 (default: 8)")
    parser.add_argument("--version", action="version", version=f"tsdm {__version__}")
    args = parser.parse_args(argv)

    if args.url:
        return _cli_download(args.url, args.output_dir, max(1, min(32, args.segments)))

    from .gui import run  # imported lazily so the CLI works without Tk
    return run()


if __name__ == "__main__":
    sys.exit(main())
