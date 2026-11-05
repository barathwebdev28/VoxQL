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

def build_prompt(
    request: str,
    schema: dict,
    history: list,
    allow_clarification: bool,
) -> str:
    """
    Build the prompt used to convert a natural-language request into SQL.
    """

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

Recent conversation:
{history_text}

Rules:
- Only generate one SELECT statement.
- Never generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, or other writes.
- Use only the tables and columns listed above.
- If the request refers to earlier results, modify the most recent SQL.
- {clarification_rule}

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
    Execute validated SQL and convert rows into dictionaries.
    """

    with engine.connect() as connection:
        result = connection.execute(text(sql))
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