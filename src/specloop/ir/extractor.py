"""pyslang-based RTL extractor: source files → list[ModuleIR]."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import pyslang

from specloop.ir.schema import AlwaysBlock, ModuleIR, Parameter, Port, SubmoduleInstance

_CLOCK_PATTERNS = re.compile(r"^(clk|clock|clk_i|sys_clk|pclk|aclk|hclk)$", re.I)
_RESET_PATTERNS = re.compile(r"^(rst|reset|rst_n|rst_ni|aresetn|resetn|nreset|reset_n)$", re.I)
_LOW_POLARITY = re.compile(r"_n$|_ni$|_bar$|n$", re.I)

_ALWAYS_KIND_MAP = {
    "AlwaysFFBlock": "always_ff",
    "AlwaysCombBlock": "always_comb",
    "AlwaysLatchBlock": "always_latch",
    "AlwaysBlock": "always",
}


def _direction_str(direction) -> str:
    s = str(direction)
    if "Out" in s:
        return "output"
    if "InOut" in s:
        return "inout"
    return "input"


def _is_clock(name: str) -> bool:
    return bool(_CLOCK_PATTERNS.match(name))


def _is_reset(name: str) -> tuple[bool, Optional[str]]:
    if _RESET_PATTERNS.match(name):
        polarity = "low" if _LOW_POLARITY.search(name) else "high"
        return True, polarity
    return False, None


def _extract_sensitivity(timing_control_node) -> tuple[list[str], bool]:
    """Return (sensitivity_list_strings, has_async_reset) from a timing control syntax node."""
    try:
        data = json.loads(timing_control_node.to_json())
    except Exception:
        return [], False

    signals: list[str] = []
    has_negedge_reset = False

    def walk(node):
        nonlocal has_negedge_reset
        if not isinstance(node, dict):
            return
        if node.get("kind") == "SignalEventExpression":
            edge = node.get("edge", {})
            edge_kind = edge.get("kind", "")
            expr = node.get("expr", {})
            ident = expr.get("identifier", {})
            sig_name = ident.get("text", "").strip()
            if sig_name:
                prefix = "posedge " if "PosEdge" in edge_kind else "negedge " if "NegEdge" in edge_kind else ""
                signals.append(prefix + sig_name)
                if "NegEdge" in edge_kind and _is_reset(sig_name)[0]:
                    has_negedge_reset = True
        for v in node.values():
            if isinstance(v, dict):
                walk(v)
            elif isinstance(v, list):
                for item in v:
                    walk(item)

    walk(data)
    return signals, has_negedge_reset


def _extract_always_blocks(syn_members) -> list[AlwaysBlock]:
    blocks: list[AlwaysBlock] = []
    for m in syn_members:
        kind_name = type(m).__name__
        kind_str = None
        for suffix, mapped in _ALWAYS_KIND_MAP.items():
            if str(m.kind).endswith(suffix):
                kind_str = mapped
                break
        if kind_str is None:
            continue

        sensitivity: list[str] = []
        has_async_reset = False
        try:
            stmt = m.statement
            if hasattr(stmt, "timingControl") and stmt.timingControl is not None:
                sensitivity, has_async_reset = _extract_sensitivity(stmt.timingControl)
        except Exception:
            pass

        blocks.append(AlwaysBlock(kind=kind_str, sensitivity=sensitivity, has_async_reset=has_async_reset))
    return blocks


def _extract_submodules(syn_members) -> list[SubmoduleInstance]:
    subs: list[SubmoduleInstance] = []
    for m in syn_members:
        if "HierarchyInstantiation" not in str(m.kind):
            continue
        module_name = str(m.type).strip()
        for hi in m.instances:
            try:
                inst_name = str(hi.decl.name).strip()
            except Exception:
                inst_name = "unknown"
            subs.append(SubmoduleInstance(instance_name=inst_name, module_name=module_name))
    return subs


def _extract_imports(syn_members) -> list[str]:
    imports: list[str] = []
    for m in syn_members:
        kind = str(m.kind)
        if "Import" in kind or "WildcardImport" in kind:
            try:
                imports.append(str(m.getFirstToken()).strip())
            except Exception:
                pass
    return imports


def _body_to_ir(body, file_path: str, sm) -> ModuleIR:
    """Extract ModuleIR from an elaborated InstanceBodySymbol."""
    syn = body.syntax
    start_line = sm.getLineNumber(syn.sourceRange.start)
    end_line = sm.getLineNumber(syn.sourceRange.end)

    # Ports
    ports: list[Port] = []
    for p in body.portList:
        try:
            t = p.type
            width = int(t.bitstreamWidth) if hasattr(t, "bitstreamWidth") else 1
        except Exception:
            width = 1
        name = p.name
        direction = _direction_str(p.direction)
        is_clk = _is_clock(name)
        is_rst, pol = _is_reset(name)
        ports.append(Port(name=name, direction=direction, width=width,
                          is_clock=is_clk, is_reset=is_rst, reset_polarity=pol))

    # Parameters
    params: list[Parameter] = []
    for param in body.parameters:
        try:
            val = str(param.value) if param.value is not None else None
        except Exception:
            val = None
        params.append(Parameter(name=param.name, default=val))

    members = list(syn.members)
    always_blocks = _extract_always_blocks(members)
    submodules = _extract_submodules(members)
    imports = _extract_imports(members)

    return ModuleIR(
        module=body.name,
        file=file_path,
        lines=(start_line, end_line),
        parameters=params,
        ports=ports,
        always_blocks=always_blocks,
        submodules=submodules,
        imports=imports,
        parse_status="ok",
        confidence=1.0,
    )


def _walk_hierarchy(inst_sym, sm, seen: dict[str, ModuleIR], file_map: dict[str, str]) -> None:
    """Recursively walk the elaborated instance hierarchy, collecting one IR per module definition."""
    if not hasattr(inst_sym, "body"):
        return
    body = inst_sym.body
    def_name = body.name
    if def_name in seen:
        return

    try:
        loc = body.syntax.sourceRange.start
        file_path = file_map.get(def_name, sm.getFileName(loc))
        ir = _body_to_ir(body, file_path, sm)
        seen[def_name] = ir
    except Exception as exc:
        seen[def_name] = ModuleIR(
            module=def_name,
            file=file_map.get(def_name, "unknown"),
            parse_status="partial",
            confidence=0.5,
        )

    # Recurse into children
    for m in body.syntax.members:
        if "HierarchyInstantiation" not in str(m.kind):
            continue
        for hi in m.instances:
            try:
                inst_name = str(hi.decl.name).strip()
                child = body.find(inst_name)
                if child is not None:
                    _walk_hierarchy(child, sm, seen, file_map)
            except Exception:
                pass


def extract_modules(path: Path) -> list[ModuleIR]:
    """
    Parse all .sv/.v files under `path` (or a single .f filelist) and return
    one ModuleIR per unique module definition.  Never raises — failed files
    produce ModuleIR(parse_status='failed').
    """
    # Collect source files
    if path.is_file() and path.suffix == ".f":
        src_files = [
            Path(line.strip())
            for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("//")
        ]
    elif path.is_file():
        src_files = [path]
    else:
        src_files = sorted(path.rglob("*.sv")) + sorted(path.rglob("*.v"))

    if not src_files:
        return []

    comp = pyslang.ast.Compilation()
    failed: list[ModuleIR] = []
    file_map: dict[str, str] = {}  # module_name -> file_path (populated after parse)

    for src in src_files:
        try:
            tree = pyslang.syntax.SyntaxTree.fromFile(str(src))
            comp.addSyntaxTree(tree)
        except Exception as exc:
            failed.append(ModuleIR(
                module=src.stem,
                file=str(src),
                parse_status="failed",
                confidence=0.0,
            ))

    # sourceManager is only valid after trees are added
    sm = comp.sourceManager

    # Collect ALL definitions before elaboration — calling getDefinitions() after
    # getRoot() corrupts pyslang's internal state for subsequent mini compilations.
    all_defs: list = []
    try:
        all_defs = list(comp.getDefinitions())
        for defn in all_defs:
            loc = defn.syntax.sourceRange.start
            file_map[defn.name] = sm.getFileName(loc)
    except Exception:
        pass

    # Walk elaborated hierarchy
    seen: dict[str, ModuleIR] = {}
    root = comp.getRoot()
    for inst in root.topInstances:
        _walk_hierarchy(inst, sm, seen, file_map)

    # Any definition not reachable from a top instance (e.g. inside generate blocks)
    # — force-elaborate it by setting topModules in a fresh compilation.
    # IMPORTANT: extract all data from DefinitionSymbol objects into plain Python
    # strings before creating mini compilations — live C++ DefinitionSymbol refs
    # held across pyslang elaboration calls corrupt shared internal state.
    src_trees = comp.getSyntaxTrees()
    unseen = [(defn.name, file_map.get(defn.name, "unknown"))
              for defn in all_defs if defn.name not in seen]
    del all_defs  # release C++ DefinitionSymbol refs before mini compilations
    for name, file_path in unseen:
        try:
            d = pyslang.driver.Driver()
            d.addStandardArgs()
            bag = d.createOptionBag()
            bag.compilationOptions.topModules = {name}
            mini = pyslang.ast.Compilation(bag)
            for t in src_trees:
                mini.addSyntaxTree(t)
            mini_sm = mini.sourceManager
            for mini_inst in mini.getRoot().topInstances:
                if mini_inst.name == name:
                    ir = _body_to_ir(mini_inst.body, file_path, mini_sm)
                    seen[name] = ir
                    break
        except Exception:
            if name not in seen:
                seen[name] = ModuleIR(
                    module=name,
                    file=file_path,
                    parse_status="partial",
                    confidence=0.5,
                )

    return failed + list(seen.values())
