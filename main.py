import os
from datetime import datetime

import httpx
import json
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import get_db
from database.models import Conversation, Message
from pathlib import Path
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"


load_dotenv()

# RODIUMAI_URL = "https://api.rodiumai.io/v1/chat/completions"
# RODIUMAI_API_KEY = os.environ["RODIUMAI_API_KEY"]
RODIUMAI_API_KEY = os.getenv("RODIUMAI_API_KEY")
RODIUMAI_URL = os.getenv("RODIUMAI_URL")


MODEL = os.getenv("RODIUMAI_MODEL")
PREVIEW_LENGTH = 60

# Roles the LLM understands. Anything else stored in the DB (e.g. notifications) stays out of its prompt.
LLM_ROLES = {"user", "assistant"}
NOTIFICATION_ROLE = "system-notification"

NOTE_ROLE = "note"

NON_LLM_ROLES = {
    NOTIFICATION_ROLE,
    NOTE_ROLE,
}

NOTIFICATION_EVERY = 10  # a notification each time the dialogue reaches a multiple of this many messages
NOTIFICATION_TEXT = "Notification système : Une dizaine de messages écrits."

SYSTEM_PROMPT = (
    "Tu es Study Buddy, un tuteur bienveillant pour les étudiants."
    "Réponds aux questions de manière claire et concise."
)

ALLOWED_MODELS = [
    model.strip()
    for model in os.getenv("ALLOWED_MODELS", "").split(",")
    if model.strip()
]

if len(ALLOWED_MODELS) < 2:
    raise RuntimeError(
        "ALLOWED_MODELS doit contenir au moins deux modèles."
    )

if MODEL not in ALLOWED_MODELS:
    raise RuntimeError(
        "RODIUMAI_MODEL doit appartenir à ALLOWED_MODELS."
    )

app = FastAPI(title="Study Buddy Chatbot")


class ConversationResponse(BaseModel):
    conversation_id: int


class ConversationSummary(BaseModel):
    id: int
    created_at: datetime
    preview: str | None  # first user message, truncated; None while the conversation is empty


class ChatRequest(BaseModel):
    conversation_id: int
    message: str

class NoteRequest(BaseModel):
    content: str

class ChatResponse(BaseModel):
    reply: str
    notification: str | None = None  # set when this turn also stored a system-notification


class MessageResponse(BaseModel):
    seq: int
    role: str
    content: str
    model: str | None = None
    created_at: datetime

class ChatRequest(BaseModel):
    conversation_id: int  # Garde le type déjà utilisé dans ton projet.
    message: str
    model: str

def load_messages(db: Session, conversation_id: int) -> list[Message]:
    # A conversation's messages in order; 404 if the conversation doesn't exist.
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    ).all()


def build_llm_history(rows: list[Message]) -> list[dict]:
    # The DB history is not necessarily what the LLM sees: filter it here, just before building the request.
    # For now we only drop the roles the LLM doesn't know (system-notification).
    return [{"role": m.role, "content": m.content} for m in rows if m.role in LLM_ROLES and m.role not in NON_LLM_ROLES]


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


@app.post("/conversations", status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationResponse:
    conversation = Conversation()
    db.add(conversation)
    db.commit()
    return ConversationResponse(conversation_id=conversation.id)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    # Newest first, each joined to its first message (seq 1, always the user's) for the preview.
    rows = db.execute(
        select(Conversation, Message.content)
        .outerjoin(Message, and_(Message.conversation_id == Conversation.id, Message.seq == 1))
        .order_by(Conversation.id.desc())
    ).all()
    return [
        ConversationSummary(
            id=conversation.id,
            created_at=conversation.created_at,
            preview=content[:PREVIEW_LENGTH] if content else None,
        )
        for conversation, content in rows
    ]


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[MessageResponse]:
    return [
        MessageResponse(seq=m.seq, role=m.role, content=m.content, model=m.model, created_at=m.created_at)
        for m in load_messages(db, conversation_id)
    ]

@app.post("/conversations/{conversation_id}/notes",
    status_code=201,
    response_model=MessageResponse,
)
def create_note(conversation_id: int, req: NoteRequest, db: Session = Depends(get_db)) -> list[MessageResponse]:
    # Verify that the conversation exists and retrieve its messages.
    rows = load_messages(db, conversation_id)

    content = req.content.strip()
    if not content:
        raise HTTPException(
            status_code=422,
            detail="Note content cannot be empty.",
        )

    # Every message, including notes and notifications, has a unique sequence number in the conversation.
    next_seq = rows[-1].seq + 1 if rows else 1

    note = Message(
        conversation_id=conversation_id,
        seq=next_seq,
        role=NOTE_ROLE,
        content=content,
    )

    db.add(note)

    try:
        db.commit()
        db.refresh(note)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="The conversation was updated concurrently. Please retry.",
        ) from exc

    return MessageResponse(
        seq=note.seq,
        role=note.role,
        content=note.content,
        created_at=note.created_at,
    )


@app.post("/chat")
def chat(req: ChatRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    if req.model not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail="Modèle non autorisé.",
        )

    # Load this conversation from the database, in message order.
    rows = load_messages(db, req.conversation_id)
    history = build_llm_history(rows)

    content = req.message.strip()
    if not content:
        raise HTTPException(
            status_code=422,
            detail="The message cannot be empty.",
        )

    # seq follows every stored row (notifications included) so it stays unique.
    next_seq = rows[-1].seq + 1 if rows else 1
    user_message = {"role": "user", "content": content}

    # The LLM is stateless: resend the system prompt + the whole conversation each turn.
    # messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history, user_message]
    messages = [{"role": "system", "content": load_system_prompt()}, *history, user_message]

    def send_event(data: dict) -> str:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    def generate():
        parts = []
        completed = False
        chat_completion="/chat/completions"

        try:
            with httpx.stream(
                "POST",
                f"{RODIUMAI_URL.rstrip('/')}{chat_completion}",
                headers={
                    "Authorization": f"Bearer {RODIUMAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": req.model,
                    "messages": messages,
                    "max_tokens": 512,
                    "stream": True,
                },
                timeout=httpx.Timeout(60.0, connect=10.0),
            ) as response:
                response.raise_for_status()

                # Read flux SSE sended by RodiumAI.
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue

                    data = line[len("data:"):].strip()

                    if data == "[DONE]":
                        completed = True
                        break

                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    choices = payload.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    token = delta.get("content")

                    if isinstance(token, str) and token:
                        parts.append(token)
                        yield send_event({
                            "type": "token",
                            "content": token,
                        })

            # Closed connection without [DONE] is not a success.
            if not completed:
                db.rollback()
                yield send_event({
                    "type": "error",
                    "message": "Model stream closed before completion.",
                })
                return

            # Do not save anything until the generation process has completed normally. 
            reply = "".join(parts)

            # Only record the turn once the call succeeded, so a failure doesn't leave a dangling user message.
            db.add_all([
                Message(conversation_id=req.conversation_id, seq=next_seq, role="user", content=req.message),
                Message(conversation_id=req.conversation_id, seq=next_seq + 1, role="assistant", content=reply, model=req.model),
            ])

            # Count only the dialogue (user + assistant): counting notifications too would shift the total
            # off the multiples of 10 for good after the first one.
            notification = None
            if (len(history) + 2) % NOTIFICATION_EVERY == 0:
                notification = NOTIFICATION_TEXT
                db.add(Message(
                    conversation_id=req.conversation_id, seq=next_seq + 2, role=NOTIFICATION_ROLE, content=notification
                ))

            try:
                db.commit()
            except IntegrityError:
                # Another request already wrote these seq numbers in this conversation while we waited for the LLM.
                db.rollback()
                raise HTTPException(
                    status_code=409, detail="The conversation was updated concurrently, please retry."
                )

            # The notification is sent only after the data has been successfully saved.
            if notification:
                yield send_event({
                    "type": "notification",
                    "content": notification,
                })

            # Signal the success to the frontend after the commit.
            yield "data: [DONE]\n\n"

        except GeneratorExit:
            # The client has closed the stream.
            db.rollback()
            raise

        except Exception as exc:
            db.rollback()
            print(
                "Error while streaming RodiumAI:",
                type(exc).__name__,
                str(exc),
            )
            yield send_event({
                "type": "error",
                "message": (
                    "The generation failed. "
                    "The response was not recorded as complete."
                ),
            })

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

@app.get("/models")
def get_models():
    return {
        "models": ALLOWED_MODELS,
        "default": MODEL,
    }