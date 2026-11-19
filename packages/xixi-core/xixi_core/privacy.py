"""Persistence checks that never open an existing SQLite inode.

Linux production enforces owner-only POSIX mode. Windows development validates
regular non-reparse files and inherits NTFS ACLs; chmod is not an ACL boundary.
The Windows branch must not be described as equivalent to Linux isolation.
"""
import os
import errno
from pathlib import Path
import stat


PRIVACY_MODEL = 'windows-inherited-acl' if os.name == 'nt' else 'posix-owner-only'


def _is_reparse(info):
    return bool(getattr(info, 'st_file_attributes', 0) &
                getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def private_directory(path):
    """Validate a real directory; enforce UID and mode only on POSIX."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError('持久目录必须为真实目录，不能为符号链接或重解析点')
    if os.name != 'nt':
        if info.st_uid != os.getuid():
            raise ValueError('持久目录必须属于当前用户')
        os.chmod(path, 0o700, follow_symlinks=False)
    return path


def private_file(path, create=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return
        # Closing ANY descriptor for an existing SQLite/SHM inode would drop
        # process-wide POSIX advisory locks. Only create with O_EXCL here.
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise OSError(errno.ELOOP, '持久路径不能为符号链接或重解析点', str(path))
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('持久文件必须为普通文件')
    if os.name != 'nt':
        if info.st_uid != os.getuid():
            raise ValueError('持久文件必须为当前用户所有的普通文件')
        # Never open/close the live database or its -wal/-shm files here.
        os.chmod(path, 0o600, follow_symlinks=False)


def private_sqlite(path):
    if str(path) == ':memory:':
        return
    private_file(path)
    for suffix in ('-wal', '-shm', '-journal'):
        private_file(str(path) + suffix, create=False)
