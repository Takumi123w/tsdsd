# tsdm — Download Manager for Linux

A small, dependency-free download manager in the spirit of Internet Download
Manager. It splits each file into several parts, downloads them over parallel
connections, and can pause and resume downloads, even after the app restarts.

## Features

- **Segmented downloads**: each file is fetched over up to 16 parallel
  connections (configurable), which is often much faster on servers that limit
  per-connection speed.
- **Pause / resume** that survives restarts. Progress is saved per segment.
- **Automatic retries** with backoff when a connection drops, resuming from the
  last byte received when the server supports range requests.
- **Smart file names** from the URL or the `Content-Disposition` header, with
  `name (1).ext` used rather than overwriting existing files.
- **Desktop window** (Tkinter): add by URL, paste from clipboard, live speed and
  ETA, pause/resume/remove, open folder, copy link, and a desktop notification
  (via `notify-send`) when a download finishes.
- **Command line** mode for quick downloads in a terminal.
- **Standard library only**: no `pip install` needed.

## Requirements

- Python 3.8 or newer
- For the window: Tkinter. On Debian/Ubuntu install it with
  `sudo apt install python3-tk`; on Fedora with `sudo dnf install python3-tkinter`.
- Optional: `notify-send` (libnotify) for completion notifications, and
  `xdg-open` for the "Open folder" button.

## Usage

Open the window:

```bash
python3 -m tsdm
```

Download from the terminal:

```bash
python3 -m tsdm https://example.com/file.iso -o ~/Downloads -n 8
```

Press Ctrl+C to pause a terminal download. Run the same command again to resume
it. Terminal downloads also appear in the window's list.

Options:

| Option | Meaning |
| --- | --- |
| `-o`, `--output-dir` | Folder to save into (default `~/Downloads`) |
| `-n`, `--segments` | Parallel connections per file, 1–32 (default 8) |

The download list is stored in `~/.local/share/tsdm/downloads.json`
(or `$XDG_DATA_HOME/tsdm/downloads.json`).

## How it works

1. **Probe**: one `GET` with `Range: bytes=0-0` reveals the file size, whether
   the server supports range requests (HTTP 206), the final URL after
   redirects, and the file name.
2. **Plan**: the file is split into byte ranges, and a `.part` file of the full
   size is pre-allocated.
3. **Fetch**: one thread per range requests its bytes and writes them at the
   correct offset. Each segment records how far it has got.
4. **Finish**: once all segments are complete, the `.part` file is renamed to
   its final name.

If the server does not support ranges, the download uses one connection and a
dropped transfer restarts from the beginning.

## Limitations

- No browser integration (it does not catch downloads started in a browser).
- HTTP and HTTPS only; no FTP, torrents, or video-site extraction.
- Servers that require logins or cookies are not supported yet.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests run a local HTTP server and cover parallel downloads, servers without
range support, resuming after a dropped connection, pause/resume, file naming,
and saving the download list.
