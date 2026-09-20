# Graph Database

A small, from-scratch, in-memory **labeled property graph database** with
its own Cypher-inspired query language -- pure Python standard library, no
third-party dependencies. Think "a tiny Neo4j" rather than a relational or
key-value store: data is modeled as nodes and directed, typed relationships,
each carrying arbitrary properties, and queried with pattern-matching /
graph-traversal statements instead of SQL joins or key lookups.

## Data model

- **Node**: `id` (int, auto-assigned), `labels` (a *set* of strings, e.g.
  `{"Person", "Employee"}`), `properties` (a dict, e.g.
  `{"name": "Alice", "age": 30}`).
- **Edge**: `id` (int, auto-assigned), `from_id`, `to_id` (node ids -- edges
  are directed), `rel_type` (a single string, e.g. `"FRIENDS_WITH"`),
  `properties` (a dict, e.g. `{"since": 2020}`).

This mirrors Neo4j's property graph model: labels categorize nodes (a node
can have zero, one, or many), relationship types categorize edges (exactly
one per edge), and both nodes and edges can carry arbitrary key/value data.

## Indexing strategy

Two indexes are maintained **incrementally** on every `create_node`,
`update_node_properties`, and `delete_node` call (never rebuilt from
scratch at query time):

1. **Label index** -- `label -> set(node_id)`. `find_by_label(label)` reads
   straight from this dict, so it costs O(matching nodes), not O(all nodes).
2. **Property-value index** -- `label -> property_name -> value ->
   set(node_id)`. `find_by_label_property(label, prop, value)` is an O(1)
   dict-of-dicts walk down to the matching set, again independent of total
   graph size.

Adjacency (`node_id -> list(edge_id)`, kept separately for outgoing and
incoming edges) is maintained the same way, so traversal and shortest-path
queries only ever touch edges incident to nodes on the current BFS frontier
-- never the full edge set.

**This is exercised, not just present.** `GraphDB` tracks a
`full_scan_count` counter that is only ever incremented by `all_nodes()` (a
deliberate full scan, used when a query has no label to filter on). The
test suite (`tests/test_graphdb.py`) creates dozens of nodes, calls
`find_by_label` / `find_by_label_property`, and asserts `full_scan_count`
did **not** move -- then calls `all_nodes()` directly and asserts that it
*does* move, proving the counter is meaningful and that the indexed lookups
really do avoid the scan.

## Query DSL

A small, hand-written tokenizer + recursive-descent parser (see
`graphdb/query.py`) implements a Cypher-inspired language. Keywords are
case-insensitive; strings are double-quoted; `#` starts a comment that runs
to end of line.

### Grammar (informal EBNF)

```
statement      := create_node | create_edge | match_query
                 | delete_node | delete_edge | set_prop

create_node    := "CREATE" "NODE" "(" label_list [ prop_map ] ")"
label_list     := (":" IDENT)+
prop_map       := "{" [ prop_pair ("," prop_pair)* ] "}"
prop_pair      := IDENT ":" value
value          := STRING | NUMBER | "true" | "false" | "null"

create_edge    := "CREATE" "EDGE" "(" NUMBER ")"
                   "-" "[" ":" IDENT [ prop_map ] "]" "->"
                   "(" NUMBER ")"

match_query    := match_simple | match_traverse | match_shortest

match_simple   := "MATCH" "(" IDENT [ ":" IDENT ] ")"
                   [ "WHERE" IDENT "." IDENT "=" value ]
                   "RETURN" IDENT

match_traverse := "MATCH" "(" IDENT [ ":" IDENT ] ")"
                   "-" "[" ":" IDENT [ hop_range ] "]" "->"
                   "(" IDENT [ ":" IDENT ] ")"
                   "RETURN" IDENT
hop_range      := "*" NUMBER [ ".." NUMBER ]

match_shortest := "MATCH" "SHORTESTPATH" "(" NUMBER ")" "TO" "(" NUMBER ")"
                   [ "VIA" IDENT ] "RETURN" "PATH"

delete_node    := "DELETE" "NODE" NUMBER
delete_edge    := "DELETE" "EDGE" NUMBER

set_prop       := "SET" "NODE" NUMBER "." IDENT "=" value
```

### Examples

```
CREATE NODE (:Person {name: "Alice", age: 30})
CREATE NODE (:Person {name: "Bob", age: 25})
CREATE NODE (:Company {name: "Acme"})

CREATE EDGE (1)-[:FRIENDS_WITH]->(2)
CREATE EDGE (1)-[:WORKS_AT {since: 2020}]->(3)

# Filter by label
MATCH (n:Person) RETURN n

# Filter by label + property (uses the property-value index)
MATCH (n:Person) WHERE n.name = "Bob" RETURN n

# 1-hop traversal by relationship type
MATCH (n:Person)-[:FRIENDS_WITH]->(m) RETURN m

# Multi-hop / variable-length traversal ("reachable within 1 to 3 hops")
MATCH (n:Person)-[:FRIENDS_WITH*1..3]->(m) RETURN m

# Shortest path between two nodes (optionally restricted to one rel type)
MATCH SHORTESTPATH (1) TO (2) VIA FRIENDS_WITH RETURN PATH

# Mutate
SET NODE 2.age = 26
DELETE NODE 3
DELETE EDGE 1
```

Malformed statements raise a `QueryError` with a message naming what was
expected and where, e.g.:

```
>>> MATCH (n:Person RETURN n
Query error: Expected ')' but found KEYWORD 'return' (position 12)
```

### Shortest path: BFS, not Dijkstra

`shortest_path` uses breadth-first search rather than Dijkstra. Edges in
this model are unweighted by default, and BFS already finds the true
minimum-hop path in O(V + E) for unweighted graphs, so a priority-queue
based algorithm would add complexity for no benefit here. Edges do carry a
free-form `properties` dict, so if weighted shortest paths were ever
needed, a `weight` property could be added and BFS swapped for Dijkstra
without changing the public API.

## Running it

### Interactive REPL

```
python main.py
```

```
Graph Database -- interactive REPL
Type a query statement and press Enter. Type HELP for a syntax summary,
or EXIT / QUIT to leave.
graphdb> CREATE NODE (:Person {name: "Alice", age: 30})
Node #1: (#1:Person {'name': 'Alice', 'age': 30})
graphdb> MATCH (n:Person) RETURN n
  (#1:Person {'name': 'Alice', 'age': 30})
graphdb> exit
```

### Script mode

Scripts are plain text files with one or more DSL statements (this project
uses the `.gqs` extension -- "Graph Query Script" -- by convention; any
extension works since it's just statements, `;`-or-newline separated, `#`
comments allowed):

```
python main.py examples/demo.gqs
```

Sample output (truncated):

```
> CREATE NODE (:Person {name: "Alice", age: 30})
Node #1: (#1:Person {'name': 'Alice', 'age': 30})
...
> MATCH SHORTESTPATH (1) TO (3) VIA FRIENDS_WITH RETURN PATH
  1
  2
  3
```

A malformed statement inside a script is reported and execution continues
with the remaining statements (it does not abort the whole script), and
the process exits with a non-zero status if any statement failed --
convenient for CI.

### As a library

```python
from graphdb import GraphDB, Parser

db = GraphDB()
alice = db.create_node(["Person"], {"name": "Alice"})
bob = db.create_node(["Person"], {"name": "Bob"})
db.create_edge(alice.id, bob.id, "FRIENDS_WITH")

db.find_by_label("Person")                      # index-backed
db.find_by_label_property("Person", "name", "Bob")
db.traverse(alice.id, "FRIENDS_WITH", 1, 1)      # 1-hop
db.shortest_path(alice.id, bob.id)               # BFS

# or drive it with the query DSL directly:
Parser(db).execute('MATCH (n:Person) RETURN n')
```

## Tests

```
python -m unittest discover -s tests -v
```

30 tests cover node/edge CRUD round-trips, label- and property-index
correctness *and actual use* (not full scans), 1-hop and multi-hop
traversal on graphs with known answers, shortest-path correctness on a
crafted graph with two routes of different lengths, cascading node
deletion, graceful handling of nonexistent labels/nodes, parser rejection
of malformed queries with clear errors, and end-to-end script-mode
execution (including a script that keeps going after a bad statement).

CI (`.github/workflows/tests.yml`) runs the suite on every push/PR against
Python 3.11 and 3.12.

## Project layout

```
graphdb/
  model.py   -- Node, Edge, GraphDB (storage, indexes, traversal, BFS shortest path)
  query.py   -- tokenizer, recursive-descent parser, statement executor
  cli.py     -- REPL + script-mode runner
main.py      -- entry point
examples/demo.gqs -- worked example script
tests/test_graphdb.py -- unit tests
```
