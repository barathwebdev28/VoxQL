# VoxQL

VoxQL is a conversational analytics interface for PostgreSQL. It lets users ask questions about sales data in plain language or by voice, translates those requests into safe SQL, executes them against a read-only database, and presents the result in a clear chat experience.

It is designed for people who need answers from operational data without having to write SQL.

## Project objective

The objective of VoxQL is to make database exploration feel as natural as asking a colleague a question:

> “Which region generated the most revenue last quarter?”

VoxQL interprets the request, asks for clarification when necessary, generates a PostgreSQL query grounded in the live schema, validates the query before execution, and returns a concise answer with transparent SQL and results.

The system supports typed and spoken requests, conversational follow-ups, mixed-language input such as Hinglish and Tanglish, and safe failure handling when an external model or database is unavailable.

## What is included

- Natural-language text-to-SQL queries
- Browser-based voice queries
- Native Gemini audio transcription
- Schema-aware SQL generation
- Conversational clarification and follow-up questions
- Session-scoped conversation memory
- SQL AST validation
- Nested write-operation detection
- Single-statement enforcement
- Approved-table and schema allowlisting
- Dangerous-function filtering
- Read-only database execution
- Query row limits and statement timeouts
- Gemini retries with exponential backoff and jitter
- Optional verified Gemini fallback model
- Fast failure on rate limits
- Safe, user-friendly provider error messages
- Off-topic request redirection
- Expandable generated SQL
- Results rendered as responsive tables
- Starter prompts for common analytics questions
- Dark, minimalist analytics interface

## Architecture

```text
User
  │
  ├── Typed request ─────────────┐
  └── Voice recording             │
                                  ▼
                         Next.js frontend
                                  │
                                  ▼
                            FastAPI backend
                                  │
                 ┌────────────────┴────────────────┐
                 ▼                                 ▼
       Gemini transcription                 Gemini SQL generation
                 │                                 │
                 └────────────────┬────────────────┘
                                  ▼
                         SQL safety pipeline
                                  │
                                  ▼
                   Read-only PostgreSQL execution
                                  │
                                  ▼
                         Results and explanation
```

## Technology stack

### Frontend

- Next.js 16
- React 19
- TypeScript
- Tailwind CSS
- Browser MediaRecorder API

### Backend

- Python
- FastAPI
- SQLAlchemy
- PostgreSQL
- `sqlglot`
- Google Gemini API
- `psycopg2-binary`

### Database

VoxQL uses a Neon PostgreSQL database with the following commerce schema:

| Table | Purpose |
| --- | --- |
| `regions` | Geographic sales regions |
| `customers` | Customer identities and signup dates |
| `products` | Products, categories, and unit prices |
| `orders` | Order dates, customers, and statuses |
| `order_items` | Products, quantities, and prices within orders |

The demo dataset contains:

- 4 regions
- 50 customers
- 20 products
- 300 orders
- Multiple order items per order

## Request lifecycle

### Text query

1. The user submits a natural-language question.
2. The frontend creates or reuses a browser session ID.
3. The backend reads the reflected database schema.
4. Gemini generates either:
   - a single SQL query, or
   - one clarification question.
5. The SQL safety pipeline parses and validates the query.
6. The query runs inside a read-only database context.
7. VoxQL returns the SQL, rows, summary, and execution metadata.

### Voice query

1. The browser records audio locally.
2. The user explicitly chooses **Done**, then **Send** or **Cancel**.
3. The backend sends the recording to Gemini for transcription.
4. The transcript enters the same conversational SQL pipeline as typed input.
5. The transcript, query, and results appear in the chat.

## Conversational behavior

VoxQL maintains lightweight session memory for follow-up requests such as:

```text
User: Show total revenue by region.
VoxQL: Which time range should I use?
User: This year.
User: Now break that down by product category.
```

The backend stores recent requests and generated SQL for the active session. Clarification is limited so the assistant does not repeatedly ask questions instead of making reasonable assumptions.

## SQL safety model

VoxQL treats generated SQL as untrusted input.

Before execution, it enforces:

- exactly one SQL statement
- a root `SELECT` statement
- no nested `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `GRANT`, or `REVOKE` operations
- no data-modifying CTEs
- access only to approved analytics tables
- access only to approved schemas
- rejection of dangerous PostgreSQL functions
- server-side result limits
- statement execution timeouts

The database connection also uses read-only permissions. AST validation provides application-level protection, while PostgreSQL permissions provide database-level enforcement.

## Gemini reliability

All Gemini calls go through a shared reliability layer.

- SDK-level retries are disabled so retry behavior remains predictable.
- Temporary failures such as `408`, `500`, `502`, `503`, and `504` use bounded exponential backoff with jitter.
- `429` rate-limit responses fail fast instead of being retried repeatedly.
- A fallback model can be configured only after it has been verified for the active API key.
- Invalid JSON, empty model responses, and empty transcripts become safe application errors.
- Raw provider errors are never shown directly in the user interface.

## Environment configuration

Create `backend/.env` locally:

```env
DATABASE_URL=your_postgresql_connection_string
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-3.6-flash
GEMINI_FALLBACK_MODEL=
```

Optional frontend configuration:

```env
NEXT_PUBLIC_API_URL=http://localhost:8000
```

Never commit real `.env` files or credentials. Use an `.env.example` file for shareable configuration templates.

## Local development

### Start the backend

From the `backend` directory:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload
```

The API runs at:

```text
http://localhost:8000
```

### Start the frontend

From the `frontend` directory:

```powershell
npm install
npm run dev
```

The web application runs at:

```text
http://localhost:3000
```

## API endpoints

### `GET /schema`

Returns the reflected database schema used to ground SQL generation.

### `POST /query`

Accepts a typed request:

```json
{
  "text": "Show total revenue by region",
  "session_id": "browser-session-id"
}
```

### `POST /voice-query`

Accepts a multipart form request containing:

- `file`: recorded audio
- `session_id`: browser session identifier

### `POST /echo`

Simple connectivity endpoint used during development.

## Example questions

- “Show total revenue by region.”
- “Which products sell the most?”
- “How many completed orders were placed this month?”
- “Break that down by category.”
- “North region-la total sales evlo?”
- “West region mein kitne completed orders hain?”

## Testing

Backend safety tests can be run from the `backend` directory:

```powershell
.\venv\Scripts\python.exe test_safety.py
```

The safety suite covers:

- valid `SELECT` queries
- joins and aggregations
- CTEs
- multiple statements
- destructive statements
- nested write operations
- unknown tables
- system schemas

Frontend checks:

```powershell
npm run lint
npm run build
```

Manual end-to-end verification should cover:

1. A normal typed analytics request.
2. An ambiguous request followed by a clarification answer.
3. A conversational follow-up.
4. A voice query.
5. A microphone permission denial.
6. A Gemini outage or rate-limit response.
7. A destructive SQL request.
8. An off-topic request.

## Product principles

- Plain language first; SQL remains inspectable.
- Safe defaults over clever guesses.
- Read-only by design.
- Graceful failure instead of provider error leakage.
- Voice input should always be reversible before submission.
- Every answer should be grounded in the connected database.

## Project status

VoxQL is a completed MVP for conversational, voice-enabled sales analytics over PostgreSQL. The architecture is intentionally modular so future iterations can add richer explanations, charts, exports, persistent sessions, authentication, and improved multilingual ambiguity handling without changing the core safety pipeline.
