import os
import re
import stat
from pathlib import Path, PurePosixPath

from .errors import ValidationError

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FORBIDDEN_CHARS = set('<>:"|?*\\')
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MAX_RELPATH = 400


def validate_relpath(raw: str) -> PurePosixPath:
    """Accept only portable, vault-relative, forward-slash paths.

    Rejects anything that could escape the vault or behave differently on
    Windows: absolute paths, drive letters, '..', alternate data streams (':'),
    reserved device names, trailing dots/spaces that Win32 silently strips.
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_RELPATH:
        raise ValidationError("BAD_PATH", f"invalid path: {raw!r}")
    if raw.startswith("/") or _CONTROL.search(raw):
        raise ValidationError("BAD_PATH", f"path must be vault-relative: {raw!r}")
    parts = raw.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValidationError("BAD_PATH", f"empty, '.' or '..' component in {raw!r}")
        if _FORBIDDEN_CHARS.intersection(part):
            raise ValidationError("BAD_PATH", f"forbidden character in {raw!r}")
        if part[-1] in ". " or len(part) > 255:
            raise ValidationError("BAD_PATH", f"component {part!r} not portable")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise ValidationError("BAD_PATH", f"reserved Windows name {part!r}")
    return PurePosixPath(*parts)


def is_link_like(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def resolve_in_vault(vault: Path, relpath: PurePosixPath) -> Path:
    """Map a validated relpath into the vault, refusing symlinks/junctions on the way."""
    current = vault
    for part in relpath.parts:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            return vault.joinpath(*relpath.parts)
        if is_link_like(st):
            raise ValidationError("LINK_IN_PATH", f"symlink or junction in path: {current.relative_to(vault).as_posix()}")  # vault-relative: no local layout leaks
    return current


def fold(relpath: PurePosixPath) -> str:
    """Case-folded key: Windows/NTFS treats 'A.md' and 'a.md' as the same file."""
    return str(relpath).casefold()
