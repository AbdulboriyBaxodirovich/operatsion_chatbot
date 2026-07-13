from sqlalchemy import create_engine, Column, String, Integer, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime, timedelta, timezone
from backend.config import settings

# Toshkent vaqt mintaqasi
tashkent_tz = timezone(timedelta(hours=5))

DATABASE_URL = f"postgresql://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}@{settings.POSTGRES_HOST}:5432/{settings.POSTGRES_DB}"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class ChatSession(Base):
    __tablename__ = "chat_sessions_info"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(tashkent_tz))
    updated_at = Column(DateTime, default=lambda: datetime.now(tashkent_tz), onupdate=lambda: datetime.now(tashkent_tz))
    is_paused = Column(Boolean, default=False)
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, ForeignKey("chat_sessions_info.session_id", ondelete="CASCADE"))
    sender = Column(String)
    text = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(tashkent_tz))
    session = relationship("ChatSession", back_populates="messages")

class AdminUser(Base):
    __tablename__ = "admin_users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password = Column(String)
    role = Column(String, default="admin")
    created_at = Column(DateTime, default=lambda: datetime.now(tashkent_tz))

class KnowledgeLog(Base):
    __tablename__ = "knowledge_logs"
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, index=True)
    title = Column(String)
    added_by = Column(String)
    status = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(tashkent_tz))

def fix_fk_constraint():
    """PostgreSQL da eski chat_sessions FK ni chat_sessions_info ga o'zgartiradi"""
    with engine.connect() as conn:
        from sqlalchemy import text
        try:
            conn.execute(text("""
                ALTER TABLE chat_messages 
                DROP CONSTRAINT IF EXISTS chat_messages_session_id_fkey;
            """))
            conn.execute(text("""
                ALTER TABLE chat_messages 
                ADD CONSTRAINT chat_messages_session_id_fkey 
                FOREIGN KEY (session_id) 
                REFERENCES chat_sessions_info(session_id) 
                ON DELETE CASCADE;
            """))
            conn.commit()
            print("FK constraint muvaffaqiyatli yangilandi.")
        except Exception as e:
            print(f"FK constraint yangilashda xatolik (ehtimol allaqachon to'g'ri): {e}")

Base.metadata.create_all(bind=engine)

# Eski FK ni avtomatik tuzatamiz
fix_fk_constraint()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()