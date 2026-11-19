"""One OS advisory lock per Core data directory, without deleting lock files."""
import errno
import os
from pathlib import Path
from .privacy import private_file


class SingleInstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    def acquire(self):
        if self._fd is not None:
            return self
        private_file(self.path)
        fd = os.open(self.path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        try:
            # Refuse a path replaced between validation and open.
            opened, named = os.fstat(fd), self.path.lstat()
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise OSError(errno.ELOOP, '锁文件在打开时发生变化', str(self.path))
            if os.name == 'nt':
                import msvcrt
                if opened.st_size == 0:
                    os.write(fd, b'\0')
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise BlockingIOError(errno.EWOULDBLOCK,
                                              '该数据目录已有运行中的进程') from None
                    raise
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    close = release

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
