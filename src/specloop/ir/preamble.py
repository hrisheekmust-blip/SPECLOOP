"""PreambleCapsule: captures per-file compile context (timescale, defines, includes)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_TIMESCALE_RE = re.compile(r"`timescale\s+(\S+)\s*/\s*(\S+)")
_DEFINE_RE = re.compile(r"`define\s+(\w+)(?:\s+(.+))?")
_INCLUDE_DIR_RE = re.compile(r"\+incdir\+(\S+)")
_DEFAULT_NETTYPE_RE = re.compile(r"`default_nettype\s+(\w+)")


@dataclass
class PreambleCapsule:
    timescale: Optional[str] = None          # e.g. "1ns/1ps"
    defines: dict[str, str] = field(default_factory=dict)
    include_dirs: list[str] = field(default_factory=list)
    default_nettype: str = "wire"

    @classmethod
    def from_file(cls, path: Path) -> "PreambleCapsule":
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return cls()
        return cls.from_text(text)

    @classmethod
    def from_text(cls, text: str) -> "PreambleCapsule":
        cap = cls()
        for line in text.splitlines():
            stripped = line.strip()
            m = _TIMESCALE_RE.search(stripped)
            if m:
                cap.timescale = f"{m.group(1)}/{m.group(2)}"
            m = _DEFINE_RE.match(stripped)
            if m:
                cap.defines[m.group(1)] = (m.group(2) or "").strip()
            m = _INCLUDE_DIR_RE.search(stripped)
            if m:
                cap.include_dirs.append(m.group(1))
            m = _DEFAULT_NETTYPE_RE.search(stripped)
            if m:
                cap.default_nettype = m.group(1)
        return cap

    def merge(self, other: "PreambleCapsule") -> "PreambleCapsule":
        """Return a new capsule combining self with other (other wins on conflicts)."""
        merged = PreambleCapsule(
            timescale=other.timescale or self.timescale,
            defines={**self.defines, **other.defines},
            include_dirs=list(dict.fromkeys(self.include_dirs + other.include_dirs)),
            default_nettype=other.default_nettype if other.default_nettype != "wire" else self.default_nettype,
        )
        return merged
