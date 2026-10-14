#!/usr/bin/env python3
"""Python AST parser and shared intra-file call resolution utilities."""
from __future__ import annotations

import ast
from typing import Any


def _first_docstring_line(node: ast.AST) -> str | None:
    doc = ast.get_docstring(node)
    if not doc:
        return None
    first = doc.strip().splitlines()[0].strip()
    return first or None


def _collect_imports(node: ast.AST, imports: list[str]) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.asname or alias.name
            if name and name not in imports:
                imports.append(name)
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module and module not in imports:
            imports.append(module)


def _unparse_expr(expr: ast.AST) -> str:
    try:
        return ast.unparse(expr)
    except Exception:
        if isinstance(expr, ast.Name):
            return expr.id
        if isinstance(expr, ast.Attribute):
            return f"{_unparse_expr(expr.value)}.{expr.attr}"
        if isinstance(expr, ast.Call):
            return f"{_unparse_expr(expr.func)}(...)"
        if isinstance(expr, ast.Subscript):
            return f"{_unparse_expr(expr.value)}[...]"
        return "?"


def _build_signature(node: ast.AST) -> str:
    try:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args_src = ast.unparse(node.args)
            returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            return f"{node.name}({args_src}){returns}"
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(_unparse_expr(b) for b in node.bases)
            return f"{node.name}({bases})" if bases else node.name
    except Exception:
        pass
    return getattr(node, "name", "?")


def _extract_calls(node: ast.AST) -> list[str]:
    calls: list[str] = []

    def _walk(n: ast.AST, top: bool) -> None:
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not top:
                continue
            if isinstance(child, ast.Call):
                target = _unparse_expr(child.func)
                if target and target not in calls:
                    calls.append(target)
            _walk(child, top=False)

    _walk(node, top=True)
    return calls


def _disambiguate_qualname(base: str, seen_qualnames: dict[str, int]) -> str:
    count = seen_qualnames.get(base, 0) + 1
    seen_qualnames[base] = count
    if count == 1:
        return base
    return f"{base}#{count}"


def _collect_symbols(
    body: list[ast.stmt],
    symbols: list[dict],
    inside_class: bool = False,
    class_name: str | None = None,
    seen_qualnames: dict[str, int] | None = None,
) -> None:
    if seen_qualnames is None:
        seen_qualnames = {}

    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            base_qualname = (
                f"{class_name}.{stmt.name}" if inside_class and class_name else stmt.name
            )
            qualname = _disambiguate_qualname(base_qualname, seen_qualnames)
            decorator_lines = [d.lineno for d in stmt.decorator_list]
            line_start = (
                min(decorator_lines + [stmt.lineno]) if decorator_lines else stmt.lineno
            )
            symbols.append(
                {
                    "kind": "method" if inside_class else "function",
                    "name": stmt.name,
                    "qualname": qualname,
                    "line_start": line_start,
                    "line_end": stmt.end_lineno or stmt.lineno,
                    "docstring": _first_docstring_line(stmt),
                    "signature": _build_signature(stmt),
                    "decorators": [_unparse_expr(d) for d in stmt.decorator_list],
                    "calls_out_raw": _extract_calls(stmt),
                }
            )
        elif isinstance(stmt, ast.ClassDef):
            qualname = _disambiguate_qualname(stmt.name, seen_qualnames)
            decorator_lines = [d.lineno for d in stmt.decorator_list]
            line_start = (
                min(decorator_lines + [stmt.lineno]) if decorator_lines else stmt.lineno
            )
            symbols.append(
                {
                    "kind": "class",
                    "name": stmt.name,
                    "qualname": qualname,
                    "line_start": line_start,
                    "line_end": stmt.end_lineno or stmt.lineno,
                    "docstring": _first_docstring_line(stmt),
                    "signature": _build_signature(stmt),
                    "base_classes": [_unparse_expr(b) for b in stmt.bases],
                }
            )
            _collect_symbols(
                stmt.body,
                symbols,
                inside_class=True,
                class_name=stmt.name,
                seen_qualnames=seen_qualnames,
            )


def _resolve_calls_intra_file(symbols: list[dict]) -> None:
    by_bare_name: dict[str, list[dict]] = {}
    methods_by_class: dict[str, dict[str, dict]] = {}
    for sym in symbols:
        if "calls_out_raw" not in sym:
            continue
        by_bare_name.setdefault(sym["name"], []).append(sym)
        qualname = sym.get("qualname", sym["name"])
        if sym["kind"] == "method" and "." in qualname:
            class_name = qualname.rsplit(".", 1)[0]
            methods_by_class.setdefault(class_name, {})[sym["name"]] = sym

    for sym in symbols:
        if "calls_out_raw" not in sym:
            continue
        resolved: list[str] = []
        unresolved: list[str] = []
        qualname = sym.get("qualname", sym["name"])
        own_class = (
            qualname.rsplit(".", 1)[0] if sym["kind"] == "method" and "." in qualname else None
        )

        for raw_call in sym["calls_out_raw"]:
            target_name = None
            receiver_rest = None
            if raw_call.startswith("self."):
                receiver_rest = raw_call[len("self."):]
            elif raw_call.startswith("this."):
                receiver_rest = raw_call[len("this."):]
            if receiver_rest is not None and own_class:
                method_name = receiver_rest.split(".")[0]
                sibling = methods_by_class.get(own_class, {}).get(method_name)
                if sibling is not None:
                    target_name = sibling.get("qualname", sibling["name"])
            if target_name is None and "." not in raw_call:
                matches = by_bare_name.get(raw_call, [])
                if len(matches) == 1:
                    target_name = matches[0].get("qualname", matches[0]["name"])

            if target_name is not None:
                if target_name not in resolved:
                    resolved.append(target_name)
            else:
                if raw_call not in unresolved:
                    unresolved.append(raw_call)

        sym["calls_out"] = resolved
        sym["calls_out_unresolved"] = unresolved


def _parse_python(content: str) -> tuple[list[str], list[dict]]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return [], []

    imports: list[str] = []
    symbols: list[dict] = []
    seen_qualnames: dict[str, int] = {}
    for node in tree.body:
        _collect_imports(node, imports)
    _collect_symbols(tree.body, symbols, seen_qualnames=seen_qualnames)
    _resolve_calls_intra_file(symbols)
    return imports, symbols
