"""
Unit tests for the in-memory property graph database and its query DSL.

Run with:  python -m pytest tests/  (or  python -m unittest discover)
"""

import unittest

from graphdb.model import GraphDB, GraphError
from graphdb.query import Parser, QueryError, run_script


class NodeEdgeCrudTests(unittest.TestCase):
    def test_create_node_roundtrip(self):
        db = GraphDB()
        node = db.create_node(["Person"], {"name": "Alice", "age": 30})
        fetched = db.get_node(node.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.labels, {"Person"})
        self.assertEqual(fetched.properties, {"name": "Alice", "age": 30})

    def test_create_edge_roundtrip(self):
        db = GraphDB()
        a = db.create_node(["Person"], {"name": "Alice"})
        b = db.create_node(["Person"], {"name": "Bob"})
        edge = db.create_edge(a.id, b.id, "FRIENDS_WITH", {"since": 2020})
        fetched = db.get_edge(edge.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.from_id, a.id)
        self.assertEqual(fetched.to_id, b.id)
        self.assertEqual(fetched.rel_type, "FRIENDS_WITH")
        self.assertEqual(fetched.properties, {"since": 2020})

    def test_create_edge_rejects_missing_node(self):
        db = GraphDB()
        a = db.create_node(["Person"], {})
        with self.assertRaises(GraphError):
            db.create_edge(a.id, 9999, "FRIENDS_WITH", {})

    def test_update_node_properties(self):
        db = GraphDB()
        node = db.create_node(["Person"], {"age": 30})
        db.update_node_properties(node.id, {"age": 31, "city": "NYC"})
        fetched = db.get_node(node.id)
        self.assertEqual(fetched.properties["age"], 31)
        self.assertEqual(fetched.properties["city"], "NYC")

    def test_delete_node_cascades_edges(self):
        db = GraphDB()
        a = db.create_node(["Person"], {"name": "A"})
        b = db.create_node(["Person"], {"name": "B"})
        c = db.create_node(["Person"], {"name": "C"})
        e1 = db.create_edge(a.id, b.id, "FRIENDS_WITH", {})
        e2 = db.create_edge(c.id, a.id, "FRIENDS_WITH", {})
        db.delete_node(a.id)
        self.assertIsNone(db.get_node(a.id))
        self.assertIsNone(db.get_edge(e1.id))
        self.assertIsNone(db.get_edge(e2.id))
        # unrelated nodes survive
        self.assertIsNotNone(db.get_node(b.id))
        self.assertIsNotNone(db.get_node(c.id))

    def test_delete_edge(self):
        db = GraphDB()
        a = db.create_node(["Person"], {})
        b = db.create_node(["Person"], {})
        edge = db.create_edge(a.id, b.id, "FRIENDS_WITH", {})
        db.delete_edge(edge.id)
        self.assertIsNone(db.get_edge(edge.id))
        # nodes remain
        self.assertIsNotNone(db.get_node(a.id))
        self.assertIsNotNone(db.get_node(b.id))

    def test_delete_nonexistent_node_raises(self):
        db = GraphDB()
        with self.assertRaises(GraphError):
            db.delete_node(9999)


class IndexTests(unittest.TestCase):
    def test_label_index_correct_and_actually_used(self):
        db = GraphDB()
        people = [db.create_node(["Person"], {"name": f"P{i}"}) for i in range(50)]
        db.create_node(["Company"], {"name": "Acme"})

        before = db.full_scan_count
        results = db.find_by_label("Person")
        after = db.full_scan_count

        self.assertEqual({n.id for n in results}, {p.id for p in people})
        # The label lookup must not have triggered a full node scan.
        self.assertEqual(before, after, "find_by_label should not perform a full scan")

        # Sanity check: a genuine full scan DOES move the counter, proving
        # the counter is meaningful and find_by_label's index path is what
        # avoids it (not that the counter is simply never incremented).
        db.all_nodes()
        self.assertEqual(db.full_scan_count, after + 1)

    def test_label_index_data_structure_is_correct(self):
        db = GraphDB()
        a = db.create_node(["Person"], {})
        b = db.create_node(["Person"], {})
        db.create_node(["Company"], {})
        self.assertEqual(db._label_index["Person"], {a.id, b.id})

    def test_property_value_index_correct_and_used(self):
        db = GraphDB()
        for i in range(30):
            db.create_node(["Person"], {"name": f"P{i}"})
        target = db.create_node(["Person"], {"name": "Needle"})

        before = db.full_scan_count
        results = db.find_by_label_property("Person", "name", "Needle")
        after = db.full_scan_count

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, target.id)
        self.assertEqual(before, after, "property lookup should not perform a full scan")

    def test_property_index_updates_on_property_change(self):
        db = GraphDB()
        node = db.create_node(["Person"], {"name": "Alice"})
        db.update_node_properties(node.id, {"name": "Alicia"})
        self.assertEqual(db.find_by_label_property("Person", "name", "Alice"), [])
        results = db.find_by_label_property("Person", "name", "Alicia")
        self.assertEqual([n.id for n in results], [node.id])

    def test_property_index_cleared_on_delete(self):
        db = GraphDB()
        node = db.create_node(["Person"], {"name": "Alice"})
        db.delete_node(node.id)
        self.assertEqual(db.find_by_label_property("Person", "name", "Alice"), [])


class TraversalTests(unittest.TestCase):
    def _chain_graph(self):
        # 1 -[NEXT]-> 2 -[NEXT]-> 3 -[NEXT]-> 4
        db = GraphDB()
        nodes = [db.create_node(["Step"], {"n": i}) for i in range(4)]
        for i in range(3):
            db.create_edge(nodes[i].id, nodes[i + 1].id, "NEXT", {})
        return db, nodes

    def test_one_hop_traversal_by_rel_type(self):
        db, nodes = self._chain_graph()
        reached = db.traverse(nodes[0].id, "NEXT", 1, 1)
        self.assertEqual(reached, {nodes[1].id})

    def test_one_hop_traversal_ignores_other_rel_types(self):
        db = GraphDB()
        a = db.create_node(["Person"], {})
        b = db.create_node(["Person"], {})
        db.create_edge(a.id, b.id, "BLOCKS", {})
        reached = db.traverse(a.id, "FRIENDS_WITH", 1, 1)
        self.assertEqual(reached, set())

    def test_multi_hop_traversal_known_graph(self):
        db, nodes = self._chain_graph()
        reached = db.traverse(nodes[0].id, "NEXT", 1, 3)
        self.assertEqual(reached, {nodes[1].id, nodes[2].id, nodes[3].id})

    def test_multi_hop_traversal_min_hops_excludes_closer_nodes(self):
        db, nodes = self._chain_graph()
        reached = db.traverse(nodes[0].id, "NEXT", 2, 2)
        self.assertEqual(reached, {nodes[2].id})


class ShortestPathTests(unittest.TestCase):
    def _diamond_graph(self):
        # Two paths of different lengths from A to D:
        #   A -> B -> D            (2 hops, shortest)
        #   A -> E -> F -> G -> D  (4 hops)
        db = GraphDB()
        a = db.create_node(["N"], {"name": "A"})
        b = db.create_node(["N"], {"name": "B"})
        d = db.create_node(["N"], {"name": "D"})
        e = db.create_node(["N"], {"name": "E"})
        f = db.create_node(["N"], {"name": "F"})
        g = db.create_node(["N"], {"name": "G"})
        db.create_edge(a.id, b.id, "ROAD", {})
        db.create_edge(b.id, d.id, "ROAD", {})
        db.create_edge(a.id, e.id, "ROAD", {})
        db.create_edge(e.id, f.id, "ROAD", {})
        db.create_edge(f.id, g.id, "ROAD", {})
        db.create_edge(g.id, d.id, "ROAD", {})
        return db, a, b, d, e, f, g

    def test_shortest_path_known_graph(self):
        db, a, b, d, e, f, g = self._diamond_graph()
        path = db.shortest_path(a.id, d.id, "ROAD")
        self.assertIsNotNone(path)
        # 2 hops == 3 nodes, and it must be the short route through B.
        self.assertEqual(len(path), 3)
        self.assertEqual(path[0], a.id)
        self.assertEqual(path[-1], d.id)
        self.assertEqual(path[1], b.id)

    def test_shortest_path_no_route_returns_none(self):
        db = GraphDB()
        a = db.create_node(["N"], {})
        b = db.create_node(["N"], {})
        # no edge between them at all
        self.assertIsNone(db.shortest_path(a.id, b.id))

    def test_shortest_path_nonexistent_endpoint_returns_none(self):
        db = GraphDB()
        a = db.create_node(["N"], {})
        self.assertIsNone(db.shortest_path(a.id, 9999))
        self.assertIsNone(db.shortest_path(9999, a.id))

    def test_shortest_path_same_node(self):
        db = GraphDB()
        a = db.create_node(["N"], {})
        self.assertEqual(db.shortest_path(a.id, a.id), [a.id])


class GracefulEmptyResultTests(unittest.TestCase):
    def test_query_nonexistent_label_returns_empty_not_crash(self):
        db = GraphDB()
        db.create_node(["Person"], {})
        self.assertEqual(db.find_by_label("Alien"), [])

    def test_query_nonexistent_node_id_returns_none_not_crash(self):
        db = GraphDB()
        self.assertIsNone(db.get_node(12345))

    def test_dsl_match_nonexistent_label_returns_empty(self):
        db = GraphDB()
        db.create_node(["Person"], {"name": "Alice"})
        parser = Parser(db)
        result = parser.execute('MATCH (n:Alien) RETURN n')
        self.assertEqual(result, [])


class ParserTests(unittest.TestCase):
    def test_parser_accepts_create_node(self):
        db = GraphDB()
        parser = Parser(db)
        node = parser.execute('CREATE NODE (:Person {name: "Alice", age: 30})')
        self.assertEqual(node.labels, {"Person"})
        self.assertEqual(node.properties["name"], "Alice")

    def test_parser_rejects_missing_label(self):
        db = GraphDB()
        parser = Parser(db)
        with self.assertRaises(QueryError):
            parser.execute("CREATE NODE {name: \"Alice\"}")

    def test_parser_rejects_unclosed_paren(self):
        db = GraphDB()
        parser = Parser(db)
        db.create_node(["Person"], {"name": "Alice"})
        with self.assertRaises(QueryError):
            parser.execute("MATCH (n:Person RETURN n")

    def test_parser_rejects_unknown_statement_keyword(self):
        db = GraphDB()
        parser = Parser(db)
        with self.assertRaises(QueryError):
            parser.execute("FROBNICATE NODE (:Person)")

    def test_parser_error_message_is_descriptive(self):
        db = GraphDB()
        parser = Parser(db)
        try:
            parser.execute("CREATE NODE (:Person} {name: \"Alice\"})")
        except QueryError as exc:
            self.assertTrue(str(exc))  # non-empty, human readable
        else:
            self.fail("Expected a QueryError for malformed CREATE NODE")


class ScriptModeTests(unittest.TestCase):
    def test_script_mode_multi_statement_end_to_end(self):
        db = GraphDB()
        script = """
        # sample script
        CREATE NODE (:Person {name: "Alice"})
        CREATE NODE (:Person {name: "Bob"})
        CREATE EDGE (1)-[:FRIENDS_WITH]->(2)
        MATCH (n:Person) RETURN n
        MATCH (n:Person)-[:FRIENDS_WITH]->(m) RETURN m
        """
        results = run_script(db, script)
        # 5 executable statements, in order
        self.assertEqual(len(results), 5)
        stmt, match_all = results[3]
        self.assertTrue(stmt.startswith("MATCH (n:Person) RETURN n"))
        self.assertEqual(len(match_all), 2)

        stmt, traverse_result = results[4]
        self.assertEqual([n.properties["name"] for n in traverse_result], ["Bob"])

    def test_script_mode_continues_after_error_and_reports_it(self):
        db = GraphDB()
        script = """
        CREATE NODE (:Person {name: "Alice"})
        THIS IS NOT VALID
        CREATE NODE (:Person {name: "Bob"})
        """
        results = run_script(db, script)
        self.assertEqual(len(results), 3)
        self.assertIsInstance(results[1][1], QueryError)
        # valid statements before and after the bad one still executed
        self.assertEqual(len(db.find_by_label("Person")), 2)


if __name__ == "__main__":
    unittest.main()
