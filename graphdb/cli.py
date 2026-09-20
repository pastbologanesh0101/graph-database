"""
graphdb.cli
===========

Two ways to drive the graph database from the command line:

  * Interactive REPL:      python main.py
  * Script mode:            python main.py path/to/script.gqs

Script files use the ``.gqs`` extension ("Graph Query Script") by
convention -- any plain-text extension works since the file is just
statements in this project's query DSL, one or more per line, ``#``
comments allowed. See examples/demo.gqs for a full worked example.
"""

from __future__ import annotations

import sys
from typing import List

from .model import Edge, GraphDB, Node
from .query import Parser, QueryError, run_script

BANNER = """\
Graph Database -- interactive REPL
Type a query statement and press Enter. Type HELP for a syntax summary,
or EXIT / QUIT to leave.\
"""

HELP_TEXT = """\
Statements:
  CREATE NODE (:Label {key: "value"})
  CREATE EDGE (<id>)-[:REL_TYPE {key: "value"}]->(<id>)
  MATCH (n:Label) RETURN n
  MATCH (n:Label) WHERE n.prop = "value" RETURN n
  MATCH (n:Label)-[:REL_TYPE]->(m) RETURN m
  MATCH (n:Label)-[:REL_TYPE*1..3]->(m) RETURN m
  MATCH SHORTESTPATH (<id>) TO (<id>) RETURN PATH
  MATCH SHORTESTPATH (<id>) TO (<id>) VIA REL_TYPE RETURN PATH
  DELETE NODE <id>
  DELETE EDGE <id>
  SET NODE <id>.prop = "value"
See README.md for the full grammar and worked examples.\
"""


def format_result(result) -> str:
    if result is None:
        return "OK"
    if isinstance(result, Node):
        return f"Node #{result.id}: {result!r}"
    if isinstance(result, Edge):
        return f"Edge #{result.id}: {result!r}"
    if isinstance(result, list):
        if not result:
            return "(no results)"
        return "\n".join(f"  {item!r}" for item in result)
    if isinstance(result, str):
        return result
    # shortest_path returns a plain list of ints, or None (handled above)
    return repr(result)


def run_repl() -> None:
    db = GraphDB()
    parser = Parser(db)
    print(BANNER)
    while True:
        try:
            line = input("graphdb> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            break
        if line.lower() == "help":
            print(HELP_TEXT)
            continue
        try:
            result = parser.execute(line)
            print(format_result(result))
        except QueryError as exc:
            print(f"Query error: {exc}")


def run_script_file(path: str) -> int:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    db = GraphDB()
    results = run_script(db, text)
    exit_code = 0
    for stmt, result in results:
        print(f"> {stmt}")
        if isinstance(result, QueryError):
            print(f"Query error: {result}")
            exit_code = 1
        else:
            print(format_result(result))
    return exit_code


def main(argv: List[str] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        return run_script_file(argv[0])
    run_repl()
    return 0
