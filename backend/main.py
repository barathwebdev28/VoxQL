import os
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from db import get_schema_context ,get_schema_summary
from query_engine import generate_structured, validate_sql, execute_sql, transcribe_audio
from sessions import get_session

app = FastAPI()
FRONTEND_ORIGIN = os.getenv(
    "FRONTEND_ORIGIN",
    "http://localhost:3000",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
)



class Message(BaseModel):
    text: str
    session_id: str = "default"


def run_pipeline(text: str, session_id: str, transcript: str | None = None) -> dict:
    session = get_session(session_id)
    schema = get_schema_context()
    extra = {"transcript": transcript} if transcript else {}

    # If we asked a question last turn, this message is the answer: merge it back in.
    pending = session["pending"]
    if pending:
        effective = (
            f'{pending["original"]}\n'
            f'Clarifying question asked: "{pending["question"]}"\n'
            f'User answered: "{text}"'
        )
    else:
        effective = text

    try:
        result = generate_structured(
            effective, schema, session["history"],
            allow_clarification=session["clarify_count"] < 2,
        )
    except Exception as e:
        return {"status": "error", "error": str(e), **extra}

    if result.get("status") == "needs_clarification":
        session["pending"] = {"original": effective, "question": result["question"]}
        session["clarify_count"] += 1
        return {"status": "ambiguous", "question": result["question"], **extra}

    sql = result.get("sql", "")
    session["pending"] = None
    session["clarify_count"] = 0

    is_safe, reason = validate_sql(sql)
    if not is_safe:
        return {"status": "blocked", "sql": sql, "reason": reason, **extra}

    try:
        rows = execute_sql(sql)
    except Exception as e:
        return {"status": "error", "sql": sql, "error": str(e), **extra}

    session["history"].append({"request": effective, "sql": sql})
    session["history"] = session["history"][-5:]
    return {"status": "success", "sql": sql, "results": rows, **extra}


@app.post("/echo")
def echo(msg: Message):
    return {"received": msg.text}

@app.get("/schema")
def schema():
    return get_schema_summary()

@app.post("/query")
def query(msg: Message):
    return run_pipeline(msg.text, msg.session_id)

@app.post("/voice-query")
async def voice_query(file: UploadFile = File(...), session_id: str = Form("default")):
    audio_bytes = await file.read()
    try:
        transcript = transcribe_audio(audio_bytes, mime_type=file.content_type)
    except Exception as e:
        return {"status": "error", "error": str(e)}
    return run_pipeline(transcript, session_id, transcript=transcript)