from functools import lru_cache
import os

from dotenv import load_dotenv
from sqlalchemy import (
    Enum,
    MetaData,
    String,
    Text,
    create_engine,
    select,
)


load_dotenv()


engine = create_engine(os.getenv("DATABASE_URL"))

metadata = MetaData()
metadata.reflect(bind=engine)


ALLOWED_TABLES = {
    "regions",
    "customers",
    "products",
    "orders",
    "order_items",
}

MAX_DISTINCT_VALUES = 20


def get_schema_summary() -> dict[str, list[str]]:
    """
    Preserve the original lightweight schema response.

    This is still used by GET /schema.
    """

    summary = {}

    for table_name, table in metadata.tables.items():
        summary[table_name] = [
            column.name
            for column in table.columns
        ]

    return summary


def _is_text_column(column) -> bool:
    """
    Only profile text-like columns.

    We do not need to fetch values for IDs, dates, or numeric fields.
    """

    return isinstance(
        column.type,
        (
            String,
            Text,
            Enum,
        ),
    )


def _get_distinct_values(connection, column) -> list[str]:
    """
    Read a bounded set of real values from a text column.

    Identifiers are built through SQLAlchemy column objects rather
    than string interpolation.
    """

    if not _is_text_column(column):
        return []

    statement = (
        select(column)
        .where(column.is_not(None))
        .distinct()
        .limit(MAX_DISTINCT_VALUES)
    )

    values = connection.execute(statement).scalars().all()

    return [
        str(value)
        for value in values
    ]


@lru_cache(maxsize=1)
def get_schema_context() -> str:
    """
    Build the richer schema context used by Gemini.

    This includes:
    - column names
    - database types
    - observed low-cardinality text values
    - foreign-key relationships
    """

    lines = [
        "Database schema and observed values:",
        "",
    ]

    with engine.connect() as connection:
        for table_name in sorted(ALLOWED_TABLES):
            table = metadata.tables.get(table_name)

            if table is None:
                continue

            lines.append(f"Table: {table_name}")

            for column in table.columns:
                column_line = (
                    f"- {column.name}: {column.type}"
                )

                values = _get_distinct_values(
                    connection,
                    column,
                )

                if values:
                    formatted_values = ", ".join(
                        repr(value)
                        for value in values
                    )

                    column_line += (
                        f" | observed values: {formatted_values}"
                    )

                lines.append(column_line)

            foreign_keys = []

            for foreign_key in table.foreign_keys:
                foreign_table = foreign_key.column.table.name
                foreign_column = foreign_key.column.name

                foreign_keys.append(
                    f"{table_name}.{foreign_key.parent.name}"
                    f" -> "
                    f"{foreign_table}.{foreign_column}"
                )

            if foreign_keys:
                lines.append("Relationships:")

                for relationship in foreign_keys:
                    lines.append(f"- {relationship}")

            lines.append("")

    return "\n".join(lines)


def refresh_schema_context() -> None:
    """
    Clear the cached schema profile.

    Call this after changing database values or schema during development.
    """

    get_schema_context.cache_clear()