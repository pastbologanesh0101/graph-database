"""
graphdb.query
=============

A small, Cypher-inspired query language for :class:`graphdb.model.GraphDB`,
implemented as a hand-written tokenizer + recursive-descent parser +
executor (no regex-based statement dispatch, no third-party parser
libraries).

Grammar (informal EBNF)
------------------------

::

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

Keywords are case-insensitive. Strings are double-quoted. Comments start
with ``#`` and run to end of line. Statements are terminated by a newline
or a semicolon, so a script file may put one statement per line or several
per line separated by ``;``.

See the top-level README for worked examples of every statement form.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .model import GraphDB, GraphError


class QueryError(Exception):
    """Raised for malformed queries -- always with a human-readable message."""


# ----------------------------------------------------------------------
# Tokenizer
# ----------------------------------------------------------------------

_KEYWORDS = {
    "create", "node", "edge", "match", "where", "return", "delete", "set",
    "shortestpath", "to", "via", "true", "false", "null", "path",
}

_SYMBOLS = ["->", "..", "(", ")", "[", "]", "{", "}", ":", ",", ".", "=", "*", "-"]


@dataclass
class Token:
    kind: str  # "KEYWORD" | "IDENT" | "NUMBER" | "STRING" | "SYMBOL" | "EOF"
    value: object
    pos: int


def tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                buf.append(text[j])
                j += 1
            if j >= n:
                raise QueryError(f"Unterminated string literal starting at position {i}")
            tokens.append(Token("STRING", "".join(buf), i))
            i = j + 1
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1 if c == "-" else i
            while j < n and text[j].isdigit():
                j += 1
            # Allow a single decimal point followed by a digit (a real
            # number like "3.14"), but NOT when it's the start of the ".."
            # range operator (e.g. "1..3") -- that must stay two tokens.
            if j < n and text[j] == "." and j + 1 < n and text[j + 1].isdigit():
                j += 1
                while j < n and text[j].isdigit():
                    j += 1
            raw = text[i:j]
            num: object = float(raw) if "." in raw else int(raw)
            tokens.append(Token("NUMBER", num, i))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            low = word.lower()
            if low in _KEYWORDS:
                tokens.append(Token("KEYWORD", low, i))
            else:
                tokens.append(Token("IDENT", word, i))
            i = j
            continue
        matched = False
        for sym in _SYMBOLS:
            if text.startswith(sym, i):
                tokens.append(Token("SYMBOL", sym, i))
                i += len(sym)
                matched = True
                break
        if matched:
            continue
        raise QueryError(f"Unexpected character {c!r} at position {i}")
    tokens.append(Token("EOF", None, n))
    return tokens


# ----------------------------------------------------------------------
# Parser + executor
# ----------------------------------------------------------------------

class Parser:
    """Recursive-descent parser that executes each statement as it parses.

    Keeping parse-and-execute in one pass is a deliberate simplification for
    this mini-DSL: statements are independent (no cross-statement
    expressions to build an AST for), so there is no benefit to a separate
    AST stage.
    """

    def __init__(self, db: GraphDB) -> None:
        self.db = db

    # -- token helpers --------------------------------------------------
    def _peek(self, tokens: List[Token], i: int) -> Token:
        return tokens[i]

    def _expect_keyword(self, tokens: List[Token], i: int, kw: str) -> int:
        tok = tokens[i]
        if tok.kind == "KEYWORD" and tok.value == kw:
            return i + 1
        raise QueryError(f"Expected keyword '{kw.upper()}' but found {self._describe(tok)} (position {tok.pos})")

    def _expect_symbol(self, tokens: List[Token], i: int, sym: str) -> int:
        tok = tokens[i]
        if tok.kind == "SYMBOL" and tok.value == sym:
            return i + 1
        raise QueryError(f"Expected '{sym}' but found {self._describe(tok)} (position {tok.pos})")

    def _expect_ident(self, tokens: List[Token], i: int) -> Tuple[str, int]:
        tok = tokens[i]
        if tok.kind == "IDENT":
            return tok.value, i + 1
        # Allow keywords to double as identifiers (e.g. a label named "Path").
        if tok.kind == "KEYWORD":
            return tok.value, i + 1
        raise QueryError(f"Expected an identifier but found {self._describe(tok)} (position {tok.pos})")

    def _expect_number(self, tokens: List[Token], i: int) -> Tuple[object, int]:
        tok = tokens[i]
        if tok.kind == "NUMBER":
            return tok.value, i + 1
        raise QueryError(f"Expected a number but found {self._describe(tok)} (position {tok.pos})")

    @staticmethod
    def _describe(tok: Token) -> str:
        if tok.kind == "EOF":
            return "end of statement"
        return f"{tok.kind} {tok.value!r}"

    # -- value / property-map parsing -----------------------------------
    def _parse_value(self, tokens: List[Token], i: int):
        tok = tokens[i]
        if tok.kind == "STRING":
            return tok.value, i + 1
        if tok.kind == "NUMBER":
            return tok.value, i + 1
        if tok.kind == "KEYWORD" and tok.value == "true":
            return True, i + 1
        if tok.kind == "KEYWORD" and tok.value == "false":
            return False, i + 1
        if tok.kind == "KEYWORD" and tok.value == "null":
            return None, i + 1
        raise QueryError(f"Expected a value (string/number/true/false/null) but found {self._describe(tok)} (position {tok.pos})")

    def _parse_prop_map(self, tokens: List[Token], i: int) -> Tuple[dict, int]:
        props: dict = {}
        i = self._expect_symbol(tokens, i, "{")
        if tokens[i].kind == "SYMBOL" and tokens[i].value == "}":
            return props, i + 1
        while True:
            key, i = self._expect_ident(tokens, i)
            i = self._expect_symbol(tokens, i, ":")
            value, i = self._parse_value(tokens, i)
            props[key] = value
            if tokens[i].kind == "SYMBOL" and tokens[i].value == ",":
                i += 1
                continue
            break
        i = self._expect_symbol(tokens, i, "}")
        return props, i

    def _maybe_prop_map(self, tokens: List[Token], i: int) -> Tuple[dict, int]:
        if tokens[i].kind == "SYMBOL" and tokens[i].value == "{":
            return self._parse_prop_map(tokens, i)
        return {}, i

    def _parse_label_list(self, tokens: List[Token], i: int) -> Tuple[List[str], int]:
        labels: List[str] = []
        while tokens[i].kind == "SYMBOL" and tokens[i].value == ":":
            i += 1
            name, i = self._expect_ident(tokens, i)
            labels.append(name)
        if not labels:
            raise QueryError(f"Expected at least one ':Label' but found {self._describe(tokens[i])} (position {tokens[i].pos})")
        return labels, i

    # -- top level --------------------------------------------------------
    def execute(self, statement: str):
        text = statement.strip()
        if not text:
            return None
        tokens = tokenize(text)
        if tokens[0].kind == "EOF":
            return None
        tok = tokens[0]
        if tok.kind != "KEYWORD":
            raise QueryError(f"Expected a statement keyword (CREATE/MATCH/DELETE/SET) but found {self._describe(tok)} (position {tok.pos})")

        if tok.value == "create":
            return self._parse_create(tokens)
        if tok.value == "match":
            return self._parse_match(tokens)
        if tok.value == "delete":
            return self._parse_delete(tokens)
        if tok.value == "set":
            return self._parse_set(tokens)
        raise QueryError(f"Unknown statement type '{tok.value.upper()}' (position {tok.pos})")

    # -- CREATE -----------------------------------------------------------
    def _parse_create(self, tokens: List[Token]):
        i = self._expect_keyword(tokens, 0, "create")
        tok = tokens[i]
        if tok.kind == "KEYWORD" and tok.value == "node":
            i += 1
            i = self._expect_symbol(tokens, i, "(")
            labels, i = self._parse_label_list(tokens, i)
            props, i = self._maybe_prop_map(tokens, i)
            i = self._expect_symbol(tokens, i, ")")
            self._expect_end(tokens, i)
            node = self.db.create_node(labels, props)
            return node
        if tok.kind == "KEYWORD" and tok.value == "edge":
            i += 1
            i = self._expect_symbol(tokens, i, "(")
            from_id, i = self._expect_number(tokens, i)
            i = self._expect_symbol(tokens, i, ")")
            i = self._expect_symbol(tokens, i, "-")
            i = self._expect_symbol(tokens, i, "[")
            i = self._expect_symbol(tokens, i, ":")
            rel_type, i = self._expect_ident(tokens, i)
            props, i = self._maybe_prop_map(tokens, i)
            i = self._expect_symbol(tokens, i, "]")
            i = self._expect_symbol(tokens, i, "->")
            i = self._expect_symbol(tokens, i, "(")
            to_id, i = self._expect_number(tokens, i)
            i = self._expect_symbol(tokens, i, ")")
            self._expect_end(tokens, i)
            try:
                edge = self.db.create_edge(int(from_id), int(to_id), rel_type, props)
            except GraphError as exc:
                raise QueryError(str(exc)) from exc
            return edge
        raise QueryError(f"Expected 'NODE' or 'EDGE' after CREATE but found {self._describe(tok)} (position {tok.pos})")

    # -- MATCH ------------------------------------------------------------
    def _parse_match(self, tokens: List[Token]):
        i = self._expect_keyword(tokens, 0, "match")
        if tokens[i].kind == "KEYWORD" and tokens[i].value == "shortestpath":
            return self._parse_match_shortest(tokens, i + 1)

        i = self._expect_symbol(tokens, i, "(")
        var1, i = self._expect_ident(tokens, i)
        label1: Optional[str] = None
        if tokens[i].kind == "SYMBOL" and tokens[i].value == ":":
            i += 1
            label1, i = self._expect_ident(tokens, i)
        i = self._expect_symbol(tokens, i, ")")

        if tokens[i].kind == "SYMBOL" and tokens[i].value == "-":
            return self._parse_match_traverse(tokens, i, var1, label1)

        # simple match, optional WHERE
        where_prop = None
        where_val = None
        if tokens[i].kind == "KEYWORD" and tokens[i].value == "where":
            i += 1
            where_var, i = self._expect_ident(tokens, i)
            if where_var != var1:
                raise QueryError(f"WHERE refers to unknown variable '{where_var}' (position {tokens[i].pos})")
            i = self._expect_symbol(tokens, i, ".")
            where_prop, i = self._expect_ident(tokens, i)
            i = self._expect_symbol(tokens, i, "=")
            where_val, i = self._parse_value(tokens, i)

        i = self._expect_keyword(tokens, i, "return")
        ret_var, i = self._expect_ident(tokens, i)
        if ret_var != var1:
            raise QueryError(f"RETURN refers to unknown variable '{ret_var}' (position {tokens[i-1].pos})")
        self._expect_end(tokens, i)

        if label1 is None:
            nodes = self.db.all_nodes()
        elif where_prop is not None:
            nodes = self.db.find_by_label_property(label1, where_prop, where_val)
        else:
            nodes = self.db.find_by_label(label1)
        return nodes

    def _parse_match_traverse(self, tokens: List[Token], i: int, var1: str, label1: Optional[str]):
        i = self._expect_symbol(tokens, i, "-")
        i = self._expect_symbol(tokens, i, "[")
        i = self._expect_symbol(tokens, i, ":")
        rel_type, i = self._expect_ident(tokens, i)
        min_hops, max_hops = 1, 1
        if tokens[i].kind == "SYMBOL" and tokens[i].value == "*":
            i += 1
            first, i = self._expect_number(tokens, i)
            min_hops = max_hops = int(first)
            if tokens[i].kind == "SYMBOL" and tokens[i].value == "..":
                i += 1
                second, i = self._expect_number(tokens, i)
                max_hops = int(second)
        i = self._expect_symbol(tokens, i, "]")
        i = self._expect_symbol(tokens, i, "->")
        i = self._expect_symbol(tokens, i, "(")
        var2, i = self._expect_ident(tokens, i)
        label2: Optional[str] = None
        if tokens[i].kind == "SYMBOL" and tokens[i].value == ":":
            i += 1
            label2, i = self._expect_ident(tokens, i)
        i = self._expect_symbol(tokens, i, ")")
        i = self._expect_keyword(tokens, i, "return")
        ret_var, i = self._expect_ident(tokens, i)
        if ret_var not in (var1, var2):
            raise QueryError(f"RETURN refers to unknown variable '{ret_var}'")
        self._expect_end(tokens, i)

        if label1 is not None:
            start_nodes = self.db.find_by_label(label1)
        else:
            start_nodes = self.db.all_nodes()

        result_ids = set()
        for node in start_nodes:
            result_ids |= self.db.traverse(node.id, rel_type, min_hops, max_hops)

        results = [self.db.get_node(nid) for nid in result_ids if self.db.get_node(nid) is not None]
        if label2 is not None:
            results = [n for n in results if label2 in n.labels]
        return results

    def _parse_match_shortest(self, tokens: List[Token], i: int):
        i = self._expect_symbol(tokens, i, "(")
        from_id, i = self._expect_number(tokens, i)
        i = self._expect_symbol(tokens, i, ")")
        i = self._expect_keyword(tokens, i, "to")
        i = self._expect_symbol(tokens, i, "(")
        to_id, i = self._expect_number(tokens, i)
        i = self._expect_symbol(tokens, i, ")")
        rel_type = None
        if tokens[i].kind == "KEYWORD" and tokens[i].value == "via":
            i += 1
            rel_type, i = self._expect_ident(tokens, i)
        i = self._expect_keyword(tokens, i, "return")
        i = self._expect_keyword(tokens, i, "path")
        self._expect_end(tokens, i)
        path = self.db.shortest_path(int(from_id), int(to_id), rel_type)
        return path

    # -- DELETE -----------------------------------------------------------
    def _parse_delete(self, tokens: List[Token]):
        i = self._expect_keyword(tokens, 0, "delete")
        tok = tokens[i]
        if tok.kind == "KEYWORD" and tok.value == "node":
            i += 1
            node_id, i = self._expect_number(tokens, i)
            self._expect_end(tokens, i)
            try:
                self.db.delete_node(int(node_id))
            except GraphError as exc:
                raise QueryError(str(exc)) from exc
            return f"Deleted node {int(node_id)}"
        if tok.kind == "KEYWORD" and tok.value == "edge":
            i += 1
            edge_id, i = self._expect_number(tokens, i)
            self._expect_end(tokens, i)
            try:
                self.db.delete_edge(int(edge_id))
            except GraphError as exc:
                raise QueryError(str(exc)) from exc
            return f"Deleted edge {int(edge_id)}"
        raise QueryError(f"Expected 'NODE' or 'EDGE' after DELETE but found {self._describe(tok)} (position {tok.pos})")

    # -- SET ---------------------------------------------------------------
    def _parse_set(self, tokens: List[Token]):
        i = self._expect_keyword(tokens, 0, "set")
        i = self._expect_keyword(tokens, i, "node")
        node_id, i = self._expect_number(tokens, i)
        i = self._expect_symbol(tokens, i, ".")
        prop, i = self._expect_ident(tokens, i)
        i = self._expect_symbol(tokens, i, "=")
        value, i = self._parse_value(tokens, i)
        self._expect_end(tokens, i)
        try:
            node = self.db.update_node_properties(int(node_id), {prop: value})
        except GraphError as exc:
            raise QueryError(str(exc)) from exc
        return node

    # -- misc --------------------------------------------------------------
    def _expect_end(self, tokens: List[Token], i: int) -> None:
        if tokens[i].kind != "EOF":
            raise QueryError(f"Unexpected trailing input {self._describe(tokens[i])} (position {tokens[i].pos})")


def run_statement(db: GraphDB, statement: str):
    """Convenience wrapper: parse-and-execute a single statement string."""
    return Parser(db).execute(statement)


def run_script(db: GraphDB, script_text: str) -> List[Tuple[str, object]]:
    """
    Execute a multi-statement script. Statements are separated by newlines
    and/or semicolons; blank lines and ``#`` comments are ignored.
    Returns a list of (statement_text, result_or_exception) pairs, in order.
    """
    results: List[Tuple[str, object]] = []
    for raw_line in script_text.splitlines():
        line = raw_line.split("#", 1)[0]
        for stmt in line.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                result = run_statement(db, stmt)
                results.append((stmt, result))
            except QueryError as exc:
                results.append((stmt, exc))
    return results
