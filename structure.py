# =================================================
# 1. IMPORTS
# =================================================
import os
import re
import json
import random
import unicodedata
from html import unescape
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from openai import OpenAI
from jose import JWTError, jwt
from passlib.context import CryptContext

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, func, ForeignKey, Boolean
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship

# =================================================
# 2. CONFIGURATION
# =================================================
# Database
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sadie_local.db")
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(SQLALCHEMY_DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Security
SECRET_KEY = "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

# OpenAI Client
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key="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"
)

# =================================================
# 3. DATABASE MODELS
# =================================================
class UserModel(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    poetry_score = Column(Integer, default=0, nullable=False) 

class PoemModel(Base):
    __tablename__ = "poems"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=True)
    content = Column(Text, nullable=False)
    visibility = Column(String, default="private")
    likes_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user = relationship("UserModel", backref="poems")

class WhisperModel(Base):
    __tablename__ = "whispers"
    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    recipient_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    poem_content = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False, nullable=False) # CORRECTED: Boolean is now imported
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    sender = relationship("UserModel", foreign_keys=[sender_id])
    recipient = relationship("UserModel", foreign_keys=[recipient_id])

# =================================================
# 4. PYDANTIC SCHEMAS
# =================================================
class UserSchema(BaseModel):
    id: int
    email: str
    username: str
    poetry_score: int
    class Config:
        from_attributes = True # CORRECTED: Standardized to Pydantic v2 syntax

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
        from_attributes = True # CORRECTED: Standardized to Pydantic v2 syntax

class WhisperSchema(BaseModel):
    id: int
    poem_content: str
    is_read: bool
    created_at: datetime
    class Config:
        from_attributes = True

class UserListSchema(BaseModel):
    id: int
    username: str
    poetry_score: int
    class Config:
        from_attributes = True

class WhisperCreateSchema(BaseModel):
    recipient_id: int
    poem_content: str

class ReceivedWhispersResponse(BaseModel):
    whispers: list[WhisperSchema]
    unread_count: int

class PoemRequest(BaseModel):
    theme: str
    style: str
    persona: str

class LineRequest(BaseModel):
    line: str

class LearnRequest(BaseModel):
    subject: str
    question: str

class LoginRequest(BaseModel):
    email: str
    password: str

# =================================================
# 5. HELPER FUNCTIONS
# =================================================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_password_hash(password: str) -> str:
    password_bytes = password.encode('utf-8')
    if len(password_bytes) > 72:
        password_bytes = password_bytes[:72]
    truncated_password = password_bytes.decode('utf-8', 'ignore')
    return pwd_context.hash(truncated_password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
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

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid authentication")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid authentication")
    user = get_user_by_email(db, email=email)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user

def clean_text(text: str) -> str:
    if text is None: return ""
    s = unescape(str(text))
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    s = re.sub(r"\s+", " ", s)
    return s.strip()

# =================================================
# 6. FASTAPI APP & ENDPOINTS
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

@app.get("/")
async def root():
    return {"message": "Sadie Poetry backend is alive 💙"}

# --- AUTH ENDPOINTS ---
@app.post("/signup", response_model=UserSchema)
def signup(user: UserCreateSchema, db: Session = Depends(get_db)):
    if get_user_by_email(db, email=user.email):
        raise HTTPException(status_code=400, detail="Email already registered")
    if get_user_by_username(db, username=user.username):
        raise HTTPException(status_code=400, detail="Username already taken")
    return create_user(db=db, user=user)

@app.post("/login", response_model=TokenSchema)
def login(form_data: LoginRequest, db: Session = Depends(get_db)):
    user = get_user_by_email(db, email=form_data.email)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": user.email}, expires_delta=access_token_expires)
    return {"access_token": access_token, "token_type": "bearer"}

# --- POEM CRUD ENDPOINTS ---
@app.post("/poems", response_model=PoemSchema)
def create_poem(poem: PoemCreateSchema, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    if len(poem.content) > 10000:
        raise HTTPException(status_code=400, detail="Poem too long")
    db_poem = PoemModel(user_id=current_user.id, title=poem.title, content=poem.content, visibility=poem.visibility)
    db.add(db_poem)
    db.commit()
    db.refresh(db_poem)
    return db_poem

@app.get("/poems", response_model=list[PoemSchema])
def list_my_poems(skip: int = 0, limit: int = 50, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    return db.query(PoemModel).filter(PoemModel.user_id == current_user.id).order_by(PoemModel.created_at.desc()).offset(skip).limit(limit).all()

@app.get("/public/poems", response_model=list[PoemSchema])
def list_public_poems(skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    return db.query(PoemModel).filter(PoemModel.visibility == "public").order_by(PoemModel.likes_count.desc()).offset(skip).limit(limit).all()

@app.get("/poems/{poem_id}", response_model=PoemSchema)
def get_poem(poem_id: int, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    poem = db.query(PoemModel).filter(PoemModel.id == poem_id).first()
    if not poem:
        raise HTTPException(status_code=404, detail="Poem not found")
    if poem.visibility == "private" and poem.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return poem

# NEW: Endpoint to delete a poem
@app.delete("/poems/{poem_id}", status_code=200)
def delete_poem(
    poem_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user)
):
    """
    Deletes a poem owned by the current user.
    """
    db_poem = db.query(PoemModel).filter(PoemModel.id == poem_id).first()

    if not db_poem:
        raise HTTPException(status_code=404, detail="Poem not found")

    # Security check: Ensure the user owns the poem they are trying to delete.
    if db_poem.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this poem")

    db.delete(db_poem)
    db.commit()

    return {"message": "Poem deleted successfully"}

# --- WHISPER ENDPOINTS ---
@app.get("/users", response_model=list[UserListSchema])
def get_all_users(db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    return db.query(UserModel).filter(UserModel.id != current_user.id).all()

@app.post("/whispers/send", status_code=201)
def send_whisper(whisper_data: WhisperCreateSchema, db: Session = Depends(get_db), sender: UserModel = Depends(get_current_user)):
    recipient = db.query(UserModel).filter(UserModel.id == whisper_data.recipient_id).first()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient user not found")
    if sender.id == whisper_data.recipient_id:
        raise HTTPException(status_code=400, detail="Cannot send a whisper to yourself")
    db_whisper = WhisperModel(sender_id=sender.id, recipient_id=whisper_data.recipient_id, poem_content=whisper_data.poem_content)
    db.add(db_whisper)
    db.commit()
    return {"message": "Whisper sent successfully"}

@app.get("/whispers/received", response_model=ReceivedWhispersResponse)
def get_received_whispers(db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    whispers = db.query(WhisperModel).filter(WhisperModel.recipient_id == current_user.id).order_by(WhisperModel.created_at.desc()).all()
    unread_count = sum(1 for whisper in whispers if not whisper.is_read)
    return {"whispers": whispers, "unread_count": unread_count}

@app.post("/whispers/{whisper_id}/read", status_code=200)
def mark_whisper_as_read(whisper_id: int, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    db_whisper = db.query(WhisperModel).filter(WhisperModel.id == whisper_id).first()
    if not db_whisper:
        raise HTTPException(status_code=404, detail="Whisper not found")
    if db_whisper.recipient_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to mark this whisper as read")
    if not db_whisper.is_read:
        db_whisper.is_read = True
        db.commit()
    return {"message": "Whisper marked as read"}

# --- AI & GENERATION ENDPOINTS ---
@app.post("/generate")
async def generate_poem(request: PoemRequest):
    prompt = (f"Write a poem in the style of {request.persona} about {request.theme}. Use the {request.style} format. Only output the poem with a title. Do not include any commentary or explanation. Use standard English punctuation and characters only. Avoid any non-English symbols, emojis, or special characters.be creative.")
    completion = client.chat.completions.create(model="qwen/qwen3-next-80b-a3b-instruct", messages=[{"role": "system", "content": "You are a poetic assistant."}, {"role": "user", "content": prompt}], max_tokens=1000)
    poem = unicodedata.normalize("NFKD", completion.choices[0].message.content).encode("ascii", "ignore").decode("ascii")
    return {"poem": poem.strip()}

@app.post("/oneonone")
async def one_on_one(request: LineRequest):
    prompt = (f"The user wrote: \"{request.line}\".\nRespond with one poetic line that continues the mood and rhythm. Do not explain or comment—just the poetic line.")
    completion = client.chat.completions.create(model="qwen/qwen3-next-80b-a3b-instruct", messages=[{"role": "system", "content": "You are a poetic companion."}, {"role": "user", "content": prompt}], max_tokens=100)
    reply = clean_text(completion.choices[0].message.content)
    return {"reply": reply.strip()}

@app.post("/learn")
async def poetic_learn(request: LearnRequest):
    prompt = ("You are Sadie, a wise and creative poet who explains complex topics through beautiful, accessible verse. A student is learning about the subject of '{subject}'. Their specific question is: '{question}'.\n\nYour task is to answer their question in the form of a clear, accurate, and elegant poem. The poem should be easy to understand but also artistically crafted, using metaphors and imagery to make the concept memorable. Structure the poem into a few short stanzas.\n\nConstraints:\n1. The answer must be factually correct.\n2. The language must be poetic and engaging.\n3. Output ONLY the poem itself. Do not include a title, introduction, or any commentary.")
    try:
        completion = client.chat.completions.create(model="qwen/qwen3-next-80b-a3b-instruct", messages=[{"role": "system", "content": "You are a poetic educator named Sadie, skilled at explaining complex topics with verse."}, {"role": "user", "content": prompt.format(subject=request.subject, question=request.question)}], max_tokens=1200, temperature=0.7)
        poem_answer = clean_text(completion.choices[0].message.content)
        return {"poem_answer": poem_answer.strip()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")

@app.post("/learn/detailed")
async def poetic_learn_detailed(request: LearnRequest):
    prompt = ("You are Sadie, a wise and creative poet who explains complex topics through beautiful, accessible verse. A student requires a more detailed explanation on the subject of '{subject}'. Their question is: '{question}'.\n\nYour task is to provide a comprehensive answer in the form of a longer, more detailed poem. Expand on the core concepts, provide examples or analogies within the verse, and explore nuances. The poem should be structured into multiple stanzas to create a clear narrative flow of information.\n\nConstraints:\n1. The answer must be factually correct and more in-depth than a basic explanation.\n2. Use rich imagery and sophisticated poetic devices.\n3. Output ONLY the poem itself. Do not include a title, introduction, or any commentary.")
    try:
        completion = client.chat.completions.create(model="qwen/qwen3-next-80b-a3b-instruct", messages=[{"role": "system", "content": "You are a poetic educator named Sadie, skilled at providing deep and detailed explanations with verse."}, {"role": "user", "content": prompt.format(subject=request.subject, question=request.question)}], max_tokens=2000, temperature=0.7)
        poem_answer = clean_text(completion.choices[0].message.content)
        return {"poem_answer": poem_answer.strip()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")

# --- PRACTICE ENDPOINTS ---
PRACTICE_SESSIONS = {}
def _normalize_ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")

@app.post("/practice/start")
async def practice_start(request: PoemRequest):
    typ, theme = (request.style.strip() or "free verse"), (request.theme.strip() or "longing")
    prompt = ("You are an exact, careful poetry assistant. Produce exactly one original poetic line in plain ASCII, matching type '{typ}' and theme '{theme}'. Each time, make the line fresh and different from previous ones, with a new image, metaphor, or feeling , and keep the same rythm. Do NOT repeat previous lines, blanks, or words. Keep the line concise (no more than 16 words). Choose one meaningful nontrivial word (not an article, short preposition, or punctuation) and return two non-empty lines only, separated by a single blank line: first line = the full complete line (no underscores), second line = the cloze_line: same line but with the chosen word replaced by exactly ____ (four underscores). Output nothing else, no explanation, no JSON wrappers.")
    completion = client.chat.completions.create(model="qwen/qwen3-next-80b-a3b-instruct", messages=[{"role":"system","content":"You are a precise poetry assistant."}, {"role":"user","content":prompt}], max_tokens=200, temperature=0.6)
    raw = completion.choices[0].message.content
    lines = [ln.strip() for ln in _normalize_ascii(raw).strip().splitlines() if ln.strip()]
    if len(lines) < 2: raise HTTPException(status_code=500, detail="AI output parsing failed; retry")
    full_line, cloze_line = lines[-2], lines[-1]
    if "____" not in cloze_line: raise HTTPException(status_code=500, detail="Cloze not present; retry")
    full_tokens, cloze_tokens = re.findall(r"\w+|____|[^\s\w]+", full_line), re.findall(r"\w+|____|[^\s\w]+", cloze_line)
    missing_word = next((f for f, c in zip(full_tokens, cloze_tokens) if c == "____"), None)
    if not missing_word: missing_word = next((t for t in full_tokens if t not in cloze_tokens and re.sub(r'\W+','',t).isalpha()), None)
    if not missing_word: raise HTTPException(status_code=500, detail="Could not determine missing word; retry")
    PRACTICE_SESSIONS[cloze_line] = missing_word.lower()
    return {"cloze_line": cloze_line}

@app.post("/practice/guess")
async def practice_guess(request: Request):
    body = await request.json()
    cloze_line, guess = (body.get("cloze_line") or "").strip(), (body.get("guess") or "").strip()
    if not cloze_line or not guess: raise HTTPException(status_code=400, detail="cloze_line and guess required")
    missing = PRACTICE_SESSIONS.pop(cloze_line, None)
    if not missing: raise HTTPException(status_code=404, detail="Session not found; request a new cloze line")
    is_exact = re.sub(r'\W+','',guess).lower() == re.sub(r'\W+','',missing).lower()
    eval_prompt = (f"You are a concise, constructive poetry tutor. The correct missing word is: '{missing}'. The user's guess is: '{guess}'. The cloze line is: '{cloze_line}'.\n\nEvaluate the guess and return a single ASCII JSON object with these fields only: \"result\" (either \"correct\" or \"incorrect\"), \"score\" (integer 0-100), \"feedback\" (one or two sentences explaining accuracy and how to improve), \"missing_word\" (the canonical correct word), \"next_line\" (a single complete poetic line suggestion for the user to try next). Do not include any other text or commentary.")
    try:
        completion = client.chat.completions.create(model="qwen/qwen3-next-80b-a3b-instruct", messages=[{"role":"system","content":"You are a helpful, succinct poetry tutor."}, {"role":"user","content":eval_prompt}], max_tokens=200, temperature=0.5)
        parsed = json.loads(_normalize_ascii(completion.choices[0].message.content).strip())
        return {
            "result": parsed.get("result", "correct" if is_exact else "incorrect"),
            "score": int(parsed.get("score", 100 if is_exact else 50)),
            "feedback": parsed.get("feedback", ""),
            "missing_word": parsed.get("missing_word", missing),
            "next_line": parsed.get("next_line", "")
        }
    except Exception:
        return {
            "result": "correct" if is_exact else "incorrect",
            "score": 100 if is_exact else 50,
            "feedback": "Exact match." if is_exact else f"Close. The word was '{missing}'.",
            "missing_word": missing,
            "next_line": "The harbour keeps the night's small, honest lights."
        }
# NEW: Endpoint to increment a user's score
@app.post("/practice/increment_score", status_code=200)
def increment_score(db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    """
    Safely increments the current user's poetry score by 1.
    """
    current_user.poetry_score = (current_user.poetry_score or 0) + 1
    db.commit()
    return {"message": "Score updated", "new_score": current_user.poetry_score}

# NEW: Endpoint to get the leaderboard
@app.get("/leaderboard", response_model=list[UserListSchema])
def get_leaderboard(db: Session = Depends(get_db)):
    """
    Returns a list of all users, ordered by their poetry score in descending order.
    """
    leaderboard_users = db.query(UserModel).order_by(UserModel.poetry_score.desc()).all()
    return leaderboard_users
