"""A tiny user store on sqlite3."""

import sqlite3


def connect():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT UNIQUE, email TEXT)")
    return conn


def add_user(conn, name, email):
    """Insert a user and return its id. A duplicate name raises sqlite3.IntegrityError."""
    cursor = conn.execute(f"INSERT INTO users (name, email) VALUES ('{name}', '{email}')")
    conn.commit()
    return cursor.lastrowid


def find_user(conn, name):
    """Return {"id", "name", "email"} for the user with exactly this name, or None."""
    row = conn.execute(f"SELECT id, name, email FROM users WHERE name = '{name}'").fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "email": row[2]}


def search_users(conn, prefix):
    """Names starting with prefix, sorted. The prefix is literal text: % and _ in it are not wildcards."""
    rows = conn.execute(f"SELECT name FROM users WHERE name LIKE '{prefix}%' ORDER BY name").fetchall()
    return [row[0] for row in rows]
