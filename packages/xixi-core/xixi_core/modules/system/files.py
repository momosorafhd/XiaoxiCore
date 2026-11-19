"""Small ordinary-file reader retained from the old telemetry support code."""
import os
from pathlib import Path
import stat


def no_links(path, *, must_exist=True):
    """Keep the old ordinary-file boundary for module-owned SQLite files."""
    path = Path(path).absolute()
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if must_exist and item == path:
                raise
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError("Resource path contains a link")
    return path


def open_regular(path):
    """Open the CPU-mode label, rejecting links and files changed during open."""
    path = no_links(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise OSError("CPU mode label is not an ordinary file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    stream = os.fdopen(descriptor, "rb")
    actual = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino) or actual.st_nlink != 1:
        stream.close()
        raise OSError("CPU mode file changed during read")
    return stream
