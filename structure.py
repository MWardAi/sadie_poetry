from fastapi import FastAPI, HTTPException, Request, Depends
from pydantic import BaseModel
from openai import OpenAI
from fastapi.middleware.cors import CORSMiddleware
import unicodedata
import re
import json
import random
import os
from datetime import datetime, timedelta

# NEW IMPORTS for database, security, and tokens
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
from jose import JWTError, jwt
from sqlalchemy import ForeignKey, Text, DateTime, func
from sqlalchemy.orm import relationship

# =================================================
# 1. DATABASE SETUP (from before)
# =================================================
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sadie_local.db")
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# =================================================
# 2. DATABASE MODELS (from before)
# =================================================
class UserModel(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)


class PoemModel(Base):
    __tablename__ = "poems"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=True)
    content = Column(Text, nullable=False)
    visibility = Column(String, default="private")  # "private" or "public"
    likes_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("UserModel", backref="poems")


class WhisperModel(Base):
    __tablename__ = "whispers"
    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    recipient_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    poem_content = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships to get user objects if needed
    sender = relationship("UserModel", foreign_keys=[sender_id])
    recipient = relationship("UserModel", foreign_keys=[recipient_id])

# =================================================
# 3. PYDANTIC SCHEMAS (from before, with new Token schemas)
# =================================================
class UserSchema(BaseModel):
    id: int
    email: str
    username: str
    class Config:
        orm_mode = True

class UserCreateSchema(BaseModel):
    email: str
    username: str
    password: str

class TokenSchema(BaseModel):
    access_token: str
    token_type: str


class PoemCreateSchema(BaseModel):
    title: str | None = None
    content: str
    visibility: str = "private"

class PoemSchema(BaseModel):
    id: int
    user_id: int
    title: str | None
    content: str
    visibility: str
    likes_count: int
    created_at: datetime
    class Config:
        orm_mode = True

class WhisperSchema(BaseModel):
    id: int
    # We don't include sender_id for anonymity
    poem_content: str
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True

# NEW: Schema for listing users in the recipient list
class UserListSchema(BaseModel):
    id: int
    username: str

    class Config:
        from_attributes = True


# NEW: Schema for the body of the "send whisper" request
class WhisperCreateSchema(BaseModel):
    recipient_id: int
    poem_content: str

# NEW: Schema for the response when fetching received whispers
class ReceivedWhispersResponse(BaseModel):
    whispers: list[WhisperSchema]
    unread_count: int

from pydantic import BaseModel



import re
import unicodedata
from html import unescape

def clean_text(text: str) -> str:
    """
    Normalize and sanitize freeform text for storage/display:
    - convert to str and unescape HTML entities
    - normalize unicode (NFKC)
    - remove NULL/control chars
    - collapse repeated whitespace to single space
    - strip leading/trailing whitespace
    """
    if text is None:
        return ""
    # Ensure string and unescape HTML entities
    s = unescape(str(text))
    # Unicode normalization
    s = unicodedata.normalize("NFKC", s)
    # Remove C0 control characters (except newline, tab)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    # Remove any remaining non-printable characters
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    # Collapse whitespace and normalize newlines to single spaces
    s = re.sub(r"\s+", " ", s)
    return s.strip()


client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key="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"
)

# =================================================
# 4. SECURITY & TOKEN CONFIGURATION (NEW)
# =================================================
# Generate a secret key. In a real production app, get this from an environment variable.
# You can generate one using: openssl rand -hex 32
SECRET_KEY = "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password: str) -> str:
    # Truncate password to 72 bytes before hashing
    password_bytes = password.encode('utf-8')
    if len(password_bytes) > 72:
        password_bytes = password_bytes[:72]
    
    # Decode back to string to pass to passlib
    truncated_password = password_bytes.decode('utf-8', 'ignore')
    
    return pwd_context.hash(truncated_password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    # Also truncate the plain password during verification
    password_bytes = plain_password.encode('utf-8')
    if len(password_bytes) > 72:
        password_bytes = password_bytes[:72]
    
    truncated_password = password_bytes.decode('utf-8', 'ignore')

    return pwd_context.verify(truncated_password, hashed_password)


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# =================================================
# FastAPI APP and DATABASE INITIALIZATION
# =================================================
app = FastAPI()
Base.metadata.create_all(bind=engine)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency to get a DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# =================================================
# 5. CRUD (Create, Read, Update, Delete) HELPERS (NEW)
# =================================================
def get_user_by_email(db: Session, email: str):
    return db.query(UserModel).filter(UserModel.email == email).first()

def get_user_by_username(db: Session, username: str):
    return db.query(UserModel).filter(UserModel.username == username).first()

def create_user(db: Session, user: UserCreateSchema):
    hashed_password = get_password_hash(user.password)
    db_user = UserModel(email=user.email, username=user.username, hashed_password=hashed_password)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


from fastapi.security import OAuth2PasswordBearer
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

def get_user_by_email_obj(db: Session, email: str):
    return db.query(UserModel).filter(UserModel.email == email).first()

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    from jose import JWTError, jwt
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid authentication")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid authentication")
    user = get_user_by_email_obj(db, email=email)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user
# =================================================
# 6. AUTHENTICATION ENDPOINTS (NEW)
# =================================================
@app.post("/signup", response_model=UserSchema)
def signup(user: UserCreateSchema, db: Session = Depends(get_db)):
    db_user_email = get_user_by_email(db, email=user.email)
    if db_user_email:
        raise HTTPException(status_code=400, detail="Email already registered")
    db_user_username = get_user_by_username(db, username=user.username)
    if db_user_username:
        raise HTTPException(status_code=400, detail="Username already taken")
    return create_user(db=db, user=user)

# Replace the existing login(...) definition with this

class LoginRequest(BaseModel):
    email: str
    password: str

@app.post("/login", response_model=TokenSchema)
def login(form_data: LoginRequest, db: Session = Depends(get_db)):
    user = get_user_by_email(db, email=form_data.email)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.post("/poems", response_model=PoemSchema)
def create_poem(poem: PoemCreateSchema, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    # limit content length server-side
    if len(poem.content) > 10000:
        raise HTTPException(status_code=400, detail="Poem too long")
    db_poem = PoemModel(
        user_id=current_user.id,
        title=(poem.title or None),
        content=poem.content,
        visibility=poem.visibility
    )
    db.add(db_poem)
    db.commit()
    db.refresh(db_poem)
    return db_poem

@app.get("/poems", response_model=list[PoemSchema])
def list_my_poems(skip: int = 0, limit: int = 50, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    poems = db.query(PoemModel).filter(PoemModel.user_id == current_user.id).order_by(PoemModel.created_at.desc()).offset(skip).limit(limit).all()
    return poems

@app.get("/public/poems", response_model=list[PoemSchema])
def list_public_poems(skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    poems = db.query(PoemModel).filter(PoemModel.visibility == "public").order_by(PoemModel.likes_count.desc()).offset(skip).limit(limit).all()
    return poems

@app.get("/poems/{poem_id}", response_model=PoemSchema)
def get_poem(poem_id: int, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    poem = db.query(PoemModel).filter(PoemModel.id == poem_id).first()
    if poem is None:
        raise HTTPException(status_code=404, detail="Poem not found")
    if poem.visibility == "private" and poem.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return poem
# =================================================
# EXISTING POEM/PRACTICE MODELS AND ENDPOINTS
# =================================================
# ... (all your other endpoints like /generate, /oneonone, etc. remain here without any changes) ...
# I am omitting them for brevity, but you should keep them in your file.

class PoemRequest(BaseModel):
    theme: str
    style: str
    persona: str

class LineRequest(BaseModel):
    line: str

class LearnRequest(BaseModel):
    subject: str
    question: str
    
@app.get("/")
async def root():
    return {"message": "Sadie Poetry backend is alive 💙"}



# ... and so on for all your other endpoints.

@app.post("/generate")
async def generate_poem(request: PoemRequest):
    prompt = (
        f"Write a poem in the style of {request.persona} about {request.theme}. "
        f"Use the {request.style} format. "
        "Only output the poem with a title. Do not include any commentary or explanation. "
        "Use standard English punctuation and characters only. Avoid any non-English symbols, emojis, or special characters.be creative."
    )
    completion = client.chat.completions.create(
        model="qwen/qwen3-next-80b-a3b-instruct",
        messages=[
            {"role": "system", "content": "You are a poetic assistant."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=1000
    )
    poem = unicodedata.normalize("NFKD", completion.choices[0].message.content).encode("ascii", "ignore").decode("ascii")
    return {"poem": poem.strip()}

# ... and so on for all your other endpoints.

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
        "Each time, make the line fresh and different from previous ones, with a new image, metaphor, or feeling , and keep the same rythm. "
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

# =================================================
# NEW: Endpoints for the Poetic Learn feature
# =================================================

@app.post("/learn")
async def poetic_learn(request: LearnRequest):
    """
    Generates a poetic explanation for a given subject and question.
    """
    prompt = (
        "You are Sadie, a wise and creative poet who explains complex topics through beautiful, accessible verse. "
        "A student is learning about the subject of '{subject}'. Their specific question is: '{question}'.\n\n"
        "Your task is to answer their question in the form of a clear, accurate, and elegant poem. "
        "The poem should be easy to understand but also artistically crafted, using metaphors and imagery to make the concept memorable. "
        "Structure the poem into a few short stanzas.\n\n"
        "Constraints:\n"
        "1. The answer must be factually correct.\n"
        "2. The language must be poetic and engaging.\n"
        "3. Output ONLY the poem itself. Do not include a title, introduction, or any commentary."
    )
    
    try:
        completion = client.chat.completions.create(
            model="qwen/qwen3-next-80b-a3b-instruct",
            messages=[
                {"role": "system", "content": "You are a poetic educator named Sadie, skilled at explaining complex topics with verse."},
                {"role": "user", "content": prompt.format(subject=request.subject, question=request.question)}
            ],
            max_tokens=1200,
            temperature=0.7,
        )
        poem_answer = clean_text(completion.choices[0].message.content)
        return {"poem_answer": poem_answer.strip()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")

@app.post("/learn/detailed")
async def poetic_learn_detailed(request: LearnRequest):
    """
    Generates a more detailed poetic explanation.
    """
    prompt = (
        "You are Sadie, a wise and creative poet who explains complex topics through beautiful, accessible verse. "
        "A student requires a more detailed explanation on the subject of '{subject}'. Their question is: '{question}'.\n\n"
        "Your task is to provide a comprehensive answer in the form of a longer, more detailed poem. "
        "Expand on the core concepts, provide examples or analogies within the verse, and explore nuances. "
        "The poem should be structured into multiple stanzas to create a clear narrative flow of information.\n\n"
        "Constraints:\n"
        "1. The answer must be factually correct and more in-depth than a basic explanation.\n"
        "2. Use rich imagery and sophisticated poetic devices.\n"
        "3. Output ONLY the poem itself. Do not include a title, introduction, or any commentary."
    )
    
    try:
        completion = client.chat.completions.create(
            model="qwen/qwen3-next-80b-a3b-instruct",
            messages=[
                {"role": "system", "content": "You are a poetic educator named Sadie, skilled at providing deep and detailed explanations with verse."},
                {"role": "user", "content": prompt.format(subject=request.subject, question=request.question)}
            ],
            max_tokens=2000, # Increased token limit for more detail
            temperature=0.7,
        )
        poem_answer = clean_text(completion.choices[0].message.content)
        return {"poem_answer": poem_answer.strip()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")


// ...existing code...
# ... (your /login endpoint code) ...


# =================================================
# 7. WHISPER ENDPOINTS (NEW)
# =================================================

# Endpoint to get a list of all users (to select a recipient)
@app.get("/users", response_model=list[UserListSchema])
def get_all_users(db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    """
    Provides a list of all users so the sender can choose a recipient.
    The current user is excluded from the list.
    """
    users = db.query(UserModel).filter(UserModel.id != current_user.id).all()
    return users


# Endpoint to send a whisper
@app.post("/whispers/send", status_code=201)
def send_whisper(
    whisper_data: WhisperCreateSchema,
    db: Session = Depends(get_db),
    sender: UserModel = Depends(get_current_user)
):
    """
    Creates a new whisper in the database.
    The sender is identified by their auth token.
    """
    # Verify the recipient user exists
    recipient = db.query(UserModel).filter(UserModel.id == whisper_data.recipient_id).first()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient user not found")

    # Prevent a user from sending a whisper to themselves
    if sender.id == whisper_data.recipient_id:
        raise HTTPException(status_code=400, detail="Cannot send a whisper to yourself")

    # Create the new whisper record
    db_whisper = WhisperModel(
        sender_id=sender.id,
        recipient_id=whisper_data.recipient_id,
        poem_content=whisper_data.poem_content
    )
    db.add(db_whisper)
    db.commit()

    return {"message": "Whisper sent successfully"}



# Endpoint to get all whispers received by the current user
@app.get("/whispers/received", response_model=ReceivedWhispersResponse)
def get_received_whispers(db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    """
    Fetches all whispers for the logged-in user and counts how many are unread.
    """
    whispers = db.query(WhisperModel).filter(WhisperModel.recipient_id == current_user.id).order_by(WhisperModel.created_at.desc()).all()
    unread_count = sum(1 for whisper in whispers if not whisper.is_read)
    
    return {"whispers": whispers, "unread_count": unread_count}


# Endpoint to mark a whisper as read
@app.post("/whispers/{whisper_id}/read", status_code=200)
def mark_whisper_as_read(
    whisper_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user)
):
    """
    Marks a specific whisper as read.
    Ensures that only the recipient can perform this action.
    """
    db_whisper = db.query(WhisperModel).filter(WhisperModel.id == whisper_id).first()

    if not db_whisper:
        raise HTTPException(status_code=404, detail="Whisper not found")

    if db_whisper.recipient_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to mark this whisper as read")

    if not db_whisper.is_read:
        db_whisper.is_read = True
        db.commit()

    return {"message": "Whisper marked as read"}
