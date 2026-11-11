import json
import logging
import os
import random
import time
from typing import Any
import sqlglot
from sqlglot import exp
from dotenv import load_dotenv
from google import genai
from google.genai import types
from sqlalchemy import create_engine, text


# Load values from backend/.env
load_dotenv()


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logger = logging.getLogger("voxql.gemini")


# -----------------------------------------------------------------------------
# Gemini and database configuration
# -----------------------------------------------------------------------------

PRIMARY_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "").strip() or None

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY"),
    http_options=types.HttpOptions(
        # Maximum duration of a single Gemini request.
        timeout=30_000,

        # Disable SDK-level retries.
        # call_gemini() below owns the retry policy.
        retry_options=types.HttpRetryOptions(
            attempts=1,
        ),
    ),
)

engine = create_engine(os.getenv("DATABASE_URL"))
MAX_RESULT_ROWS = int(
    os.getenv("MAX_RESULT_ROWS", "100")
)


# -----------------------------------------------------------------------------
# Retry configuration
# -----------------------------------------------------------------------------

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.8
MAX_BACKOFF_SECONDS = 8.0

# These errors are considered temporary and may be retried.
RETRYABLE_STATUS_CODES = {
    408,  # Request timeout
    500,  # Internal server error
    502,  # Bad gateway
    503,  # Service unavailable
    504,  # Gateway timeout
}

# These errors may justify trying a configured fallback model.
FALLBACK_STATUS_CODES = {
    404,  # Model not found or unavailable
    500,
    502,
    503,
    504,
}


# -----------------------------------------------------------------------------
# Application-level Gemini exceptions
# -----------------------------------------------------------------------------

class GeminiServiceError(RuntimeError):
    """Base class for safe application-level Gemini errors."""


class GeminiRateLimitError(GeminiServiceError):
    """Gemini rate limit reached."""


class GeminiUnavailableError(GeminiServiceError):
    """Gemini remained unavailable after retry attempts."""


# -----------------------------------------------------------------------------
# Gemini helper functions
# -----------------------------------------------------------------------------

def _status_code(error: Exception) -> int | None:
    """
    Extract a numeric HTTP-like status code from a Gemini SDK exception.

    The Google SDK may expose the value as:
    - code
    - status_code
    - http_status
    """

    value = (
        getattr(error, "code", None)
        or getattr(error, "status_code", None)
        or getattr(error, "http_status", None)
    )

    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_transient_network_error(error: Exception) -> bool:
    """
    Detect common temporary network failures that do not expose
    an HTTP status code.
    """

    return isinstance(
        error,
        (
            TimeoutError,
            ConnectionError,
            OSError,
        ),
    )


def _backoff_delay(attempt: int) -> float:
    """
    Calculate exponential backoff with a small random jitter.

    Example:
    attempt 0 -> roughly 0.8 seconds
    attempt 1 -> roughly 1.6 seconds
    attempt 2 -> roughly 3.2 seconds
    """

    exponential_delay = min(
        BASE_BACKOFF_SECONDS * (2 ** attempt),
        MAX_BACKOFF_SECONDS,
    )

    jitter = random.uniform(0, 0.25)

    return exponential_delay + jitter


def call_gemini(
    *,
    contents: Any,
    config: types.GenerateContentConfig | None = None,
    operation: str,
):
    """
    Make a reliable Gemini request.

    Behavior:
    - Retries temporary 5xx/time-out failures.
    - Uses exponential backoff with jitter.
    - Fails fast on 429 rate limits.
    - Attempts a configured fallback model when appropriate.
    - Never exposes raw provider errors to the rest of the application.
    """

    models = [PRIMARY_MODEL]

    # Only use a fallback model when explicitly configured.
    if FALLBACK_MODEL and FALLBACK_MODEL != PRIMARY_MODEL:
        models.append(FALLBACK_MODEL)

    last_error: Exception | None = None

    for model_index, model in enumerate(models):
        for attempt in range(MAX_ATTEMPTS):
            try:
                return client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )

            except Exception as error:
                last_error = error
                status_code = _status_code(error)

                # Do not immediately retry rate limits.
                # Repeated requests can make throttling worse.
                if status_code == 429:
                    logger.warning(
                        "Gemini rate limit during %s using model=%s",
                        operation,
                        model,
                    )

                    raise GeminiRateLimitError(
                        "The analytics service is busy right now. "
                        "Please try again shortly."
                    ) from error

                retryable = (
                    status_code in RETRYABLE_STATUS_CODES
                    or _is_transient_network_error(error)
                )

                if not retryable:
                    can_use_fallback = (
                        model_index == 0
                        and len(models) > 1
                        and status_code in FALLBACK_STATUS_CODES
                    )

                    # For example, a 404 may mean that the primary
                    # model is unavailable. Try the fallback instead.
                    if can_use_fallback:
                        logger.warning(
                            "Primary Gemini model failed during %s "
                            "(model=%s, status=%s). "
                            "Trying fallback model=%s.",
                            operation,
                            model,
                            status_code,
                            FALLBACK_MODEL,
                        )
                        break

                    logger.exception(
                        "Non-retryable Gemini failure during %s "
                        "using model=%s.",
                        operation,
                        model,
                    )

                    raise GeminiServiceError(
                        "The analytics service could not complete "
                        "that request."
                    ) from error

                is_last_attempt = attempt == MAX_ATTEMPTS - 1

                if is_last_attempt:
                    logger.warning(
                        "Gemini retries exhausted during %s "
                        "using model=%s.",
                        operation,
                        model,
                    )
                    break

                delay = _backoff_delay(attempt)

                logger.warning(
                    "Temporary Gemini failure during %s "
                    "(model=%s, status=%s, retry=%s/%s, delay=%.2fs).",
                    operation,
                    model,
                    status_code,
                    attempt + 1,
                    MAX_ATTEMPTS - 1,
                    delay,
                )

                time.sleep(delay)

        # If the primary model failed with a fallback-eligible error,
        # continue the outer loop and try the fallback model.
        can_use_fallback = (
            model_index == 0
            and len(models) > 1
            and (
                _status_code(last_error) in FALLBACK_STATUS_CODES
                or (
                    last_error is not None
                    and _is_transient_network_error(last_error)
                )
            )
        )

        if can_use_fallback:
            logger.warning(
                "Primary Gemini model exhausted during %s. "
                "Trying fallback model=%s.",
                operation,
                FALLBACK_MODEL,
            )
            continue

    logger.error(
        "Gemini unavailable after retries during %s.",
        operation,
    )

    raise GeminiUnavailableError(
        "The analytics service is temporarily unavailable. "
        "Please try again in a moment."
    ) from last_error


# -----------------------------------------------------------------------------
# Prompt construction
# -----------------------------------------------------------------------------
BUSINESS_RULES = """
Business semantics:

- Revenue means:
  SUM(order_items.quantity * order_items.unit_price)

- Exclude cancelled orders from revenue and sales calculations
  unless the user explicitly asks to include cancelled orders.

- Treat order statuses case-insensitively.
  For example, 'Cancelled', 'cancelled', and 'CANCELLED'
  refer to the same status.

- Use these canonical relationships:
  - order_items.order_id = orders.order_id
  - order_items.product_id = products.product_id
  - orders.customer_id = customers.customer_id
  - customers.region_id = regions.region_id

- When joining orders to order_items, count DISTINCT orders.order_id
  when the user asks for number of orders.

- Do not calculate revenue by summing unit_price alone.
  Always multiply quantity by unit_price.

- Interpret:
  - 'revenue' and 'sales' as monetary value
  - 'orders' as order count
  - 'customers' as customer count or customer records,
    depending on the request
  - 'products' as product records or product sales,
    depending on the request

- Use order_date for order-time analysis.
- Use signup_date for customer acquisition analysis.
"""
def build_prompt(
    request: str,
    schema: str | dict,
    history: list,
    allow_clarification: bool,
) -> str:
    """
    Build the prompt used to convert a natural-language request into SQL.
    """

    if isinstance(schema, str):
        schema_text = schema
    else:
        schema_text = "\n".join(
        f"- {table}({', '.join(columns)})"
        for table, columns in schema.items()
    )

    history_text = "\n".join(
        f'- User asked: "{item["request"]}" -> SQL: {item["sql"]}'
        for item in history
    ) or "(none)"

    if allow_clarification:
        clarification_rule = (
            "If something essential is missing and you cannot reasonably "
            "infer it, such as the metric, time range, or entity, ask ONE "
            "short clarifying question. Only ask when reasonable people "
            "would interpret the request differently. Do not ask about "
            "things with a sensible default."
        )
    else:
        clarification_rule = (
            "Do NOT ask for clarification. Make the most reasonable "
            "assumption and write the SQL."
        )

    return f"""
You are a PostgreSQL query assistant.

The user's request may be casual and may mix languages, for example
Hinglish or Tanglish. Interpret the meaning, not only the exact words.

Schema:
{schema_text}
Business rules:
{BUSINESS_RULES}

Recent conversation:
{history_text}

Rules:
- Only generate one SELECT statement.
- Never generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, or other writes.
- Use only the tables and columns listed above.
- If the request refers to earlier results, modify the most recent SQL.
- {clarification_rule}
- Treat observed values as the source of truth.
- Do not assume the user's casing matches the database casing.
- For text filters, prefer case-insensitive comparisons such as:
  LOWER(column) = LOWER('value')
  or ILIKE.
- Use the observed database values when choosing status, category,
  and region filters.
- Do not invent values that are not present in the schema context.

Respond with JSON only, using exactly one of these shapes:

{{"status": "ready", "sql": "<the SELECT query>"}}

{{"status": "needs_clarification", "question": "<one short question>"}}

Request:
{request}
"""


# -----------------------------------------------------------------------------
# Natural language to SQL
# -----------------------------------------------------------------------------

def generate_structured(
    request: str,
    schema: dict,
    history: list,
    allow_clarification: bool,
) -> dict:
    """
    Generate and validate Gemini's structured JSON response.

    This function does not execute SQL.
    """

    prompt = build_prompt(
        request=request,
        schema=schema,
        history=history,
        allow_clarification=allow_clarification,
    )

    response = call_gemini(
        operation="structured SQL generation",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    raw_response = getattr(response, "text", None)

    if not isinstance(raw_response, str) or not raw_response.strip():
        raise GeminiServiceError(
            "The analytics service returned an empty response. "
            "Please try again."
        )

    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError as error:
        logger.exception("Gemini returned invalid JSON.")

        raise GeminiServiceError(
            "The analytics service returned an invalid response. "
            "Please try again."
        ) from error

    if not isinstance(result, dict):
        raise GeminiServiceError(
            "The analytics service returned an invalid response. "
            "Please try again."
        )

    response_status = result.get("status")

    if response_status not in {
        "ready",
        "needs_clarification",
    }:
        raise GeminiServiceError(
            "The analytics service returned an unexpected response. "
            "Please try again."
        )

    if response_status == "ready":
        if not isinstance(result.get("sql"), str):
            raise GeminiServiceError(
                "The analytics service returned invalid SQL data. "
                "Please try again."
            )

    if response_status == "needs_clarification":
        if not isinstance(result.get("question"), str):
            raise GeminiServiceError(
                "The analytics service returned an invalid clarification. "
                "Please try again."
            )

    return result


# -----------------------------------------------------------------------------
# SQL validation and execution
# -----------------------------------------------------------------------------
WRITE_OPERATION_KEYS = {
    "insert",
    "update",
    "delete",
    "merge",
    "drop",
    "alter",
    "create",
    "truncate",
    "grant",
    "revoke",
}

ALLOWED_TABLES = {
    "regions",
    "customers",
    "products",
    "orders",
    "order_items",
}

ALLOWED_SCHEMAS = {
    "",
    "public",
}

DANGEROUS_FUNCTION_NAMES = {
    # Delays and resource exhaustion
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",

    # Runtime/configuration mutation or inspection
    "set_config",
    "current_setting",

    # File-system access
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "pg_logdir_ls",

    # External connections
    "dblink_connect",
    "dblink_connect_u",
    "dblink_exec",
    "dblink_send_query",

    # Backend/process control
    "pg_cancel_backend",
    "pg_terminate_backend",

    # Locks and notifications
    "pg_advisory_lock",
    "pg_advisory_xact_lock",
    "pg_advisory_unlock",
    "pg_advisory_unlock_all",
    "pg_notify",

    # Sequence mutation
    "nextval",
    "setval",
}

def validate_sql(sql: str) -> tuple[bool, str]:
    """
    Validate that SQL is:
    - non-empty
    - exactly one statement
    - rooted at SELECT
    - free from nested write operations
    - restricted to approved tables and schemas
    """

    if not isinstance(sql, str) or not sql.strip():
        return False, "SQL query is empty."

    try:
        statements = sqlglot.parse(
            sql,
            read="postgres",
        )
    except Exception as error:
        return False, f"Failed to parse SQL: {error}"

    if not statements:
        return False, "No valid SQL statement found."

    if len(statements) != 1:
        return False, "Only one SQL statement is allowed."

    statement = statements[0]

    if statement.key.lower() != "select":
        return (
            False,
            f"Rejected non-SELECT statement: {statement.key}",
        )

    write_operation_keys = {
        "insert",
        "update",
        "delete",
        "merge",
        "drop",
        "alter",
        "create",
        "truncate",
        "grant",
        "revoke",
    }

    for node in statement.walk():
        node_key = node.key.lower()

        if node_key in write_operation_keys:
            return (
                False,
                f"Rejected write operation inside query: {node_key}",
            )

    # CTE names are temporary query aliases, not real database tables.
    cte_names = {
        cte.alias_or_name.lower()
        for cte in statement.find_all(exp.CTE)
    }

    # Reject dangerous functions even when they appear inside
    # an otherwise valid SELECT statement.
    for node in statement.find_all(exp.Func):
        function_name = getattr(node, "name", None)

        if not function_name:
            function_name = node.sql_name()

        function_name = function_name.lower()

        if function_name in DANGEROUS_FUNCTION_NAMES:
            return (
                False,
                f"Rejected dangerous function: {function_name}",
            )

    for table in statement.find_all(exp.Table):
        table_name = table.name.lower()
        schema_name = (table.db or "").lower()
        catalog_name = (table.catalog or "").lower()

        # Ignore references to local CTEs.
        if table_name in cte_names:
            continue

        if catalog_name:
            return (
                False,
                f"Cross-database table references are not allowed: "
                f"{table.catalog}.{table.db}.{table.name}",
            )

        if schema_name not in ALLOWED_SCHEMAS:
            return (
                False,
                f"Schema is not allowed: {schema_name}",
            )

        if table_name not in ALLOWED_TABLES:
            return (
                False,
                f"Table is not allowed: {table_name}",
            )

    return True, "OK"

def execute_sql(sql: str):
    """
    Execute a validated query with a hard server-side row limit.

    The generated SQL is wrapped as a subquery so the limit applies
    even when Gemini forgets to include LIMIT itself.
    """

    clean_sql = sql.strip().rstrip(";").strip()

    limited_sql = f"""
        SELECT *
        FROM ({clean_sql}) AS voxql_limited_result
        LIMIT :max_rows
    """

    with engine.connect() as connection:
        result = connection.execute(
            text(limited_sql),
            {
                "max_rows": MAX_RESULT_ROWS,
            },
        )

        return [
            dict(row._mapping)
            for row in result
        ]


# -----------------------------------------------------------------------------
# Voice transcription
# -----------------------------------------------------------------------------

def transcribe_audio(
    audio_bytes: bytes,
    mime_type: str = "audio/webm",
) -> str:
    """
    Transcribe audio through the same reliable Gemini wrapper.
    """

    response = call_gemini(
        operation="audio transcription",
        contents=[
            {
                "inline_data": {
                    "mime_type": mime_type,
                    "data": audio_bytes,
                }
            },
            (
                "Transcribe this audio exactly as spoken. "
                "If the speaker mixes languages, such as Hindi-English "
                "or Tamil-English, transcribe it as-is without translating. "
                "Output only the transcription."
            ),
        ],
    )

    raw_transcript = getattr(response, "text", None)

    if (
        not isinstance(raw_transcript, str)
        or not raw_transcript.strip()
    ):
        raise GeminiServiceError(
            "I could not understand that recording. "
            "Please try again."
        )

    return raw_transcript.strip()