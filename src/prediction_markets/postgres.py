"""Connection to Postgres, and schema enforcement."""

from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def connect(dsn: str) -> psycopg.Connection:
    """Opens the connection, ensures the schema, and registers pgvector.

    Every statement in schema.sql is `IF NOT EXISTS` or `OR REPLACE`, so
    running it on each connection is idempotent and keeps a single source of
    truth for the shape of the database.
    """
    connection = psycopg.connect(dsn, autocommit=True)
    connection.execute(_SCHEMA)
    # Has to happen after the schema runs: the adapter can only be
    # registered once the `vector` type exists in the database.
    register_vector(connection)
    return connection
