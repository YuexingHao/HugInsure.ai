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

import json
import os
from pathlib import Path
from typing import Any, Union

import anthropic
from anthropic import AsyncAnthropicFoundry
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# Azure AI Foundry endpoint and deployment.
ENDPOINT = os.environ.get(
    "FOUNDRY_ENDPOINT",
    "https://ai-mghassem9468ai243514660583.services.ai.azure.com/anthropic/",
)
# Deployment name on Foundry — usually matches the Anthropic model ID.
# Set HUG_MODEL=claude-opus-4-7 (or whatever your Opus deployment is named) to swap.
MODEL = os.environ.get("HUG_MODEL", "claude-haiku-4-5")

HERE = Path(__file__).resolve().parent
SYSTEM_PROMPT = (HERE / "system_prompt.md").read_text()

# Always rate with Haiku 4.5 — cheap, fast, sufficient for a 0-10 score.
RATER_MODEL = os.environ.get("HUG_RATER_MODEL", "claude-haiku-4-5")
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
client = AsyncAnthropicFoundry(
    api_key=os.environ.get("ANTHROPIC_API_KEY"),
    base_url=ENDPOINT,
)


class Turn(BaseModel):
    role: str
    # str (plain text) OR list of Anthropic content blocks (text + image for multimodal)
    content: Union[str, list[dict[str, Any]]]


class ChatRequest(BaseModel):
    messages: list[Turn]


@app.get("/")
async def index():
    return FileResponse(HERE / "index.html")


async def rate_answer(question: str, answer: str) -> dict:
    """Score an answer 0-10 for factual risk via Haiku 4.5. Best-effort; falls back to mid."""
    try:
        resp = await client.messages.create(
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
async def chat(req: ChatRequest):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY not set on server")

    api_messages = [{"role": t.role, "content": t.content} for t in req.messages]
    question = _last_user(api_messages)

    async def event_stream():
        try:
            answer_text = ""
            async with client.messages.stream(
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
            yield f"data: {json.dumps({'error': 'invalid ANTHROPIC_API_KEY'})}\n\n"
        except anthropic.RateLimitError as e:
            yield f"data: {json.dumps({'error': f'rate limited: {e}'})}\n\n"
        except anthropic.APIStatusError as e:
            msg = f"API {e.status_code}: {getattr(e, 'message', str(e))}"
            yield f"data: {json.dumps({'error': msg})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': f'{type(e).__name__}: {e}'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    print(f"\nhug.  model={MODEL}  endpoint={ENDPOINT}")
    print("→ http://127.0.0.1:8000\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
