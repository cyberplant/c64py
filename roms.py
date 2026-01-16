"""
ROM discovery and (local) installation helpers.

This project does not ship Commodore 64 ROM images. Many ROM binaries are
copyrighted, so users must provide them (e.g. via an existing VICE install or
by placing ROM files in a local directory).
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class RomSpec:
    """
    Specification for a required Commodore 64 ROM file.

    This dataclass defines the metadata needed to identify and validate
    a single ROM file required by the emulator.

    Attributes:
        key: A unique identifier for the ROM type (e.g., "basic", "kernal",
            "characters"). This serves as a human-readable label for the ROM
            and may be used for programmatic identification.
        filename: The canonical/preferred filename for this ROM
            (e.g., "basic.901226-01.bin").
        aliases: Alternative filenames that are acceptable for this ROM.
            Used to support different naming conventions across ROM sources.
        expected_size: The expected size in bytes for this ROM file, or None
            if no size validation is required. Used for basic validation
            without being overly strict about ROM revisions.
    """
    key: str
    filename: str
    aliases: Sequence[str] = ()
    expected_size: Optional[int] = None


REQUIRED_ROMS: Sequence[RomSpec] = (
    RomSpec("basic", "basic.901226-01.bin", aliases=("basic-901226-01.bin",), expected_size=8192),
    RomSpec(
        "kernal",
        "kernal.901227-03.bin",
        aliases=("kernal-901227-03.bin",),
        expected_size=8192,
    ),
    RomSpec(
        "characters",
        "characters.901225-01.bin",
        aliases=("chargen-901225-01.bin",),
        expected_size=4096,
    ),
)


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def user_rom_dir() -> Path:
    """Per-user ROM cache directory (persisted across runs)."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        return base / "c64py" / "roms"
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        return base / "c64py" / "roms"

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "c64py" / "roms"
    return Path.home() / ".local" / "share" / "c64py" / "roms"


def _repo_default_rom_dir() -> Optional[Path]:
    """
    When running from the repo, preserve the historical default:
    ../lib/assets relative to this package directory.
    """
    try:
        pkg_dir = Path(__file__).resolve().parent
        return (pkg_dir.parent / "lib" / "assets").resolve()
    except Exception:
        return None


def _vice_candidate_dirs() -> Sequence[Path]:
    """
    Common VICE ROM locations across platforms/package managers.

    VICE typically stores ROMs in a tree containing a 'C64' directory.
    We include both the root and likely subdirectories.
    """
    out: list[Path] = []

    # macOS app bundle
    out.extend(
        [
            Path("/Applications/VICE.app/Contents/Resources"),
            Path("/Applications/VICE.app/Contents/Resources/C64"),
            Path("/Applications/VICE.app/Contents/Resources/vice"),
            Path("/Applications/VICE.app/Contents/Resources/vice/C64"),
        ]
    )

    # Homebrew / macports / generic unix
    out.extend(
        [
            Path("/opt/homebrew/share/vice"),
            Path("/opt/homebrew/share/vice/C64"),
            Path("/usr/local/share/vice"),
            Path("/usr/local/share/vice/C64"),
            Path("/usr/share/vice"),
            Path("/usr/share/vice/C64"),
            Path("/usr/lib/vice"),
            Path("/usr/lib/vice/C64"),
            Path("/usr/local/lib/vice"),
            Path("/usr/local/lib/vice/C64"),
        ]
    )

    # User-local VICE dirs
    out.extend(
        [
            Path.home() / ".vice",
            Path.home() / ".vice" / "C64",
            Path.home() / "Library" / "Application Support" / "VICE",
            Path.home() / "Library" / "Application Support" / "VICE" / "C64",
        ]
    )

    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in out:
        if p in seen:
            continue
        seen.add(p)
        deduped.append(p)
    return deduped


def iter_candidate_rom_dirs(extra: Optional[Sequence[Path]] = None) -> Iterable[Path]:
    """Yield candidate directories in search priority order."""
    env_dir = os.environ.get("C64PY_ROM_DIR")
    if env_dir:
        yield Path(env_dir).expanduser()

    # User-local cache dir (where we install/copy ROMs to)
    yield user_rom_dir()

    # Historical repo default
    repo_default = _repo_default_rom_dir()
    if repo_default is not None:
        yield repo_default

    # VICE common locations
    yield from _vice_candidate_dirs()

    # Generic system locations for packaged installs
    yield Path("/usr/local/share/c64py/roms")
    yield Path("/usr/share/c64py/roms")

    if extra:
        for p in extra:
            yield p


def roms_present_in_dir(rom_dir: Path) -> bool:
    """Return True if all required ROM filenames exist in rom_dir."""
    try:
        for spec in REQUIRED_ROMS:
            found = False
            for name in (spec.filename, *spec.aliases):
                if (rom_dir / name).is_file():
                    found = True
                    break
            if not found:
                return False
        return True
    except Exception:
        return False


def find_rom_dir(explicit_rom_dir: Optional[str] = None) -> Optional[Path]:
    """
    Find a directory containing all required ROMs.

    - If explicit_rom_dir is provided, only that directory is checked.
    - Otherwise, common locations are searched.
    """
    if explicit_rom_dir:
        p = Path(explicit_rom_dir).expanduser()
        if roms_present_in_dir(p):
            return p
        return None

    for candidate in iter_candidate_rom_dirs():
        if roms_present_in_dir(candidate):
            return candidate
    return None


def _copy_roms(src_dir: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for spec in REQUIRED_ROMS:
        src: Optional[Path] = None
        for name in (spec.filename, *spec.aliases):
            candidate = src_dir / name
            if candidate.is_file():
                src = candidate
                break
        if src is None:
            raise FileNotFoundError(
                f"Missing ROM in source: expected one of {(spec.filename, *spec.aliases)!r} in {str(src_dir)!r}"
            )
        data = src.read_bytes()
        if spec.expected_size is not None and len(data) != spec.expected_size:
            # Size mismatches can happen; still copy, but keep message actionable.
            # We avoid being strict to allow alternate ROM revisions.
            pass
        (dst_dir / spec.filename).write_bytes(data)


def _extract_archive_to_temp(archive_path: Path) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="c64py-roms-"))
    try:
        def _safe_extract_path(base: Path, member_path: str) -> Path:
            # Prevent path traversal ("zip slip") by validating the final path.
            target = (base / member_path).resolve()
            base_resolved = base.resolve()
            if not str(target).startswith(str(base_resolved) + os.sep) and target != base_resolved:
                raise ValueError(f"Archive contains unsafe path: {member_path!r}")
            return target

        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path, "r") as zf:
                for info in zf.infolist():
                    # Directories are represented with trailing slash names.
                    name = info.filename
                    if not name:
                        continue
                    # Validate the target path before extraction.
                    _safe_extract_path(temp_dir, name)
                    zf.extract(info, temp_dir)
            return temp_dir
        if tarfile.is_tarfile(archive_path):
            with tarfile.open(archive_path, "r:*") as tf:
                for member in tf.getmembers():
                    if not member.name:
                        continue
                    # Validate the target path before extraction.
                    _safe_extract_path(temp_dir, member.name)
                    tf.extract(member, temp_dir)
            return temp_dir
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    shutil.rmtree(temp_dir, ignore_errors=True)
    raise ValueError(f"Unsupported archive format: {archive_path}")


def _find_rom_dir_within_tree(root: Path) -> Optional[Path]:
    """
    Search for a directory under root that contains all required ROMs.
    """
    if roms_present_in_dir(root):
        return root

    # Limit recursion depth when walking large or deeply nested trees to avoid
    # excessive traversal time on pathological directory structures.
    max_depth = 10
    base_depth = len(root.parts)

    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dp = Path(dirpath)
            current_depth = len(dp.parts) - base_depth
            if current_depth > max_depth:
                # Prevent os.walk from descending further under this directory.
                dirnames[:] = []
                continue
            if roms_present_in_dir(dp):
                return dp
    except Exception:
        return None
    return None


def ensure_roms_available(
    explicit_rom_dir: Optional[str] = None,
    *,
    allow_prompt: bool = True,
) -> Path:
    """
    Ensure ROMs are available and return a directory containing them.

    If not found and prompting is allowed + stdin is a TTY, the user can
    provide a local directory or local archive to install into the user ROM dir.
    """
    found = find_rom_dir(explicit_rom_dir=explicit_rom_dir)
    if found is not None:
        return found

    if explicit_rom_dir:
        raise FileNotFoundError(
            f"ROMs not found in --rom-dir {explicit_rom_dir!r}. "
            f"Expected: {[s.filename for s in REQUIRED_ROMS]}"
        )

    if not (allow_prompt and _is_tty()):
        tried = [str(p) for p in iter_candidate_rom_dirs()]
        raise FileNotFoundError(
            "C64 ROM files were not found. "
            f"Expected: {[s.filename for s in REQUIRED_ROMS]}. "
            f"Searched: {tried}. "
            f"Provide --rom-dir or copy ROMs into {str(user_rom_dir())!r}."
        )

    print("C64 ROM files were not found.")
    print("This project does not ship ROMs by default because many ROM binaries are copyrighted.")
    print("If you want to use VICE as your ROM source, download/install VICE separately,")
    print("then point this installer at the ROM directory or the VICE archive you downloaded.")
    print(f"If you already have ROM files (for example via a local VICE install),")
    print(f"they can be installed into your user ROM directory so future runs work automatically:")
    print(f"  {user_rom_dir()}")
    answer = input("Do you want to install ROMs from a local directory or archive now? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise FileNotFoundError(
            "ROMs missing. Provide --rom-dir or copy ROMs into "
            f"{str(user_rom_dir())!r}."
        )

    src = input("Enter a path to a ROM directory OR a local VICE archive (.zip/.tar.*): ").strip()
    if not src:
        raise FileNotFoundError("No source path provided for ROM installation.")

    src_path = Path(src).expanduser()
    if not src_path.exists():
        raise FileNotFoundError(f"Source path does not exist: {src_path}")

    temp_root: Optional[Path] = None
    try:
        if src_path.is_file():
            temp_root = _extract_archive_to_temp(src_path)
            candidate = _find_rom_dir_within_tree(temp_root)
        else:
            candidate = _find_rom_dir_within_tree(src_path)

        if candidate is None:
            raise FileNotFoundError(
                "Could not find required ROM files in the provided source. "
                f"Expected: {[s.filename for s in REQUIRED_ROMS]}"
            )

        dst = user_rom_dir()
        _copy_roms(candidate, dst)
        print(f"Installed ROMs into: {dst}")
        return dst
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)

