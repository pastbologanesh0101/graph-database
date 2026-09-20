"""
graphdb.model
=============

A minimal in-memory, labeled property graph database, modeled after the
property-graph data model popularized by Neo4j:

  * A Node has a unique integer id, a *set* of string labels
    (e.g. {"Person", "Employee"}), and a dict of properties.
  * An Edge has a unique integer id, is directed from one node to another,
    carries a single *relationship type* (a string, e.g. "FRIENDS_WITH"),
    and a dict of properties.

Everything is pure Python standard library -- no third-party dependencies.

Indexing strategy
------------------
Two indexes are maintained incrementally (created/updated/removed on every
mutation, not rebuilt from scratch on every query):

  1. Label index: ``label -> set(node_id)``
     Lets ``find_by_label`` answer in O(matching nodes) instead of
     scanning every node in the graph.

  2. Property-value index: ``label -> property_name -> value -> set(node_id)``
     Lets ``find_by_label_property`` answer an equality lookup in
     O(matching nodes) as well, instead of scanning + filtering every node
     that has the label.

Adjacency (``_out_edges`` / ``_in_edges``, both ``node_id -> list(edge_id)``)
is also maintained so that 1-hop/multi-hop traversal and shortest-path never
need to scan the full edge set either -- they only ever look at edges
incident to the nodes on the current BFS frontier.

Instrumentation
----------------
``GraphDB`` tracks a ``full_scan_count`` counter. It is ONLY incremented by
``all_nodes()``, a method that deliberately walks every node (used, e.g., by
``MATCH (n) RETURN n`` with no label filter, where a scan is unavoidable).
``find_by_label`` and ``find_by_label_property`` never touch that path --
they go straight through the indexes. Tests assert this counter does not
move when an indexed lookup runs, which is a real (not cosmetic) check that
the indexes are actually being used.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set


class GraphError(Exception):
    """Raised for invalid operations against the graph (e.g. bad node id)."""


@dataclass
class Node:
    id: int
    labels: Set[str] = field(default_factory=set)
    properties: Dict[str, object] = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        labels = "".join(f":{l}" for l in sorted(self.labels))
        return f"(#{self.id}{labels} {self.properties})"


@dataclass
class Edge:
    id: int
    from_id: int
    to_id: int
    rel_type: str
    properties: Dict[str, object] = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"[#{self.id} ({self.from_id})-[:{self.rel_type}]->({self.to_id}) {self.properties}]"


class GraphDB:
    """An in-memory labeled property graph with label/property indexing."""

    def __init__(self) -> None:
        self._nodes: Dict[int, Node] = {}
        self._edges: Dict[int, Edge] = {}
        self._next_node_id = 1
        self._next_edge_id = 1

        # --- indexes ---
        # label -> set(node_id)
        self._label_index: Dict[str, Set[int]] = {}
        # label -> prop_name -> value -> set(node_id)
        self._property_index: Dict[str, Dict[str, Dict[object, Set[int]]]] = {}

        # --- adjacency (also index-like: avoids scanning all edges) ---
        self._out_edges: Dict[int, List[int]] = {}
        self._in_edges: Dict[int, List[int]] = {}

        # --- instrumentation ---
        self.full_scan_count = 0
        self.label_index_lookups = 0
        self.property_index_lookups = 0

    # ------------------------------------------------------------------
    # Node CRUD
    # ------------------------------------------------------------------
    def create_node(self, labels: Iterable[str] = (), properties: Optional[dict] = None) -> Node:
        node_id = self._next_node_id
        self._next_node_id += 1
        node = Node(id=node_id, labels=set(labels), properties=dict(properties or {}))
        self._nodes[node_id] = node
        self._out_edges[node_id] = []
        self._in_edges[node_id] = []
        for label in node.labels:
            self._label_index.setdefault(label, set()).add(node_id)
            for prop, value in node.properties.items():
                self._index_property(label, prop, value, node_id)
        return node

    def get_node(self, node_id: int) -> Optional[Node]:
        return self._nodes.get(node_id)

    def update_node_properties(self, node_id: int, updates: dict) -> Node:
        node = self._nodes.get(node_id)
        if node is None:
            raise GraphError(f"No such node: {node_id}")
        for prop, value in updates.items():
            old_value = node.properties.get(prop)
            for label in node.labels:
                if prop in node.properties:
                    self._unindex_property(label, prop, old_value, node_id)
                self._index_property(label, prop, value, node_id)
            node.properties[prop] = value
        return node

    def delete_node(self, node_id: int) -> None:
        """Delete a node and cascade-delete every edge touching it."""
        node = self._nodes.get(node_id)
        if node is None:
            raise GraphError(f"No such node: {node_id}")

        # Cascade: remove every edge incident to this node (both directions).
        incident_edges = list(self._out_edges.get(node_id, [])) + list(self._in_edges.get(node_id, []))
        for edge_id in set(incident_edges):
            if edge_id in self._edges:
                self.delete_edge(edge_id)

        # Remove from indexes.
        for label in node.labels:
            self._label_index.get(label, set()).discard(node_id)
            for prop, value in node.properties.items():
                self._unindex_property(label, prop, value, node_id)

        del self._nodes[node_id]
        self._out_edges.pop(node_id, None)
        self._in_edges.pop(node_id, None)

    def all_nodes(self) -> List[Node]:
        """Return every node. Deliberately a full scan -- see module docstring."""
        self.full_scan_count += 1
        return list(self._nodes.values())

    # ------------------------------------------------------------------
    # Edge CRUD
    # ------------------------------------------------------------------
    def create_edge(self, from_id: int, to_id: int, rel_type: str, properties: Optional[dict] = None) -> Edge:
        if from_id not in self._nodes:
            raise GraphError(f"No such node: {from_id}")
        if to_id not in self._nodes:
            raise GraphError(f"No such node: {to_id}")
        edge_id = self._next_edge_id
        self._next_edge_id += 1
        edge = Edge(id=edge_id, from_id=from_id, to_id=to_id, rel_type=rel_type, properties=dict(properties or {}))
        self._edges[edge_id] = edge
        self._out_edges.setdefault(from_id, []).append(edge_id)
        self._in_edges.setdefault(to_id, []).append(edge_id)
        return edge

    def get_edge(self, edge_id: int) -> Optional[Edge]:
        return self._edges.get(edge_id)

    def delete_edge(self, edge_id: int) -> None:
        edge = self._edges.get(edge_id)
        if edge is None:
            raise GraphError(f"No such edge: {edge_id}")
        if edge_id in self._out_edges.get(edge.from_id, []):
            self._out_edges[edge.from_id].remove(edge_id)
        if edge_id in self._in_edges.get(edge.to_id, []):
            self._in_edges[edge.to_id].remove(edge_id)
        del self._edges[edge_id]

    def all_edges(self) -> List[Edge]:
        return list(self._edges.values())

    # ------------------------------------------------------------------
    # Index maintenance helpers
    # ------------------------------------------------------------------
    def _index_property(self, label: str, prop: str, value: object, node_id: int) -> None:
        by_label = self._property_index.setdefault(label, {})
        by_prop = by_label.setdefault(prop, {})
        by_prop.setdefault(value, set()).add(node_id)

    def _unindex_property(self, label: str, prop: str, value: object, node_id: int) -> None:
        try:
            self._property_index[label][prop][value].discard(node_id)
        except KeyError:
            pass

    # ------------------------------------------------------------------
    # Indexed lookups
    # ------------------------------------------------------------------
    def find_by_label(self, label: str) -> List[Node]:
        """O(matches) lookup via the label index -- never scans all nodes."""
        self.label_index_lookups += 1
        node_ids = self._label_index.get(label, set())
        return [self._nodes[nid] for nid in node_ids if nid in self._nodes]

    def find_by_label_property(self, label: str, prop: str, value: object) -> List[Node]:
        """O(matches) equality lookup via the property-value index."""
        self.property_index_lookups += 1
        node_ids = self._property_index.get(label, {}).get(prop, {}).get(value, set())
        return [self._nodes[nid] for nid in node_ids if nid in self._nodes]

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------
    def traverse(self, start_id: int, rel_type: str, min_hops: int = 1, max_hops: int = 1) -> Set[int]:
        """
        Breadth-first traversal from ``start_id`` following only edges whose
        relationship type equals ``rel_type``. Returns the set of node ids
        reachable at a hop-count between ``min_hops`` and ``max_hops``
        (inclusive), i.e. a "variable length path" query like Cypher's
        ``-[:REL*min..max]->``.
        """
        if start_id not in self._nodes:
            return set()

        reachable_by_hop: Dict[int, Set[int]] = {0: {start_id}}
        visited = {start_id}
        frontier = {start_id}
        for hop in range(1, max_hops + 1):
            next_frontier: Set[int] = set()
            for nid in frontier:
                for edge_id in self._out_edges.get(nid, []):
                    edge = self._edges[edge_id]
                    if edge.rel_type == rel_type and edge.to_id not in visited:
                        next_frontier.add(edge.to_id)
            visited |= next_frontier
            reachable_by_hop[hop] = next_frontier
            frontier = next_frontier
            if not frontier:
                break

        result: Set[int] = set()
        for hop, nodes_at_hop in reachable_by_hop.items():
            if min_hops <= hop <= max_hops and hop > 0:
                result |= nodes_at_hop
        return result

    # ------------------------------------------------------------------
    # Shortest path (BFS -- graph is treated as unweighted)
    # ------------------------------------------------------------------
    def shortest_path(self, from_id: int, to_id: int, rel_type: Optional[str] = None) -> Optional[List[int]]:
        """
        Find the shortest path (fewest hops) between two nodes using
        breadth-first search. BFS is used rather than Dijkstra because edges
        in this model are unweighted by default -- BFS already guarantees
        the minimum-hop path in O(V + E) for unweighted graphs. (If weighted
        shortest paths were needed, edges carry a free-form ``properties``
        dict, so a ``weight`` property could be added and BFS swapped for
        Dijkstra without changing the public API.)

        Returns the list of node ids from ``from_id`` to ``to_id`` inclusive,
        or ``None`` if no path exists.
        """
        if from_id not in self._nodes or to_id not in self._nodes:
            return None
        if from_id == to_id:
            return [from_id]

        visited = {from_id}
        queue = deque([[from_id]])
        while queue:
            path = queue.popleft()
            last = path[-1]
            for edge_id in self._out_edges.get(last, []):
                edge = self._edges[edge_id]
                if rel_type is not None and edge.rel_type != rel_type:
                    continue
                nxt = edge.to_id
                if nxt == to_id:
                    return path + [nxt]
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(path + [nxt])
        return None
