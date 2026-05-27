"""Vendor blackbox library: maps unknown module names to stub .sv files."""
from __future__ import annotations

from pathlib import Path

# Shipped with the package; resolved relative to this file's location
_VENDOR_DIR = Path(__file__).parent.parent.parent.parent / "vendor_blackboxes"


def get_vendor_stubs(names: list[str], vendor_dir: Path | None = None) -> list[Path]:
    """
    Return paths to vendor stub files that define any of `names`.
    Logs a warning for names with no known stub.
    """
    import logging
    log = logging.getLogger(__name__)

    vdir = vendor_dir or _VENDOR_DIR
    if not vdir.exists():
        return []

    # Build an index: module_name -> stub_file
    index = _build_index(vdir)

    stubs: list[Path] = []
    seen_files: set[Path] = set()
    for name in names:
        stub = index.get(name.lower())
        if stub and stub not in seen_files:
            stubs.append(stub)
            seen_files.add(stub)
        elif stub is None:
            log.warning("No vendor stub for module '%s' — will rely on Yosys blackbox pass", name)
    return stubs


def _build_index(vdir: Path) -> dict[str, Path]:
    """Scan vendor_blackboxes/ for module declarations and build name→file map."""
    import re
    _MODULE_RE = re.compile(r"^\s*module\s+(\w+)", re.MULTILINE)
    index: dict[str, Path] = {}
    for sv_file in vdir.glob("*.sv"):
        try:
            text = sv_file.read_text()
            for m in _MODULE_RE.finditer(text):
                index[m.group(1).lower()] = sv_file
        except OSError:
            pass
    return index
