---
name: graph-query
description: Look up a symbol or file in the file graph to get exact line numbers before a Read — avoids full-file reads. Use before calling Read with a known symbol name or function to get precise line offsets.
---

# Graph Query

Query the indexed file graph to find exact line numbers for a symbol or list all symbols in a file.

## Usage

```bash
python3 skills/graph-query/run.py --symbol NAME
python3 skills/graph-query/run.py --file PATH
```

## Output Format

For `--symbol NAME`:
```json
{
  "file": "/absolute/path/to/file.py",
  "line_start": 42,
  "line_end": 87,
  "symbol": "function_name"
}
```

For `--file PATH`:
```json
[
  {"symbol": "function_1", "line_start": 10, "line_end": 35},
  {"symbol": "function_2", "line_start": 37, "line_end": 89}
]
```

## Guidelines

- The graph index is stored at `DATA_DIR/file-graphs/index.json` (DATA_DIR resolved from `scripts/_common.py`).
- If the graph is missing or stale (file mtime newer than graph timestamp), `run.py` automatically rebuilds it via `scripts/build_file_graph.py`.
- Use `line_start` as the Read tool's `offset` parameter.
- Use `(line_end - line_start)` as the Read tool's `limit` parameter.
- Never perform full-file reads on files that exist in the graph index — use targeted reads instead.

## Example Workflow

Before reading a function, query first:

```bash
# Find the exact lines for a specific function
python3 skills/graph-query/run.py --symbol my_function

# Output tells you to read that file with offset and limit
# Then call: Read(file_path, offset=line_start, limit=line_end-line_start)
```
