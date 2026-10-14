#!/usr/bin/env python3
"""Build config/routing_situations.json (Tier1 catalogue) from the decision-table source.

Dev-only: reads knowledge/plans/model-router_complete/, which is never synced to the plugin.

    source.json   -> id, family, situation (q/f of each row, row order)
    overlay.json  -> families/policy/escalation blocks, dropped ids, and the
                     hand-decided fields per row (r, i, n, th, m, t, v, c)

Usage:
    build_routing_situations.py            write the catalogue
    build_routing_situations.py --check    exit 1 if the catalogue is stale
    build_routing_situations.py --init-overlay
                                           one-time: derive the overlay from the
                                           current catalogue (refuses to overwrite)
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLANS = ROOT / "knowledge" / "plans" / "model-router_complete"
SOURCE = PLANS / "decision-table-source.json"
OVERLAY = PLANS / "decision-table-overlay.json"
CATALOGUE = ROOT / "config" / "routing_situations.json"


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def load_source_rows(path: Path = SOURCE) -> list:
    return json.loads(path.read_text(encoding="utf-8"))["decision_table"]["observable_situations"]


def build(source_rows: list, overlay: dict) -> str:
    """Render the catalogue text (one row per line, matching the committed layout)."""
    src_ids = {r["id"] for r in source_rows}
    dropped, rows = set(overlay["dropped"]), overlay["rows"]
    problems = [f"overlay row {i} not in source" for i in rows if i not in src_ids]
    problems += [f"dropped id {i} not in source" for i in dropped if i not in src_ids]
    problems += [f"id {i} is both dropped and in overlay rows" for i in dropped & rows.keys()]
    problems += [f"source row {i} has no overlay row and is not dropped"
                 for i in sorted(src_ids - dropped - rows.keys())]
    if problems:
        raise ValueError("; ".join(problems))
    out = []
    for s in source_rows:
        if s["id"] in dropped:
            continue
        row = {"id": s["id"], "q": s["situation"], "f": s["family"]}
        row.update(rows[s["id"]])  # overlay may override q/f in place; adds the rest in order
        out.append(_dumps(row))
    fams = ",\n".join(f"{_dumps(k)}: {_dumps(v)}" for k, v in overlay["families"].items())
    return (
        '{\n"families": {\n' + fams + "\n},\n"
        f'"policy": {_dumps(overlay["policy"])},\n'
        f'"escalation": {_dumps(overlay["escalation"])},\n'
        '"situations": [\n' + ",\n".join(out) + "\n]\n}\n"
    )


def init_overlay(source_rows: list, catalogue: dict) -> dict:
    src = {r["id"]: r for r in source_rows}
    cat_ids = {r["id"] for r in catalogue["situations"]}
    rows = {}
    for r in catalogue["situations"]:
        ov = {k: v for k, v in r.items() if k != "id"}
        if ov.get("q") == src[r["id"]]["situation"]:
            del ov["q"]
        if ov.get("f") == src[r["id"]]["family"]:
            del ov["f"]
        rows[r["id"]] = ov
    return {
        "families": catalogue["families"],
        "policy": catalogue["policy"],
        "escalation": catalogue["escalation"],
        "dropped": [r["id"] for r in source_rows if r["id"] not in cat_ids],
        "rows": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--init-overlay", action="store_true")
    args = ap.parse_args(argv)
    source_rows = load_source_rows()
    if args.init_overlay:
        if OVERLAY.exists():
            print(f"refusing to overwrite {OVERLAY}", file=sys.stderr)
            return 1
        overlay = init_overlay(source_rows, json.loads(CATALOGUE.read_text(encoding="utf-8")))
        OVERLAY.write_text(json.dumps(overlay, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {OVERLAY} ({len(overlay['rows'])} rows, {len(overlay['dropped'])} dropped)")
        return 0
    text = build(source_rows, json.loads(OVERLAY.read_text(encoding="utf-8")))
    if args.check:
        if CATALOGUE.read_text(encoding="utf-8") != text:
            print("STALE: config/routing_situations.json differs from build output", file=sys.stderr)
            return 1
        print("OK: catalogue matches build output")
        return 0
    CATALOGUE.write_text(text, encoding="utf-8")
    print(f"wrote {CATALOGUE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
