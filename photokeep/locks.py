"""Advisory file locks, skipped where the platform or the filesystem has none."""
import errno

try:
    import fcntl
except ImportError:  # Windows: no flock, so writers are not serialized (best effort)
    fcntl = None  # type: ignore[assignment]

# Filesystems without advisory locks (some network and FUSE mounts) answer flock with these.
_UNSUPPORTED = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOLCK, errno.EINVAL})


def flock(fd: int, wait: bool = True) -> None:
    """Lock fd exclusively until it is closed; without `wait`, BlockingIOError if another holds it."""
    if fcntl is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except OSError as e:
        if e.errno not in _UNSUPPORTED:
            raise
