"""TrainingLogger: append-only JSONL log of formally-verified training examples.

Usage (called by the formal verification layer once SBY confirms a proof):

    from specloop.training.logger import TrainingLogger
    from specloop.training.schema import ProvenPair, ProofSummary

    logger = TrainingLogger(Path("work/training_data.jsonl"))
    logger.log(ProvenPair(
        module_name="counter",
        module_type="sequential",
        file_path="rtl/counter.sv",
        rtl_source=rtl_text,
        module_ir=ir.model_dump(),
        bind_module_sv=bind_sv,
        assertion_index=[...],
        proof=ProofSummary(status="all_proven", proven=5, total=5, depth=20),
        model_id="CodeV-CodeQwen-7B-AWQ",
    ))

Export for fine-tuning:

    logger.export_flat(Path("ft_data/flat.jsonl"))
    logger.export_chat(Path("ft_data/chat.jsonl"))
"""
from __future__ import annotations

import fcntl
import json
import logging
from pathlib import Path
from typing import Union

from specloop.training.schema import ProvenPair, RepairStep

log = logging.getLogger(__name__)

TrainingRecord = Union[ProvenPair, RepairStep]

# Records with proof.status in this set are stored as ProvenPair (includes pending pre-SBY)
_STORED_STATUSES = {"all_proven", "partial", "pending"}


class TrainingLogger:
    """Append-only JSONL store for training records.

    Thread-safe via file-level locking (fcntl). Each call to log() is atomic:
    one complete JSON line is written and flushed before the lock is released.
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def log(self, record: TrainingRecord) -> bool:
        """Append `record` to the JSONL log.

        Returns True if written, False if skipped (duplicate or below-threshold).
        Proven pairs with proof.status not in _PROVEN_STATUSES are silently
        skipped — there is no value in training on unverified examples.
        """
        if isinstance(record, ProvenPair):
            if record.proof.status not in _STORED_STATUSES:
                log.debug("Skipping unproven record for '%s'", record.module_name)
                return False
            if self._is_duplicate(record.module_name, record.rtl_hash):
                log.debug("Skipping duplicate record for '%s' (%s)", record.module_name, record.rtl_hash)
                return False

        line = record.model_dump_json()
        self._append_line(line)
        log.info(
            "Logged %s record for '%s' (proven=%s/%s, iter=%s)",
            record.record_type,
            record.module_name,
            getattr(getattr(record, "proof", None), "proven", "—"),
            getattr(getattr(record, "proof", None), "total", "—"),
            getattr(record, "repair_iterations", getattr(record, "iteration", 0)),
        )
        return True

    def _append_line(self, line: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(line + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _is_duplicate(self, module_name: str, rtl_hash: str) -> bool:
        """Return True only if a non-pending proven pair already exists.

        Pending records are allowed to be superseded by a proven/partial record
        once SBY confirms the assertions.
        """
        if not self.log_path.exists():
            return False
        for raw in self._iter_lines():
            if raw.get("record_type") == "proven_pair":
                if (raw.get("module_name") == module_name
                        and raw.get("rtl_hash") == rtl_hash
                        and raw.get("proof", {}).get("status") != "pending"):
                    return True
        return False

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def _iter_lines(self):
        """Yield parsed dicts for every valid line in the log."""
        if not self.log_path.exists():
            return
        with self.log_path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Malformed JSON at line %d in %s — skipping", lineno, self.log_path)

    def load_proven_pairs(self) -> list[ProvenPair]:
        return [ProvenPair.model_validate(r) for r in self._iter_lines() if r.get("record_type") == "proven_pair"]

    def load_repair_steps(self) -> list[RepairStep]:
        return [RepairStep.model_validate(r) for r in self._iter_lines() if r.get("record_type") == "repair_step"]

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_flat(self, out: Path, min_confidence: float = 0.0) -> int:
        """Write Alpaca-style flat JSONL (instruction/input/output).

        Returns number of records written.
        """
        records = self._export_records(min_confidence)
        return self._write_jsonl(out, [r.to_flat() for r in records])

    def export_chat(self, out: Path, min_confidence: float = 0.0) -> int:
        """Write OpenAI messages-style JSONL for chat fine-tuning (Axolotl / TRL).

        Returns number of records written.
        """
        records = self._export_records(min_confidence)
        return self._write_jsonl(out, [r.to_chat() for r in records])

    def _export_records(self, min_confidence: float) -> list[TrainingRecord]:
        out: list[TrainingRecord] = []
        for raw in self._iter_lines():
            rt = raw.get("record_type")
            try:
                if rt == "proven_pair":
                    rec = ProvenPair.model_validate(raw)
                    if rec.proof.status == "pending":
                        continue  # not yet verified — skip export
                    if rec.proof.proven / max(rec.proof.total, 1) >= max(min_confidence, 0.0):
                        out.append(rec)
                elif rt == "repair_step":
                    rec = RepairStep.model_validate(raw)
                    if rec.repair_succeeded:   # only include successful repairs
                        out.append(rec)
            except Exception as exc:
                log.warning("Skipping malformed record: %s", exc)
        return out

    @staticmethod
    def _write_jsonl(out: Path, rows: list[dict]) -> int:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return len(rows)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return a summary dict for the CLI stats command."""
        proven: list[dict] = []
        repair: list[dict] = []
        for raw in self._iter_lines():
            if raw.get("record_type") == "proven_pair":
                proven.append(raw)
            elif raw.get("record_type") == "repair_step":
                repair.append(raw)

        module_types: dict[str, int] = {}
        total_proven = 0
        total_assertions = 0
        models_seen: set[str] = set()

        for r in proven:
            mt = r.get("module_type", "unknown")
            module_types[mt] = module_types.get(mt, 0) + 1
            proof = r.get("proof", {})
            total_proven += proof.get("proven", 0)
            total_assertions += proof.get("total", 0)
            if r.get("model_id"):
                models_seen.add(r["model_id"])

        repair_succeeded = sum(1 for r in repair if r.get("repair_succeeded"))

        return {
            "proven_pairs": len(proven),
            "repair_steps": len(repair),
            "repair_steps_successful": repair_succeeded,
            "total_assertions_proven": total_proven,
            "total_assertions": total_assertions,
            "module_type_breakdown": module_types,
            "models": list(models_seen),
            "log_path": str(self.log_path),
            "log_size_kb": round(self.log_path.stat().st_size / 1024, 1) if self.log_path.exists() else 0,
        }
