sessions={}
def get_session(session_id: str)-> dict:
    if session_id not in sessions:
        sessions[session_id] = {
            "history": [],
            "pending": None,
            "clarify_count": 0,
        }
    return sessions[session_id]