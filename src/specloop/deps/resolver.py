"""Dependency resolver: builds a NetworkX DAG over ModuleIR and computes ordered closures."""
from __future__ import annotations

from pathlib import Path

import networkx as nx

from specloop.ir.schema import ModuleIR


class DependencyResolver:
    def __init__(self, irs: list[ModuleIR]) -> None:
        self._ir_map: dict[str, ModuleIR] = {ir.module: ir for ir in irs}
        self._graph: nx.DiGraph = nx.DiGraph()
        for ir in irs:
            self._graph.add_node(ir.module)
            for sub in ir.submodules:
                self._graph.add_edge(ir.module, sub.module_name)
                # Ensure referenced nodes exist even if no IR was parsed for them
                if sub.module_name not in self._graph:
                    self._graph.add_node(sub.module_name)

    # ------------------------------------------------------------------
    def closure(self, top: str) -> list[str]:
        """
        Return the dependency-ordered file list for `top`: leaves first, top last.
        Includes `top` itself.  Raises KeyError if `top` is unknown.
        """
        if top not in self._graph:
            raise KeyError(f"Module '{top}' not found in dependency graph")
        reachable = nx.descendants(self._graph, top) | {top}
        subgraph = self._graph.subgraph(reachable)
        return list(reversed(list(nx.topological_sort(subgraph))))

    def missing(self, top: str) -> list[str]:
        """Return module names referenced in the closure that have no parsed IR."""
        return [name for name in self.closure(top) if name not in self._ir_map]

    def write_filelist(self, top: str, out: Path) -> Path:
        """Write an ordered .f filelist for `top` to `out`. Returns `out`."""
        ordered = self.closure(top)
        lines: list[str] = []
        for name in ordered:
            ir = self._ir_map.get(name)
            if ir and ir.file and ir.file != "unknown":
                lines.append(ir.file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n")
        return out

    def all_modules(self) -> list[str]:
        return list(self._ir_map.keys())

    def get_ir(self, name: str) -> ModuleIR | None:
        return self._ir_map.get(name)

    def roots(self) -> list[str]:
        """Modules not instantiated by any other module in the graph."""
        return [n for n in self._graph.nodes if self._graph.in_degree(n) == 0]
