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

app = FastAPI()
client = AsyncAnthropicFoundry(
    api_key=os.environ.get("ANTHROPIC_API_KEY"),
    base_url=ENDPOINT,
)


class Turn(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Turn]


@app.get("/")
async def index():
    return FileResponse(HERE / "index.html")


@app.post("/chat")
async def chat(req: ChatRequest):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY not set on server")

    api_messages = [{"role": t.role, "content": t.content} for t in req.messages]

    async def event_stream():
        try:
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
                async for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"

                final = await stream.get_final_message()
                usage = {
                    "input": final.usage.input_tokens,
                    "output": final.usage.output_tokens,
                    "cache_read": final.usage.cache_read_input_tokens,
                    "cache_write": final.usage.cache_creation_input_tokens,
                }
                print(f"[hug] model={MODEL} usage={usage}", flush=True)
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
