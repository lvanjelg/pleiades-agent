"""Graph store — persistent nodes/edges for cross-session memory (README step 7).

Nodes for conversations, tasks, messages, tools, skills, SOPs and artifacts;
edges for ``created_in``, ``used_in``, ``succeeded``, ``failed``,
``composed_of`` and ``references``. This is where *relationship* queries live
that plain SQL rows can't answer comfortably: tool/skill lineage, which tools
are used together, cross-session usage and success rates, recent tasks, and
free-text search over past nodes.

Persistence: SQLite, defaulting to the same ``agent_state.db`` the rest of the
harness uses, so the graph survives restarts and is shared across sessions
(cross-session persistence). Edges carry **aggregated counters** in their JSON
properties (``count``, ``ok``, ``err``, ``last_ms``) so one row represents a
relationship instead of one row per event.

The harness writes to this store as it runs (see ``AgentHarness`` in main.py)
and exposes it to the model through the ``graph_query`` tool, so the agent can
recall work from previous sessions.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime as dt, UTC
from typing import Any, Optional

NODE_TYPES = ("conversation", "task", "message", "tool", "skill", "sop", "artifact")
EDGE_TYPES = ("created_in", "used_in", "succeeded", "failed", "composed_of", "references")


def _now() -> str:
    return dt.now(UTC).isoformat()


def _json_loads(text: Optional[str]) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


class GraphStore:
    """SQLite-backed node/edge store shared across sessions."""

    def __init__(self, db_path: str = "agent_state.db"):
        # The TUI runs harness.run() on a worker thread; only one turn runs at a
        # time, so relaxing the same-thread check is safe (as with AgentState).
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS graph_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL, key TEXT NOT NULL,
            label TEXT, properties TEXT,
            session_id TEXT, created_at TEXT, updated_at TEXT,
            UNIQUE(type, key))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS graph_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src_id INTEGER NOT NULL, dst_id INTEGER NOT NULL,
            relation TEXT NOT NULL, properties TEXT,
            session_id TEXT, created_at TEXT, updated_at TEXT,
            UNIQUE(src_id, dst_id, relation))""")
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_nodes_type ON graph_nodes(type)",
            "CREATE INDEX IF NOT EXISTS idx_edges_src ON graph_edges(src_id)",
            "CREATE INDEX IF NOT EXISTS idx_edges_dst ON graph_edges(dst_id)",
            "CREATE INDEX IF NOT EXISTS idx_edges_rel ON graph_edges(relation)",
        ):
            self.db.execute(stmt)
        self.db.commit()

    # ----------------------------------------------------------- nodes
    def upsert_node(self, type: str, key: str, label: Optional[str] = None,
                    properties: Optional[dict] = None, session_id: str = "") -> int:
        """Create or refresh a node; returns its id. Identity is (type, key)."""
        now = _now()
        key = str(key)
        self.db.execute(
            "INSERT INTO graph_nodes (type, key, label, properties, session_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(type, key) DO UPDATE SET "
            "label=excluded.label, properties=excluded.properties, updated_at=excluded.updated_at",
            (type, key, label or key, json.dumps(properties or {}), session_id, now, now))
        self.db.commit()
        row = self.db.execute("SELECT id FROM graph_nodes WHERE type=? AND key=?",
                              (type, key)).fetchone()
        return int(row[0])

    def get_node(self, type: str, key: str) -> Optional[dict]:
        row = self.db.execute(
            "SELECT id, type, key, label, properties, session_id, created_at, updated_at "
            "FROM graph_nodes WHERE type=? AND key=?", (type, str(key))).fetchone()
        return self._node_row(row) if row else None

    def node(self, node_id: int) -> Optional[dict]:
        row = self.db.execute(
            "SELECT id, type, key, label, properties, session_id, created_at, updated_at "
            "FROM graph_nodes WHERE id=?", (node_id,)).fetchone()
        return self._node_row(row) if row else None

    def nodes(self, type: Optional[str] = None, limit: int = 100) -> list[dict]:
        if type:
            rows = self.db.execute(
                "SELECT id, type, key, label, properties, session_id, created_at, updated_at "
                "FROM graph_nodes WHERE type=? ORDER BY updated_at DESC LIMIT ?",
                (type, limit)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id, type, key, label, properties, session_id, created_at, updated_at "
                "FROM graph_nodes ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._node_row(r) for r in rows]

    @staticmethod
    def _node_row(row) -> dict:
        return {"id": row[0], "type": row[1], "key": row[2], "label": row[3],
                "properties": _json_loads(row[4]), "session_id": row[5],
                "created_at": row[6], "updated_at": row[7]}

    # ----------------------------------------------------------- edges
    def touch_edge(self, src_id: int, dst_id: int, relation: str, session_id: str = "",
                   increments: Optional[dict[str, int]] = None,
                   properties: Optional[dict] = None) -> int:
        """Create or update an edge. ``increments`` add to numeric counters in the
        edge's properties (e.g. {"count": 1, "last_ms": 42}); ``properties`` set
        values outright. Returns the edge id."""
        row = self.db.execute(
            "SELECT id, properties FROM graph_edges WHERE src_id=? AND dst_id=? AND relation=?",
            (src_id, dst_id, relation)).fetchone()
        now = _now()
        if row is None:
            props: dict[str, Any] = {}
            for k, v in (increments or {}).items():
                props[k] = int(v)
            props.update(properties or {})
            self.db.execute(
                "INSERT INTO graph_edges (src_id, dst_id, relation, properties, session_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (src_id, dst_id, relation, json.dumps(props), session_id, now, now))
            self.db.commit()
            new_row = self.db.execute(
                "SELECT id FROM graph_edges WHERE src_id=? AND dst_id=? AND relation=?",
                (src_id, dst_id, relation)).fetchone()
            return int(new_row[0])
        props = _json_loads(row[1])
        for k, v in (increments or {}).items():
            props[k] = int(props.get(k, 0)) + int(v)
        props.update(properties or {})
        self.db.execute("UPDATE graph_edges SET properties=?, updated_at=? WHERE id=?",
                        (json.dumps(props), now, row[0]))
        self.db.commit()
        return int(row[0])

    def add_edge(self, src_id: int, dst_id: int, relation: str, session_id: str = "",
                 properties: Optional[dict] = None) -> int:
        return self.touch_edge(src_id, dst_id, relation, session_id=session_id,
                               properties=properties)

    def edges(self, src_id: Optional[int] = None, dst_id: Optional[int] = None,
              relation: Optional[str] = None, session_id: Optional[str] = None) -> list[dict]:
        clauses, params = [], []
        if src_id is not None:
            clauses.append("src_id=?"); params.append(src_id)
        if dst_id is not None:
            clauses.append("dst_id=?"); params.append(dst_id)
        if relation is not None:
            clauses.append("relation=?"); params.append(relation)
        if session_id is not None:
            clauses.append("session_id=?"); params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.execute(
            f"SELECT id, src_id, dst_id, relation, properties, session_id, updated_at "
            f"FROM graph_edges {where}", params).fetchall()
        return [{"id": r[0], "src_id": r[1], "dst_id": r[2], "relation": r[3],
                 "properties": _json_loads(r[4]), "session_id": r[5], "updated_at": r[6]}
                for r in rows]

    # ---------------------------------------------------------- queries
    def neighbors(self, node_id: int, relation: Optional[str] = None,
                  direction: str = "both") -> list[dict]:
        """Return [{edge, node}] adjacent to ``node_id`` (optionally by relation)."""
        out: list[dict] = []
        if direction in ("out", "both"):
            for edge in self.edges(src_id=node_id, relation=relation):
                node = self.node(edge["dst_id"])
                if node:
                    out.append({"edge": edge, "node": node, "dir": "out"})
        if direction in ("in", "both"):
            for edge in self.edges(dst_id=node_id, relation=relation):
                node = self.node(edge["src_id"])
                if node:
                    out.append({"edge": edge, "node": node, "dir": "in"})
        return out

    def traverse(self, node_id: int, relation: Optional[str] = None,
                 depth: int = 2, direction: str = "out") -> list[dict]:
        """Breadth-first traversal returning nodes with the depth they were found at."""
        seen = {node_id}
        frontier = [(node_id, 0)]
        found: list[dict] = []
        while frontier:
            current, level = frontier.pop(0)
            if level >= depth:
                continue
            for hit in self.neighbors(current, relation=relation, direction=direction):
                node = hit["node"]
                if node["id"] in seen:
                    continue
                seen.add(node["id"])
                found.append({"depth": level + 1, "relation": hit["edge"]["relation"], "node": node})
                frontier.append((node["id"], level + 1))
        return found

    def tool_stats(self) -> list[dict]:
        """Per-tool cross-session usage: calls, successes, failures, sessions."""
        tools = self.nodes(type="tool", limit=1000)
        by_id = {t["id"]: t for t in tools}
        stats = {t["id"]: {"tool": t["key"], "calls": 0, "ok": 0, "err": 0,
                           "sessions": set(), "last_ms": 0} for t in tools}
        for edge in self.edges(relation="used_in"):
            if edge["src_id"] not in stats:
                continue
            props = edge["properties"]
            stats[edge["src_id"]]["calls"] += int(props.get("count", 0))
            stats[edge["src_id"]]["last_ms"] = int(props.get("last_ms", 0) or 0)
            # distinct conversations this tool was used in (dst = conversation node)
            stats[edge["src_id"]]["sessions"].add(edge["dst_id"])
        for edge in self.edges(relation="succeeded"):
            if edge["src_id"] in stats:
                stats[edge["src_id"]]["ok"] += int(edge["properties"].get("count", 0))
        for edge in self.edges(relation="failed"):
            if edge["src_id"] in stats:
                stats[edge["src_id"]]["err"] += int(edge["properties"].get("count", 0))
        out = []
        for item in stats.values():
            item["conversations"] = len(item.pop("sessions"))
            if item["calls"] or item["ok"] or item["err"]:
                out.append(item)
        out.sort(key=lambda i: (i["calls"], i["ok"]), reverse=True)
        return out

    def tools_used_together(self, limit: int = 20) -> list[dict]:
        """Pairs of tools that appear in the same conversations (co-occurrence)."""
        per_conversation: dict[int, set[int]] = {}
        for edge in self.edges(relation="used_in"):
            per_conversation.setdefault(edge["dst_id"], set()).add(edge["src_id"])
        names = {t["id"]: t["key"] for t in self.nodes(type="tool", limit=1000)}
        pairs: dict[tuple[int, int], int] = {}
        for tool_ids in per_conversation.values():
            ordered = sorted(tool_ids)
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    pairs[(a, b)] = pairs.get((a, b), 0) + 1
        result = [{"a": names[a], "b": names[b], "sessions": n}
                  for (a, b), n in pairs.items() if a in names and b in names]
        result.sort(key=lambda i: i["sessions"], reverse=True)
        return result[:limit]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        like = f"%{query}%"
        rows = self.db.execute(
            "SELECT id, type, key, label, properties, session_id, created_at, updated_at "
            "FROM graph_nodes WHERE key LIKE ? OR label LIKE ? "
            "ORDER BY updated_at DESC LIMIT ?", (like, like, limit)).fetchall()
        return [self._node_row(r) for r in rows]

    def lineage(self, type: str, key: str) -> dict:
        """Edges around a node: what it was created in, used in, succeeded/failed."""
        node = self.get_node(type, key)
        if not node:
            return {}
        return {
            "node": node,
            "created_in": [self.node(e["dst_id"]) for e in self.edges(src_id=node["id"], relation="created_in")],
            "used_in": [self.node(e["dst_id"]) for e in self.edges(src_id=node["id"], relation="used_in")],
            "created_here": [self.node(e["src_id"]) for e in self.edges(dst_id=node["id"], relation="created_in")],
        }

    def summary(self) -> dict:
        return {
            "nodes_by_type": {t: len(self.nodes(type=t, limit=100000)) for t in NODE_TYPES},
            "edges_by_relation": {r: len(self.edges(relation=r)) for r in EDGE_TYPES},
            "top_tools": self.tool_stats()[:5],
            "conversations": len(self.nodes(type="conversation", limit=100000)),
            "tasks": len(self.nodes(type="task", limit=100000)),
        }

    def digest(self, limit: int = 5) -> str:
        """Compact cross-session memory blurb for the system prompt (empty if
        nothing has been recorded yet)."""
        conversations = self.nodes(type="conversation", limit=limit)
        if not conversations:
            return ""
        lines = [f"- prior sessions on record: {len(self.nodes(type='conversation', limit=100000))}"]
        tools = self.tool_stats()[:limit]
        if tools:
            parts = [f"{t['tool']} ({t['calls']} calls, {t['ok']} ok)" for t in tools]
            lines.append("- tools used before: " + ", ".join(parts))
        recent_tasks = self.nodes(type="task", limit=limit)
        if recent_tasks:
            labels = [t["label"][:60] for t in recent_tasks if t["label"]]
            if labels:
                lines.append("- recent tasks: " + " | ".join(labels))
        lines.append("- use the graph_query tool to explore tools/skills/lineage across sessions")
        return "\n".join(lines)

    def query(self, kind: str = "summary", name: str = "", limit: int = 10) -> str:
        """Text answer for the ``graph_query`` tool (model-facing)."""
        limit = max(1, min(int(limit or 10), 100))
        if kind == "summary":
            data = self.summary()
            nodes = ", ".join(f"{t}:{n}" for t, n in data["nodes_by_type"].items() if n)
            edges = ", ".join(f"{r}:{n}" for r, n in data["edges_by_relation"].items() if n)
            lines = [f"Graph: {nodes or 'empty'}",
                     f"Edges: {edges or 'none'}",
                     f"Conversations: {data['conversations']} · Tasks: {data['tasks']}"]
            if data["top_tools"]:
                lines.append("Top tools: " + ", ".join(
                    f"{t['tool']} ({t['calls']} calls, {t['ok']} ok, {t['err']} err)" for t in data["top_tools"]))
            return "\n".join(lines)
        if kind == "tools":
            stats = self.tool_stats()[:limit]
            if not stats:
                return "No tool usage recorded yet."
            return "\n".join(
                f"- {t['tool']}: {t['calls']} calls · {t['ok']} ok · {t['err']} err · "
                f"{t['conversations']} conversations · last {t['last_ms']} ms" for t in stats)
        if kind == "skills":
            skills = self.nodes(type="skill", limit=limit)
            if not skills:
                return "No skills recorded."
            return "\n".join(f"- {s['key']}: {s['properties'].get('description', '')[:100]}" for s in skills)
        if kind == "sessions":
            convs = self.nodes(type="conversation", limit=limit)
            if not convs:
                return "No sessions recorded."
            return "\n".join(
                f"- {c['label']} ({c['key'][:8]}) · updated {c['updated_at'][:19]} · "
                f"model {c['properties'].get('model', '?')}" for c in convs)
        if kind == "tasks":
            tasks = self.nodes(type="task", limit=limit)
            if not tasks:
                return "No tasks recorded."
            return "\n".join(f"- [{t['session_id'][:8]}] turn {t['properties'].get('turn')}: {t['label']}" for t in tasks)
        if kind == "cooccurrence":
            pairs = self.tools_used_together(limit=limit)
            if not pairs:
                return "No co-occurrence yet."
            return "\n".join(f"- {p['a']} + {p['b']}: {p['sessions']} shared sessions" for p in pairs)
        if kind == "lineage":
            if not name:
                return "lineage needs a name (a tool or skill key)."
            found = None
            for node_type in ("tool", "skill", "sop", "artifact"):
                found = self.get_node(node_type, name)
                if found:
                    break
            if not found:
                return f"No node named '{name}'."
            lines = [f"{found['type']} '{found['key']}': {found['label']}"]
            for hit in self.neighbors(found["id"]):
                other = hit["node"]
                props = hit["edge"]["properties"]
                extra = f" {props}" if props else ""
                arrow = "->" if hit["dir"] == "out" else "<-"
                lines.append(f"  {arrow} {hit['edge']['relation']} {other['type']}:{other['key']}{extra}")
            return "\n".join(lines)
        if kind == "search":
            if not name:
                return "search needs a name (text to look for)."
            hits = self.search(name, limit=limit)
            if not hits:
                return f"No nodes matching '{name}'."
            return "\n".join(f"- {h['type']}:{h['key']} ({h['label'][:60]})" for h in hits)
        return ("Unknown kind. Use one of: summary, tools, skills, sessions, tasks, "
                "lineage, cooccurrence, search.")
