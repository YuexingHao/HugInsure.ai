"""HugInsure backend — proxies user messages to Claude with prompt caching.
Calls Anthropic models hosted on Azure AI Foundry via AsyncAnthropicFoundry.

Run:
    export ANTHROPIC_API_KEY=<your-foundry-key>
    ./run.sh

Or manually:
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python server.py

Then open http://127.0.0.1:8000

Configuration via env vars:
    ANTHROPIC_API_KEY   REQUIRED — your Azure Foundry key
    FOUNDRY_ENDPOINT    optional — defaults to the endpoint baked in below
    HUG_MODEL           optional — Foundry deployment name, default claude-haiku-4-5
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import anthropic
from anthropic import AsyncAnthropicFoundry
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Azure AI Foundry endpoint and deployment.
ENDPOINT = os.environ.get(
    "FOUNDRY_ENDPOINT",
    os.environ.get(
        "AZURE_FOUNDRY_ENDPOINT",
        "https://ai-mghassem9468ai243514660583.services.ai.azure.com/anthropic/",
    ),
)
# Deployment name on Foundry — usually matches the Anthropic model ID.
# Set HUG_MODEL=claude-opus-4-7 (or whatever your Opus deployment is named) to swap.
MODEL = os.environ.get("HUG_MODEL", "claude-haiku-4-5")

HERE = Path(__file__).resolve().parent
SYSTEM_PROMPT = (HERE / "system_prompt.md").read_text()

# Always rate with Haiku 4.5 — cheap, fast, sufficient for a 0-10 score.
RATER_MODEL = os.environ.get("HUG_RATER_MODEL", "claude-haiku-4-5")
SUGGEST_MODEL = os.environ.get("HUG_SUGGEST_MODEL", "claude-haiku-4-5")
SUGGEST_SYSTEM = """You are a fact-checker reviewing an AI assistant's reply for potential errors. \
A user is considering filing a claim that this reply was wrong. Your job: produce a corrected \
version of the target reply, fixing factual, logical, or significant errors so the user can \
accept your suggestion or modify it.

Guidelines:
- If the target reply has clear factual or logical errors, fix them. Keep the same general \
  structure, length, and tone; just replace the wrong claims with correct ones.
- If the target reply is already correct, return it unchanged.
- Be calibrated: don't manufacture errors that aren't there. Don't reword for style.
- If you're uncertain, prefer the original over speculative changes.

Output ONLY the corrected message text. No quotation marks, no preamble like "Here's the \
correction:", no commentary, no markdown, no HTML. Just the text the user should see in \
their corrected version."""


VERIFIER_MODEL = os.environ.get("HUG_VERIFIER_MODEL", "claude-haiku-4-5")
VERIFIER_SYSTEM = """You are an impartial verifier of error claims against AI assistants. \
A user has filed a claim that an AI got something wrong. You will see four things:

  CONVERSATION: the full back-and-forth between the user and the AI.
  CLAIMED ERROR: what the user says was wrong about the AI's answer.
  PROPOSED CORRECTION: what the user says the actual correct answer is.

Determine whether the claim has merit:
  - "valid"     means the AI made a real, substantial error AND the user's correction is essentially right.
  - "invalid"   means the AI was actually correct (claim doesn't hold), OR the user's proposed correction is itself wrong.
  - "uncertain" means you genuinely cannot tell from what was provided (insufficient context, subjective domain, etc.).

Be calibrated and skeptical. Don't reward nitpicks (typos, formatting, stylistic preferences) — \
the AI must have made a real factual or logical error to merit a payout. Don't reward claims \
where the user just disagrees with a correctly-hedged answer.

Output ONLY a single line of JSON, no prose, no code fences, no markdown:
{"verdict": "valid"|"invalid"|"uncertain", "confidence": 0.0-1.0, "reasoning": "<1-3 sentences>"}"""


RATER_SYSTEM = """You evaluate how high-stakes a user's QUESTION is — i.e., how much careful \
attention it deserves in a response. You are NOT rating the answer's correctness; you are rating \
how consequential it is to get this question right.

Score 0-10 (integer):
  0-2  Trivial or playful — small consequence if mishandled.
  3-4  Common-knowledge factual — low real-world consequence.
  5-6  Specific or domain-bound — meaningful consequence if mishandled.
  7-8  Specialized, time-sensitive, or affects real decisions.
  9-10 High-stakes: medical, legal, financial, safety-critical decisions.

If the question is about taking medication, treatment, regulated activity, money decisions, \
or anything irreversible: lean 7+. If a child's homework or a trivia bet: lean 0-3.

Respond with ONLY a valid JSON object on a single line, no prose, no code fences:
{"score": <integer 0-10>, "reason": "<one short clause, <=14 words, why these stakes>"}"""

app = FastAPI()

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "ALLOWED_ORIGINS",
        "https://yuexinghao.github.io,http://127.0.0.1:8000,http://localhost:8000",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

client: Optional[AsyncAnthropicFoundry] = None


def foundry_api_key() -> Optional[str]:
    return (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_FOUNDRY_API_KEY")
        or os.environ.get("AZURE_FOUNDRY_API_KEY")
    )


def get_client() -> AsyncAnthropicFoundry:
    global client
    if client is None:
        client = AsyncAnthropicFoundry(
            api_key=foundry_api_key(),
            base_url=ENDPOINT,
        )
    return client

# ---------- Dataset capture ----------------------------------------------------
# Every interaction (server-driven and client-driven) lands as one JSON object
# per line in data/events.jsonl. JSONL is append-only, streamable, and consumed
# downstream with `pandas.read_json("data/events.jsonl", lines=True)`.

DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)
EVENTS_FILE = DATA_DIR / "events.jsonl"
_events_lock = asyncio.Lock()


async def log_event(
    event_type: str,
    *,
    page: Optional[str] = None,
    session_id: Optional[str] = None,
    payload: Optional[dict] = None,
    request: Optional[Request] = None,
    client_timestamp: Optional[str] = None,
) -> dict:
    """Append one JSONL record to data/events.jsonl. Best-effort; never raises."""
    record: dict[str, Any] = {
        "event_id":   str(_uuid.uuid4()),
        "session_id": session_id,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "page":       page,
        "payload":    payload or {},
    }
    if client_timestamp:
        record["client_timestamp"] = client_timestamp
    if request is not None:
        record["ip"] = request.client.host if request.client else None
        record["user_agent"] = request.headers.get("user-agent")
    try:
        async with _events_lock:
            with EVENTS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"[hug] log_event write error: {type(e).__name__}: {e}", flush=True)
    return record


class Turn(BaseModel):
    role: str
    # str (plain text) OR list of Anthropic content blocks (text + image for multimodal)
    content: Union[str, list[dict[str, Any]]]


class ChatRequest(BaseModel):
    messages: list[Turn]
    session_id: Optional[str] = None


class VerifyRequest(BaseModel):
    conversation: str
    claimed_error: str
    correct_answer: str
    session_id: Optional[str] = None


class SuggestRequest(BaseModel):
    conversation: str
    target_message: str
    session_id: Optional[str] = None


class EventIn(BaseModel):
    """Client-emitted interaction event. Fully open payload to keep the schema flexible."""
    event_type: str
    page: Optional[str] = None
    session_id: Optional[str] = None
    timestamp: Optional[str] = None  # client-side wall clock; server still stamps its own
    payload: dict[str, Any] = Field(default_factory=dict)


async def rate_answer(question: str, answer: str) -> dict:
    """Score an answer 0-10 for factual risk via Haiku 4.5. Best-effort; falls back to mid."""
    try:
        resp = await get_client().messages.create(
            model=RATER_MODEL,
            max_tokens=200,
            system=RATER_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"QUESTION:\n{question}\n\nANSWER:\n{answer}\n\nReturn the JSON now.",
            }],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        # Tolerate stray prose around the JSON: find the first {...} block.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start : end + 1])
            score = float(data.get("score", 5))
            reason = str(data.get("reason", "")).strip()[:140]
            return {"score": max(0.0, min(10.0, score)), "reason": reason}
    except Exception as e:
        print(f"[hug] rate error: {type(e).__name__}: {e}", flush=True)
    return {"score": 5.0, "reason": "rating unavailable."}


def _last_user(messages: list[dict]) -> str:
    """Pull the latest user turn as plain text — handles both string and content-block forms."""
    for m in reversed(messages):
        if m["role"] != "user":
            continue
        c = m["content"]
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
            has_image = any(isinstance(b, dict) and b.get("type") == "image" for b in c)
            text = " ".join(p for p in parts if p).strip()
            if has_image and not text:
                return "(image attached, no text)"
            if has_image:
                return f"{text} (with attached image)"
            return text
    return ""


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    if not foundry_api_key():
        raise HTTPException(500, "Foundry API key not set on server")

    api_messages = [{"role": t.role, "content": t.content} for t in req.messages]
    question = _last_user(api_messages)

    # Log the incoming request immediately so we capture the user's prompt even
    # if the stream errors mid-flight.
    await log_event(
        "chat_request",
        page="chat.html",
        session_id=req.session_id,
        payload={
            "messages": api_messages,
            "last_user_text": question,
            "model": MODEL,
        },
        request=request,
    )

    async def event_stream():
        answer_text = ""
        usage: dict[str, Any] = {}
        rating: Optional[dict] = None
        error: Optional[str] = None
        try:
            async with get_client().messages.stream(
                model=MODEL,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=api_messages,
            ) as stream:
                async for chunk in stream.text_stream:
                    answer_text += chunk
                    yield f"data: {json.dumps({'text': chunk})}\n\n"

                final = await stream.get_final_message()
                usage = {
                    "input": final.usage.input_tokens,
                    "output": final.usage.output_tokens,
                    "cache_read": final.usage.cache_read_input_tokens,
                    "cache_write": final.usage.cache_creation_input_tokens,
                }
                print(f"[hug] model={MODEL} usage={usage}", flush=True)

            # Second-pass rating with Haiku 4.5
            rating = await rate_answer(question, answer_text)
            print(f"[hug] rating={rating}", flush=True)
            yield f"data: {json.dumps({'score': rating['score'], 'reason': rating['reason']})}\n\n"

            yield f"data: {json.dumps({'done': True, 'usage': usage})}\n\n"

        except anthropic.AuthenticationError:
            error = "invalid Foundry API key"
            yield f"data: {json.dumps({'error': error})}\n\n"
        except anthropic.RateLimitError as e:
            error = f"rate limited: {e}"
            yield f"data: {json.dumps({'error': error})}\n\n"
        except anthropic.APIStatusError as e:
            error = f"API {e.status_code}: {getattr(e, 'message', str(e))}"
            yield f"data: {json.dumps({'error': error})}\n\n"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            yield f"data: {json.dumps({'error': error})}\n\n"
        finally:
            # Always log the response (success or failure) so the dataset stays paired
            # with chat_request records.
            await log_event(
                "chat_response",
                page="chat.html",
                session_id=req.session_id,
                payload={
                    "answer": answer_text,
                    "rating": rating,
                    "usage": usage,
                    "model": MODEL,
                    "error": error,
                },
                request=request,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/verify_claim")
async def verify_claim(req: VerifyRequest, request: Request):
    """LLM-as-grader: judges whether the user's claim has merit."""
    if not foundry_api_key():
        raise HTTPException(500, "Foundry API key not set on server")

    user_content = (
        f"CONVERSATION:\n{req.conversation}\n\n"
        f"CLAIMED ERROR:\n{req.claimed_error}\n\n"
        f"PROPOSED CORRECTION:\n{req.correct_answer}\n\n"
        "Return your JSON verdict now."
    )

    fallback = {"verdict": "uncertain", "confidence": 0.0, "reasoning": "verifier produced no parseable verdict."}
    result: dict = fallback
    error: Optional[str] = None
    try:
        resp = await get_client().messages.create(
            model=VERIFIER_MODEL,
            max_tokens=400,
            system=VERIFIER_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start : end + 1])
            verdict = data.get("verdict", "uncertain")
            if verdict not in ("valid", "invalid", "uncertain"):
                verdict = "uncertain"
            confidence = float(data.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
            reasoning = str(data.get("reasoning", "")).strip()[:600] or "no reasoning provided."
            result = {"verdict": verdict, "confidence": confidence, "reasoning": reasoning}
            print(f"[hug] verify {result}", flush=True)
    except anthropic.AuthenticationError:
        error = "invalid Foundry API key"
    except anthropic.APIStatusError as e:
        error = f"verifier API error {e.status_code}"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"[hug] verify error: {error}", flush=True)

    await log_event(
        "verdict_returned",
        page="claim.html",
        session_id=req.session_id,
        payload={
            "conversation": req.conversation,
            "claimed_error": req.claimed_error,
            "correct_answer": req.correct_answer,
            "verdict":    result["verdict"],
            "confidence": result["confidence"],
            "reasoning":  result["reasoning"],
            "model":      VERIFIER_MODEL,
            "error":      error,
        },
        request=request,
    )

    if error == "invalid Foundry API key":
        raise HTTPException(401, error)
    if error and error.startswith("verifier API error"):
        raise HTTPException(502, error)
    return result


@app.post("/suggest_edit")
async def suggest_edit(req: SuggestRequest, request: Request):
    """Haiku 4.5 suggests a corrected version of one assistant message."""
    if not foundry_api_key():
        raise HTTPException(500, "Foundry API key not set on server")

    user_content = (
        f"CONVERSATION:\n{req.conversation}\n\n"
        f"TARGET REPLY TO REVIEW AND CORRECT:\n{req.target_message}\n\n"
        "Output only the corrected reply text now."
    )

    suggested = req.target_message
    error: Optional[str] = None
    try:
        resp = await get_client().messages.create(
            model=SUGGEST_MODEL,
            max_tokens=600,
            system=SUGGEST_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        # Strip surrounding quotes the model might add despite instructions
        if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
            text = text[1:-1].strip()
        print(f"[hug] suggest produced {len(text)} chars", flush=True)
        suggested = text or req.target_message
    except anthropic.AuthenticationError:
        error = "invalid Foundry API key"
    except anthropic.APIStatusError as e:
        error = f"suggest API error {e.status_code}"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"[hug] suggest error: {error}", flush=True)

    await log_event(
        "suggest_returned",
        page="claim.html",
        session_id=req.session_id,
        payload={
            "conversation":   req.conversation,
            "target_message": req.target_message,
            "suggested":      suggested,
            "model":          SUGGEST_MODEL,
            "error":          error,
        },
        request=request,
    )

    if error == "invalid Foundry API key":
        raise HTTPException(401, error)
    if error and error.startswith("suggest API error"):
        raise HTTPException(502, error)
    return {"suggested": suggested}


# ---------- Client-emitted events + dataset export ---------------------------

@app.post("/event")
async def event(evt: EventIn, request: Request):
    """Client-side interactions land here (page_view, edit_committed, vote_cast, etc.)."""
    rec = await log_event(
        evt.event_type,
        page=evt.page,
        session_id=evt.session_id,
        payload=evt.payload,
        request=request,
        client_timestamp=evt.timestamp,
    )
    return {"ok": True, "event_id": rec["event_id"]}


@app.get("/export")
async def export(token: Optional[str] = None):
    """Download the entire JSONL dataset.

    Set HUG_EXPORT_TOKEN in the environment to gate access. If unset, /export
    is open (intended for local-only dev).
    """
    expected = os.environ.get("HUG_EXPORT_TOKEN")
    if expected and token != expected:
        raise HTTPException(403, "missing or invalid token")
    if not EVENTS_FILE.exists():
        return Response(content="", media_type="application/x-ndjson")
    return FileResponse(
        EVENTS_FILE,
        media_type="application/x-ndjson",
        filename="events.jsonl",
    )


@app.get("/events_count")
async def events_count():
    """Quick health check — how many events have we captured?"""
    if not EVENTS_FILE.exists():
        return {"count": 0, "bytes": 0, "path": str(EVENTS_FILE)}
    n = 0
    with EVENTS_FILE.open("rb") as f:
        for _ in f:
            n += 1
    return {"count": n, "bytes": EVENTS_FILE.stat().st_size, "path": str(EVENTS_FILE)}


# Serve every static file in the project dir (HTML pages, data assets, etc.).
# Mounted last so the POST /chat route above takes priority over GET /chat.
app.mount("/", StaticFiles(directory=str(HERE), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    print(f"\nhug.  model={MODEL}  endpoint={ENDPOINT}")
    print("→ http://127.0.0.1:8000\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
