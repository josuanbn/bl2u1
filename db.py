"""Lightweight SQLite persistence for the inventory feature."""

import json
import os
import sqlite3
from datetime import datetime, timezone

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS inventory_items (
    id              TEXT PRIMARY KEY,
    original_name   TEXT NOT NULL,
    stored_name     TEXT NOT NULL,
    upload_date     TEXT NOT NULL,
    file_size       INTEGER NOT NULL,
    filament_count  INTEGER NOT NULL DEFAULT 0,
    filament_colors TEXT NOT NULL DEFAULT '[]',
    filament_types  TEXT NOT NULL DEFAULT '[]',
    was_converted   INTEGER NOT NULL DEFAULT 0,
    source_printer  TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '[]'
);
"""

_MIGRATIONS = [
    # Add title, description, tags columns if missing (upgrade from v1 schema)
    ("title", "ALTER TABLE inventory_items ADD COLUMN title TEXT NOT NULL DEFAULT ''"),
    ("description", "ALTER TABLE inventory_items ADD COLUMN description TEXT NOT NULL DEFAULT ''"),
    ("tags", "ALTER TABLE inventory_items ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"),
]

_db_path: str = ''


def init_db(db_path: str) -> None:
    """Create the database and table if they don't exist."""
    global _db_path
    _db_path = db_path
    os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
    with _connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(_CREATE_TABLE)
        # Run migrations for existing databases
        existing = {row[1] for row in con.execute("PRAGMA table_info(inventory_items)").fetchall()}
        for col_name, sql in _MIGRATIONS:
            if col_name not in existing:
                con.execute(sql)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def add_item(
    item_id: str,
    original_name: str,
    stored_name: str,
    file_size: int,
    filament_count: int = 0,
    filament_colors: list | None = None,
    filament_types: list | None = None,
    was_converted: bool = False,
    source_printer: str = '',
    title: str = '',
    description: str = '',
    tags: list | None = None,
) -> dict:
    """Insert a new inventory item. Returns the created row as a dict."""
    upload_date = datetime.now(timezone.utc).isoformat()
    with _connect() as con:
        con.execute(
            "INSERT INTO inventory_items "
            "(id, original_name, stored_name, upload_date, file_size, "
            " filament_count, filament_colors, filament_types, was_converted, source_printer,"
            " title, description, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                original_name,
                stored_name,
                upload_date,
                file_size,
                filament_count,
                json.dumps(filament_colors or []),
                json.dumps(filament_types or []),
                int(was_converted),
                source_printer,
                title,
                description,
                json.dumps(tags or []),
            ),
        )
    return _row_to_dict_raw(item_id, original_name, stored_name, upload_date,
                            file_size, filament_count, filament_colors or [],
                            filament_types or [], was_converted, source_printer,
                            title, description, tags or [])


def update_item(item_id: str, **kwargs) -> dict | None:
    """Update fields on an existing item. Returns updated item or None."""
    allowed = {'title', 'description', 'tags'}
    updates = {}
    for key, val in kwargs.items():
        if key not in allowed:
            continue
        if key == 'tags':
            updates[key] = json.dumps(val if isinstance(val, list) else [])
        else:
            updates[key] = val

    if not updates:
        return get_item(item_id)

    set_clause = ', '.join(f'{k} = ?' for k in updates)
    values = list(updates.values()) + [item_id]
    with _connect() as con:
        con.execute(f"UPDATE inventory_items SET {set_clause} WHERE id = ?", values)
    return get_item(item_id)


def get_item(item_id: str) -> dict | None:
    """Return a single item or None."""
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM inventory_items WHERE id = ?", (item_id,)
        ).fetchone()
    return _deserialize(row) if row else None


def list_items(sort_by: str = 'upload_date', order: str = 'desc',
               search: str = '') -> list[dict]:
    """Return inventory items, optionally filtered by search query."""
    allowed_sort = {'upload_date', 'original_name', 'file_size', 'filament_count', 'title'}
    if sort_by not in allowed_sort:
        sort_by = 'upload_date'
    if order.lower() not in ('asc', 'desc'):
        order = 'desc'

    if search.strip():
        query = (
            f"SELECT * FROM inventory_items "
            f"WHERE title LIKE ? OR description LIKE ? OR tags LIKE ? OR original_name LIKE ? "
            f"ORDER BY {sort_by} {order}"
        )
        term = f'%{search.strip()}%'
        params = (term, term, term, term)
    else:
        query = f"SELECT * FROM inventory_items ORDER BY {sort_by} {order}"
        params = ()

    with _connect() as con:
        rows = con.execute(query, params).fetchall()
    return [_deserialize(r) for r in rows]


def delete_item(item_id: str) -> bool:
    """Delete an item by ID. Returns True if a row was deleted."""
    with _connect() as con:
        cur = con.execute("DELETE FROM inventory_items WHERE id = ?", (item_id,))
    return cur.rowcount > 0


def _deserialize(row: sqlite3.Row) -> dict:
    """Convert a DB row to a dict with JSON fields parsed."""
    d = dict(row)
    d['filament_colors'] = json.loads(d.get('filament_colors', '[]'))
    d['filament_types'] = json.loads(d.get('filament_types', '[]'))
    d['tags'] = json.loads(d.get('tags', '[]'))
    d['was_converted'] = bool(d.get('was_converted', 0))
    return d


def _row_to_dict_raw(item_id, original_name, stored_name, upload_date,
                     file_size, filament_count, filament_colors,
                     filament_types, was_converted, source_printer,
                     title, description, tags) -> dict:
    return {
        'id': item_id,
        'original_name': original_name,
        'stored_name': stored_name,
        'upload_date': upload_date,
        'file_size': file_size,
        'filament_count': filament_count,
        'filament_colors': filament_colors,
        'filament_types': filament_types,
        'was_converted': was_converted,
        'source_printer': source_printer,
        'title': title,
        'description': description,
        'tags': tags,
    }
