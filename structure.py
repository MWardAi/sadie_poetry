# app.py
# Full backend including poem generation, one-on-one, and practice endpoints.
# WARNING: This file keeps the same OpenAI client style you provided (including the api_key field).
# Do not commit your real API key to public repos.

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from openai import OpenAI
from fastapi.middleware.cors import CORSMiddleware
import unicodedata
import re
import json
import random

def clean_text(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Sadie Poetry backend is alive 💙"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====== KEEP YOUR CLIENT INSTANTIATION STYLE ======
# Replace the api_key value with your actual key (you already had this style).
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key="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"
)
# =================================================

class PoemRequest(BaseModel):
    theme: str
    style: str
    persona: str

class LineRequest(BaseModel):
    line: str

# Original endpoints
@app.post("/generate")
async def generate_poem(request: PoemRequest):
    prompt = (
        f"Write a poem in the style of {request.persona} about {request.theme}. "
        f"Use the {request.style} format. "
        "Only output the poem with a title. Do not include any commentary or explanation. "
        "Use standard English punctuation and characters only. Avoid any non-English symbols, emojis, or special characters."
    )
    completion = client.chat.completions.create(
        model="qwen/qwen3-next-80b-a3b-instruct",
        messages=[
            {"role": "system", "content": "You are a poetic assistant."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=1000
    )
    poem = clean_text(completion.choices[0].message.content)
    return {"poem": poem.strip()}

@app.post("/oneonone")
async def one_on_one(request: LineRequest):
    prompt = (
        f"The user wrote: \"{request.line}\".\n"
        "Respond with one poetic line that continues the mood and rhythm. "
        "Do not explain or comment—just the poetic line."
    )
    completion = client.chat.completions.create(
        model="qwen/qwen3-next-80b-a3b-instruct",
        messages=[
            {"role": "system", "content": "You are a poetic companion."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )
    reply = clean_text(completion.choices[0].message.content)
    return {"reply": reply.strip()}

# =========================
# Practice feature
# =========================

# Simple in-memory session store mapping cloze_line -> missing_word (lowercased)
# For short experiments only; restart of server clears it.
PRACTICE_SESSIONS = {}

def _normalize_ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")

def _pick_replacement_index(tokens):
    # choose index of token that is alphabetic and longer than 2 chars, else random
    candidates = [i for i,t in enumerate(tokens) if re.sub(r'\W+','',t).isalpha() and len(re.sub(r'\W+','',t))>2]
    if not candidates:
        return random.randrange(len(tokens)) if tokens else 0
    return random.choice(candidates)

@app.post("/practice/start")
async def practice_start(request: PoemRequest):
    """
    Request body reuses PoemRequest: theme (theme), style (type), persona (optional).
    Returns: {"cloze_line": "... ____ ..."}
    """
    typ = request.style.strip() or "free verse"
    theme = request.theme.strip() or (request.persona.strip() if request.persona else "longing")

    prompt = (
        "You are an exact, careful poetry assistant. Produce exactly one original poetic line in plain ASCII, "
        f"matching type '{typ}' and theme '{theme}'. "
        "Each time, make the line fresh and different from previous ones, with a new image, metaphor, or feeling. "
        "Do NOT repeat previous lines, blanks, or words. "
        "Keep the line concise (no more than 16 words). "
        "Choose one meaningful nontrivial word (not an article, short preposition, or punctuation) and return two non-empty lines only, "
        "Choose one meaningful nontrivial word (not an article, short preposition, or punctuation) and return two non-empty lines only, "
        "separated by a single blank line: "
        "first line = the full complete line (no underscores), "
        "second line = the cloze_line: same line but with the chosen word replaced by exactly ____ (four underscores). "
        "Output nothing else, no explanation, no JSON wrappers."
    )

    completion = client.chat.completions.create(
        model="qwen/qwen3-next-80b-a3b-instruct",
        messages=[
            {"role":"system","content":"You are a precise poetry assistant."},
            {"role":"user","content":prompt}
        ],
        max_tokens=200,
        temperature=0.6,
    )

    raw = completion.choices[0].message.content
    text = _normalize_ascii(raw).strip()

    # Parse last two non-empty lines: full_line then cloze_line
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 2:
        full_line = lines[-2]
        cloze_line = lines[-1]
    else:
        # If model returned only a single cloze line, ask client to retry generation
        if lines and "____" in lines[-1]:
            raise HTTPException(status_code=500, detail="AI returned only cloze line; retry generation once")
        raise HTTPException(status_code=500, detail="AI output parsing failed; retry")

    if "____" not in cloze_line:
        raise HTTPException(status_code=500, detail="Cloze not present in model output; retry")

    # Token alignment: try to find missing word by comparing tokens
    full_tokens = re.findall(r"\w+|____|[^\s\w]+", full_line)
    cloze_tokens = re.findall(r"\w+|____|[^\s\w]+", cloze_line)
    missing_word = None
    for f, c in zip(full_tokens, cloze_tokens):
        if c == "____":
            missing_word = f
            break
    if not missing_word:
        # fallback: look for first token in full_tokens not present in cloze_tokens
        for t in full_tokens:
            if t not in cloze_tokens and re.sub(r'\W+','',t).isalpha():
                missing_word = t
                break

    if not missing_word:
        raise HTTPException(status_code=500, detail="Could not determine missing word; retry")

    # Store lowercase canonical form
    PRACTICE_SESSIONS[cloze_line] = missing_word.lower()

    return {"cloze_line": cloze_line}

@app.post("/practice/guess")
async def practice_guess(request: Request):
    """
    Expect JSON body: {"cloze_line": "... ____ ...", "guess": "word"}
    Returns JSON with fields: result, score, feedback, missing_word, next_line
    """
    body = await request.json()
    cloze_line = (body.get("cloze_line") or "").strip()
    guess = (body.get("guess") or "").strip()
    if not cloze_line or not guess:
        raise HTTPException(status_code=400, detail="cloze_line and guess required")

    missing = PRACTICE_SESSIONS.get(cloze_line)
    if not missing:
        raise HTTPException(status_code=404, detail="Session not found; request a new cloze line")

    # Normalize for simple exact-check
    guess_norm = re.sub(r'\W+','',guess).lower()
    missing_norm = re.sub(r'\W+','',missing).lower()
    is_exact = guess_norm == missing_norm

    # Build an evaluation prompt that requests strict ASCII JSON output
    eval_prompt = (
        "You are a concise, constructive poetry tutor. The correct missing word is: "
        f"'{missing}'. The user's guess is: '{guess}'. The cloze line is: '{cloze_line}'.\n\n"
        "Evaluate the guess and return a single ASCII JSON object with these fields only: "
        "\"result\" (either \"correct\" or \"incorrect\"), "
        "\"score\" (integer 0-100), "
        "\"feedback\" (one or two sentences explaining accuracy and how to improve), "
        "\"missing_word\" (the canonical correct word), "
        "\"next_line\" (a single complete poetic line suggestion for the user to try next). "
        "Do not include any other text or commentary."
    )

    # Try to get model evaluation
    completion = client.chat.completions.create(
        model="qwen/qwen3-next-80b-a3b-instruct",
        messages=[
            {"role":"system","content":"You are a helpful, succinct poetry tutor."},
            {"role":"user","content":eval_prompt}
        ],
        max_tokens=200,
        temperature=0.5,
    )

    raw = completion.choices[0].message.content
    text = _normalize_ascii(raw).strip()

    # Try to parse JSON strictly; fallback to heuristic minimal response if parsing fails
    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None

    if not parsed:
        # Heuristic fallback
        score = 100 if is_exact else max(40, 75 - abs(len(guess_norm) - len(missing_norm))*6)
        result = "correct" if is_exact else "incorrect"
        feedback = ("Exact match. Good ear for tone and meter." if is_exact
                    else f"Close. Consider the line's rhythm and meaning; '{missing}' fits the tone better.")
        next_line = "Try: The harbour keeps the night's small, honest lights."
        # remove session and return
        PRACTICE_SESSIONS.pop(cloze_line, None)
        return {
            "result": result,
            "score": score,
            "feedback": feedback,
            "missing_word": missing,
            "next_line": next_line
        }

    # Remove session to avoid reuse
    PRACTICE_SESSIONS.pop(cloze_line, None)

    # Normalize parsed fields
    result = parsed.get("result", "correct" if is_exact else "incorrect")
    try:
        score = int(parsed.get("score", 100 if is_exact else 50))
    except Exception:
        score = 100 if is_exact else 50
    feedback = parsed.get("feedback", "")
    missing_word = parsed.get("missing_word", missing)
    next_line = parsed.get("next_line", "")

    return {
        "result": result,
        "score": score,
        "feedback": feedback,
        "missing_word": missing_word,
        "next_line": next_line
    }
