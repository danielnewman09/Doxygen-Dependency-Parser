"""
SQLite backend — ingests ParseResult into a SQLite database.

No external dependencies — uses Python's built-in sqlite3 module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from doxygen_index.parser import ParseResult, parse_xml_dir


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """\
-- Files
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    refid TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    path TEXT,
    language TEXT,
    source TEXT DEFAULT 'msd'
);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
CREATE INDEX IF NOT EXISTS idx_files_source ON files(source);

-- Namespaces
CREATE TABLE IF NOT EXISTS namespaces (
    id INTEGER PRIMARY KEY,
    refid TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    source TEXT DEFAULT 'msd'
);
CREATE INDEX IF NOT EXISTS idx_namespaces_name ON namespaces(name);
CREATE INDEX IF NOT EXISTS idx_namespaces_source ON namespaces(source);

-- Compounds: classes, structs, unions, enums
CREATE TABLE IF NOT EXISTS compounds (
    id INTEGER PRIMARY KEY,
    refid TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_id INTEGER REFERENCES files(id),
    line_number INTEGER,
    brief_description TEXT,
    detailed_description TEXT,
    base_classes TEXT,
    is_final INTEGER DEFAULT 0,
    is_abstract INTEGER DEFAULT 0,
    source TEXT DEFAULT 'msd'
);
CREATE INDEX IF NOT EXISTS idx_compounds_name ON compounds(name);
CREATE INDEX IF NOT EXISTS idx_compounds_qualified_name ON compounds(qualified_name);
CREATE INDEX IF NOT EXISTS idx_compounds_kind ON compounds(kind);
CREATE INDEX IF NOT EXISTS idx_compounds_file_id ON compounds(file_id);
CREATE INDEX IF NOT EXISTS idx_compounds_source ON compounds(source);

-- Members: functions, variables, typedefs
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    refid TEXT UNIQUE NOT NULL,
    compound_id INTEGER REFERENCES compounds(id),
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    type_signature TEXT,
    definition TEXT,
    argsstring TEXT,
    file_id INTEGER REFERENCES files(id),
    line_number INTEGER,
    brief_description TEXT,
    detailed_description TEXT,
    protection TEXT,
    is_static INTEGER DEFAULT 0,
    is_const INTEGER DEFAULT 0,
    is_constexpr INTEGER DEFAULT 0,
    is_virtual INTEGER DEFAULT 0,
    is_inline INTEGER DEFAULT 0,
    is_explicit INTEGER DEFAULT 0,
    source TEXT DEFAULT 'msd'
);
CREATE INDEX IF NOT EXISTS idx_members_name ON members(name);
CREATE INDEX IF NOT EXISTS idx_members_qualified_name ON members(qualified_name);
CREATE INDEX IF NOT EXISTS idx_members_kind ON members(kind);
CREATE INDEX IF NOT EXISTS idx_members_compound_id ON members(compound_id);
CREATE INDEX IF NOT EXISTS idx_members_file_id ON members(file_id);
CREATE INDEX IF NOT EXISTS idx_members_source ON members(source);

-- Parameters
CREATE TABLE IF NOT EXISTS parameters (
    id INTEGER PRIMARY KEY,
    member_id INTEGER REFERENCES members(id),
    position INTEGER NOT NULL,
    name TEXT,
    type TEXT NOT NULL,
    default_value TEXT,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_parameters_member_id ON parameters(member_id);

-- Symbol references (call graph)
CREATE TABLE IF NOT EXISTS symbol_refs (
    id INTEGER PRIMARY KEY,
    from_member_id INTEGER,
    to_member_refid TEXT NOT NULL,
    to_member_name TEXT NOT NULL,
    relationship TEXT NOT NULL,
    FOREIGN KEY (from_member_id) REFERENCES members(id)
);
CREATE INDEX IF NOT EXISTS idx_symbol_refs_from ON symbol_refs(from_member_id);
CREATE INDEX IF NOT EXISTS idx_symbol_refs_to ON symbol_refs(to_member_refid);

-- Include dependencies
CREATE TABLE IF NOT EXISTS includes (
    id INTEGER PRIMARY KEY,
    file_id INTEGER REFERENCES files(id),
    included_file TEXT NOT NULL,
    included_refid TEXT,
    is_local INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_includes_file_id ON includes(file_id);

-- Full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs USING fts5(
    name,
    qualified_name,
    description,
    tokenize='porter'
);

-- Metadata
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def create_schema(conn: sqlite3.Connection) -> None:
    """Create or update the database schema."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def clear_source(conn: sqlite3.Connection, source: str) -> None:
    """Remove all data for a specific source."""
    conn.execute("DELETE FROM symbol_refs WHERE from_member_id IN (SELECT id FROM members WHERE source = ?)", (source,))
    conn.execute("DELETE FROM parameters WHERE member_id IN (SELECT id FROM members WHERE source = ?)", (source,))
    conn.execute("DELETE FROM includes WHERE file_id IN (SELECT id FROM files WHERE source = ?)", (source,))
    conn.execute("DELETE FROM members WHERE source = ?", (source,))
    conn.execute("DELETE FROM compounds WHERE source = ?", (source,))
    conn.execute("DELETE FROM namespaces WHERE source = ?", (source,))
    conn.execute("DELETE FROM files WHERE source = ?", (source,))
    conn.commit()


def write_result(conn: sqlite3.Connection, result: ParseResult) -> dict[str, int]:
    """Write a ParseResult to SQLite. Returns counts of inserted rows."""
    file_cache: dict[str, int] = {}
    compound_cache: dict[str, int] = {}

    # Files
    for f in result.files:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO files (refid, name, path, language, source) VALUES (?, ?, ?, ?, ?)",
            (f.refid, f.name, f.path, f.language, f.source),
        )
        if cursor.lastrowid:
            file_cache[f.refid] = cursor.lastrowid
        else:
            row = conn.execute("SELECT id FROM files WHERE refid = ?", (f.refid,)).fetchone()
            if row:
                file_cache[f.refid] = row[0]

    # Includes
    for inc in result.includes:
        file_id = file_cache.get(inc.file_refid)
        if file_id:
            conn.execute(
                "INSERT INTO includes (file_id, included_file, included_refid, is_local) VALUES (?, ?, ?, ?)",
                (file_id, inc.included_file, inc.included_refid, int(inc.is_local)),
            )

    # Namespaces
    for ns in result.namespaces:
        conn.execute(
            "INSERT OR IGNORE INTO namespaces (refid, name, qualified_name, source) VALUES (?, ?, ?, ?)",
            (ns.refid, ns.name, ns.qualified_name, ns.source),
        )

    # Compounds
    for c in result.compounds:
        file_id = None
        if c.file_path:
            row = conn.execute("SELECT id FROM files WHERE path = ?", (c.file_path,)).fetchone()
            if row:
                file_id = row[0]

        base_classes_json = str(c.base_classes) if c.base_classes else None

        cursor = conn.execute(
            """INSERT OR REPLACE INTO compounds
               (refid, kind, name, qualified_name, file_id, line_number,
                brief_description, detailed_description, base_classes,
                is_final, is_abstract, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (c.refid, c.kind, c.name, c.qualified_name, file_id, c.line_number,
             c.brief_description, c.detailed_description, base_classes_json,
             int(c.is_final), int(c.is_abstract), c.source),
        )
        compound_cache[c.refid] = cursor.lastrowid

        description = f"{c.brief_description} {c.detailed_description}".strip()
        if description:
            conn.execute(
                "INSERT INTO fts_docs (name, qualified_name, description) VALUES (?, ?, ?)",
                (c.name, c.qualified_name, description),
            )

    # Members
    for m in result.members:
        compound_id = compound_cache.get(m.compound_refid)

        file_id = None
        if m.file_path:
            row = conn.execute("SELECT id FROM files WHERE path = ?", (m.file_path,)).fetchone()
            if row:
                file_id = row[0]

        cursor = conn.execute(
            """INSERT OR REPLACE INTO members
               (refid, compound_id, kind, name, qualified_name, type_signature, definition,
                argsstring, file_id, line_number, brief_description, detailed_description,
                protection, is_static, is_const, is_constexpr, is_virtual,
                is_inline, is_explicit, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (m.refid, compound_id, m.kind, m.name, m.qualified_name,
             getattr(m, 'type_signature', ''),
             getattr(m, 'definition', ''),
             getattr(m, 'argsstring', ''),
             file_id, m.line_number,
             m.brief_description, m.detailed_description,
             getattr(m, 'protection', ''),
             int(getattr(m, 'is_static', False)),
             int(getattr(m, 'is_const', False)),
             int(getattr(m, 'is_constexpr', False)),
             int(getattr(m, 'is_virtual', False)),
             int(getattr(m, 'is_inline', False)),
             int(getattr(m, 'is_explicit', False)),
             m.source),
        )
        member_id = cursor.lastrowid

        description = f"{m.brief_description} {m.detailed_description}".strip()
        if description:
            conn.execute(
                "INSERT INTO fts_docs (name, qualified_name, description) VALUES (?, ?, ?)",
                (m.name, m.qualified_name, description),
            )

        # Symbol refs for this member — matched by refid
        for call in result.calls:
            if call.from_refid == m.refid:
                conn.execute(
                    "INSERT INTO symbol_refs (from_member_id, to_member_refid, to_member_name, relationship) VALUES (?, ?, ?, ?)",
                    (member_id, call.to_refid, call.to_name, "calls"),
                )
        for cb in result.called_by:
            if cb.from_refid == m.refid:
                conn.execute(
                    "INSERT INTO symbol_refs (from_member_id, to_member_refid, to_member_name, relationship) VALUES (?, ?, ?, ?)",
                    (member_id, cb.to_refid, cb.to_name, "called_by"),
                )

    # Parameters
    for p in result.parameters:
        row = conn.execute("SELECT id FROM members WHERE refid = ?", (p.member_refid,)).fetchone()
        if row:
            conn.execute(
                "INSERT INTO parameters (member_id, position, name, type, default_value) VALUES (?, ?, ?, ?, ?)",
                (row[0], p.position, p.name, p.type, p.default_value),
            )

    conn.commit()

    return {
        "files": len(result.files),
        "namespaces": len(result.namespaces),
        "compounds": len(result.compounds),
        "members": len(result.members),
        "parameters": len(result.parameters),
        "calls": len(result.calls),
        "includes": len(result.includes),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest(
    xml_dir: Path | str,
    db_path: Path | str,
    source: str = "msd",
    append: bool = True,
) -> dict[str, int]:
    """Parse Doxygen XML and ingest into SQLite.

    Args:
        xml_dir: Directory containing Doxygen XML output.
        db_path: Path to SQLite database file (created if it doesn't exist).
        source: Source label for provenance tracking.
        append: If True, append to existing database. If False, recreate it.

    Returns:
        Dict of entity counts.
    """
    xml_dir = Path(xml_dir)
    db_path = Path(db_path)

    if not append and db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    create_schema(conn)

    if append and db_path.stat().st_size > 0:
        clear_source(conn, source)
        print(f"  Cleared existing '{source}' data.")

    print(f"Parsing {xml_dir}...")
    result = parse_xml_dir(xml_dir, source=source)

    print("Writing to SQLite...")
    counts = write_result(conn, result)

    print(f"\nStatistics ({source}):")
    for key, value in counts.items():
        print(f"  {key}: {value}")

    conn.close()
    return counts
