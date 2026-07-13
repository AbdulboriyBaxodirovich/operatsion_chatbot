from backend.embeddings import EmbeddingModel
from fastapi import APIRouter, Response, Cookie, Depends, HTTPException, Request, UploadFile, File, Query
from pydantic import BaseModel
import httpx
from sqlalchemy.orm import Session
from backend.schemas import ChatRequest, ChatResponse
from backend.database import AdminUser, KnowledgeLog, get_db, ChatSession, ChatMessage
from backend.rag import search_knowledge_base, ask_vllm_model
import uuid
from backend.llm_services import rewrite_query
from bs4 import BeautifulSoup
from backend.config import settings
from qdrant_client import QdrantClient
from qdrant_client.http import models as rest
from datetime import datetime, timedelta, timezone
from backend.rag import embeddings
import re
from backend.limiter import limiter
import io
from typing import List, Optional
from fastapi.responses import FileResponse
# PDF va DOCX o'qish uchun kutubxonalar
import PyPDF2
import docx
import os
# Toshkent vaqt mintaqasini yaratamiz (UTC + 5 soat)
tashkent_tz = timezone(timedelta(hours=5))

# ==========================================
# XATONINI TUZATISH UCHUN QO'SHILGAN QISM
# QdrantClient klassini obyektga aylantiramiz
# ==========================================
qdrant_host = settings.QDRANT_HOST
if not qdrant_host.startswith("http"):
    qdrant_host = f"http://{qdrant_host}"

qdrant_client = QdrantClient(url=qdrant_host)
# ==========================================

chat_router = APIRouter(prefix="/chat", tags=["Bot bilan suhbat"])
admin_router = APIRouter(prefix="/admin", tags=["Admin Panel"])
superadmin_router = APIRouter(prefix="/superadmin", tags=["Super Admin Panel"])

# ==========================================
# AUDIT LOG HELPER
# ==========================================
def write_audit(
    db: Session, 
    admin_username: str, 
    action: str, 
    title: str,
    url: str = "",
    status: str = None   # Qo'shimcha parametr
):
    """Har bir operatsiyani alohida log sifatida saqlaydi"""
    
    # Statusni aniqlash
    if status is None:  # Agar tashqaridan berilmasa, avtomatik aniqlaydi
        if action in ["o'chirdi", "o'chirdi (barchasi)"]:
            status = "o'chirildi"
            if action == "o'chirdi (barchasi)":
                title = "Umumiy baza"
        else:
            try:
                records, _ = qdrant_client.scroll(
                    collection_name=settings.COLLECTION_NAME,
                    limit=3,
                    with_payload=True,
                    with_vectors=False,
                    filter=rest.Filter(
                        must=[rest.FieldCondition(
                            key="savol",
                            match=rest.MatchValue(value=title)
                        )]
                    )
                )
                exists = len(records) > 0
                status = "O'zgargan" if exists else "Yangi"
            except Exception:
                status = "Yangi"

    # **HAR SAFAR YANGI YOZUV** yaratamiz (update emas!)
    new_log = KnowledgeLog(
        url=url or "",
        title=title,
        added_by=admin_username,
        status=status,
        created_at=datetime.now(tashkent_tz)
    )
    db.add(new_log)
    db.commit()
    
    print(f"✅ YANGI LOG YOZILDI → {title} | {status} | {admin_username}")
    return new_log
    
# ==========================================
# FAYLLARDAN MATN O'QISH UCHUN UMUMIY FUNKSIYA
# ==========================================
async def extract_text_from_file(file: UploadFile):
    """PDF va DOCX fayllarni o'qib, toza matn qaytaruvchi funksiya"""
    filename = file.filename.lower()
    content = await file.read()
    text = ""

    try:
        if filename.endswith(".pdf"):
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(content))
            for page in pdf_reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
                    
        elif filename.endswith(".docx"):
            doc = docx.Document(io.BytesIO(content))
            for para in doc.paragraphs:
                text += para.text + "\n"
                
        elif filename.endswith(".doc"):
            raise HTTPException(status_code=400, detail="Kechirasiz, tizim eski .doc formati o'rniga yangi .docx yoki .pdf formatini talab qiladi.")
            
        else:
            raise HTTPException(status_code=400, detail="Faqat PDF va DOCX fayllar qabul qilinadi!")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Faylni o'qishda tizimli xatolik: {str(e)}")

    clean_text = '\n'.join([line.strip() for line in text.splitlines() if line.strip()])
    
    if not clean_text:
        raise HTTPException(status_code=400, detail="Fayl ichidan hech qanday yozuv yoki matn topilmadi!")

    return {"status": "success", "text": clean_text, "title": file.filename}


# ==========================================
# 1. CHAT BO'LIMI
# ==========================================
class ChatResponse(BaseModel):
    reply: str
    context_used: List[str]
    status: str
    current_topic: Optional[str] = None
    
@chat_router.post("/", response_model=ChatResponse)
@limiter.limit("20/minute")
async def chat_with_bot(request: Request, payload: ChatRequest, response: Response, session_id: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    
    # 1. Agar brauzer cookieni tanimasdan umuman yubormasa yoki u bo'sh bo'lsa
    if not session_id:
        session_id = str(uuid.uuid4())
        response.set_cookie(
            key="session_id", 
            value=session_id, 
            httponly=True, 
            samesite="lax",    
            secure=False,      
            max_age=31536000
        )
    
    # Bazadan qidiramiz
    db_session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    
    # 2. Agar bazada yo'q bo'lsa (yoki o'chirilgan bo'lsa) yangi ochamiz
    if not db_session:
        db_session = ChatSession(session_id=session_id) 
        db.add(db_session)
        db.commit()
        
        response.set_cookie(
            key="session_id", 
            value=session_id, 
            httponly=True, 
            samesite="lax", 
            secure=False, 
            max_age=31536000
        )
    else:
        if db_session.is_paused:
            raise HTTPException(status_code=403, detail="Sizning sessiyangiz bank administratorlari tomonidan vaqtincha to'xtatilgan.")
        
        db_session.updated_at = datetime.now(tashkent_tz)
        db.commit()
        
    # Suhbat tarixini bazadan tortib olamiz
    chat_history = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at).all()
    
    formatted_history = []
    for msg in chat_history:
        role = "user" if msg.sender == "user" else "assistant"
        formatted_history.append({"role": role, "content": msg.text})
        
    rewritten_query = await rewrite_query(formatted_history, payload.message)

    # =========================================================================
    # 🔄 O'ZGARISH JOI: Qdrant'dan kontekst VA top mavzuni unpack qilib olamiz
    # =========================================================================
    context, top_topic = search_knowledge_base(rewritten_query)

    # 🧠 MAVZUNI SESSIDAN SAQLASH LOGIKASI
    # Agar Qdrant aniq mavzu topa olmasa (masalan, foydalanuvchi "rahmat" yoki "salom" desa),
    # bazadagi ushbu sessiyada saqlangan oxirgi faol mavzuni saqlab qolamiz (chizma yo'qolib qolmasligi uchun).
    # [Eslatma: ChatSession modelida 'current_topic' degan ustun (column) bor deb hisoblaymiz]
    if not top_topic:
        top_topic = getattr(db_session, "current_topic", "Umumiy suhbat") or "Umumiy suhbat"
    else:
        if hasattr(db_session, "current_topic"):
            db_session.current_topic = top_topic
            db.commit()
    # =========================================================================

    # 3. vLLM modeliga yuborib, haqiqiy javobni olamiz
    bot_reply = ask_vllm_model(rewritten_query, context, formatted_history)
    
    # 4. Yozishmani bazaga saqlash
    user_msg = ChatMessage(session_id=session_id, sender="user", text=payload.message)
    bot_msg = ChatMessage(session_id=session_id, sender="bot", text=bot_reply)
    
    db.add(user_msg)
    db.add(bot_msg)
    db.commit()
    db.refresh(db_session)
    
    # 🚀 MANA SHU LOGNI QO'SHING:
    print(f"\n================ BACKEND DIAGNOSTIKA ================")
    print(f"💬 Foydalanuvchi xabari: {payload.message}")
    print(f"🎯 Qdrantdan olingan TOPIC: {top_topic}")
    print(f"=====================================================\n")
    
    # =========================================================================
    # 🎯 RETURN QISMI: Front-end qabul qilishi uchun top_topic'ni ham qo'shamiz
    # =========================================================================
    return ChatResponse(
        reply=bot_reply, 
        context_used=[context[:200] + "..."] if context else [], 
        status="success",
        current_topic=top_topic  # 👈 SHU YERGA QO'SHILDI
    )

class ProcessStep(BaseModel):
    id: int
    name: str
    time: str
 
class ProcessResponse(BaseModel):
    code: str
    name: str
    total_time: str
    steps: List[ProcessStep] = []
 
 
@chat_router.get("/processes", response_model=List[ProcessResponse])
async def get_all_processes(
    request: Request,
    response: Response,
    q: Optional[str] = Query(None),
    limit: int = Query(50),
    session_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    # Session boshqaruvi va Qdrant'dan o'qish qismlari xuddi sizniki kabi o'zgarmaydi...
    
    # ── Session boshqaruvi ────────────────────────────────────
    if not session_id:
        session_id = str(uuid.uuid4())
        response.set_cookie(
            key="session_id", value=session_id,
            httponly=True, samesite="lax", secure=False, max_age=31536000
        )

    db_session = db.query(ChatSession).filter(
        ChatSession.session_id == session_id
    ).first()

    if not db_session:
        db_session = ChatSession(session_id=session_id)
        db.add(db_session)
        db.commit()
        response.set_cookie(
            key="session_id", value=session_id,
            httponly=True, samesite="lax", secure=False, max_age=31536000
        )
    else:
        if db_session.is_paused:
            raise HTTPException(
                status_code=403,
                detail="Sizning sessiyangiz bank administratorlari tomonidan vaqtincha to'xtatilgan."
            )
        db_session.updated_at = datetime.now(tashkent_tz)
        db.commit()

    # ── Qdrant dan ma'lumot olish ─────────────────────────────
    try:
        records, _ = qdrant_client.scroll(
            collection_name=settings.COLLECTION_NAME,
            limit=10000,
            with_payload=True,
            with_vectors=False
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Jarayonlarni yuklashda xatolik: {str(e)}"
        )

    # ── XATOLIK TUZATILGAN JOY: Guruhlash ─────────────────────
    groups: dict[str, dict] = {}

    for record in records:
        payload = record.payload or {}
        source    = str(payload.get("source") or "Noma'lum").strip()
        kontekst  = str(payload.get("kontekst") or "").strip()
        savol     = str(payload.get("savol") or "").strip()
        chunk_idx = int(payload.get("chunk_index") or 0)

        # XATOLIK YECHIMI: source emas, savol (title) bo'yicha guruhlaymiz!
        group_key = savol 
        
        # Agar qandaydir sabab bilan savol bo'sh bo'lsa, source orqali guruhlashga o'tish (fallback)
        if not group_key:
            group_key = source

        if group_key not in groups:
            groups[group_key] = {
                "source":     source,
                "savol":      savol,
                "kontekst":   kontekst,
                "chunk_index": chunk_idx,
            }
        elif chunk_idx < groups[group_key]["chunk_index"]:
            # Har doim eng birinchi chunkni (0-indeks) saqlaymiz, 
            # chunki boshlang'ich ma'lumotlar odatda shu yerda bo'ladi
            groups[group_key].update({
                "savol":      savol,
                "kontekst":   kontekst,
                "chunk_index": chunk_idx,
            })

    # ── Har bir guruhdan jarayon ma'lumotini parse qilish ─────
    result = []

    # Endi groups.items() dagi source_key bu bizning savol (title)
    for source_key, g in groups.items():
        kontekst = g["kontekst"]
        savol    = g["savol"]
        source   = g["source"] # Asl source maydonini saqlab qolganmiz

        # Qidiruv filtri
        if q and q.lower() not in kontekst.lower() and q.lower() not in savol.lower():
            continue

        # 1. Jarayon kodini aniqlash
        code_match = re.search(
            r"\b(A\d+(?:\.\d+){2,3})\b(?!\.\d)",
            kontekst + " " + savol
        )
        if code_match:
            code = code_match.group(1)
        else:
            # Fallback
            fn_match = re.match(r"^([A-Z]\d+(?:[._]\d+){2,3})", savol) # source o'rniga savoldan izlash samaraliroq
            code = fn_match.group(1).replace("_", ".") if fn_match else f"PR-{len(result)+1}"

        # 2. Jarayon nomini aniqlash
        name = savol
        name = re.sub(r"^A\d+(?:\.\d+)+\s*[\"«\-–]?\s*", "", name).strip()
        name = re.sub(r"\.(docx?|pdf)$", "", name, flags=re.IGNORECASE).strip()
        if not name:
            name = re.sub(r"\.(docx?|pdf)$", "", source, flags=re.IGNORECASE).strip()

        # 3. Umumiy vaqtni aniqlash
        time_match = re.search(
            r"(?:sarflanadigan vaqti|umumiy vaqt)[^\d]*(\d+(?:[,.]\d+)?)\s*daqiqa",
            kontekst, re.IGNORECASE
        )
        total_time = f"{time_match.group(1).replace(',', '.')} daqiqa" if time_match else "—"

        # 4. Bosqichlarni aniqlash
        steps = []
        if re.match(r"^A[\d.]+$", code):
            step_pattern = re.compile(
                rf"({re.escape(code)}\.\d+)\s+([^\n]+?)(?=\n|{re.escape(code)}\.\d+|\Z)",
                re.DOTALL
            )
            for i, m in enumerate(step_pattern.finditer(kontekst), 1):
                step_code = m.group(1).strip()
                step_name = m.group(2).strip()

                step_name = re.split(
                    r'\s+(?:Risk|Jarayon ijrochilari|Muddatlarga|Ushbu funksiya)',
                    step_name
                )[0].strip()[:150]

                step_block_match = re.search(
                    rf"{re.escape(step_code)}(.*?)(?={re.escape(code)}\.\d+|\Z)",
                    kontekst, re.DOTALL
                )
                step_time = "—"
                if step_block_match:
                    step_block = step_block_match.group(1)
                    t_match = re.search(
                        r"Muddatlarga[^\d]*(\d+)\s*daqiqa",
                        step_block
                    )
                    if t_match:
                        step_time = f"{t_match.group(1)} daqiqa"

                steps.append({
                    "id":   i,
                    "name": step_name,
                    "time": step_time,
                })

        result.append({
            "code":       code,
            "name":       name,
            "total_time": total_time,
            "steps":      steps,
        })

    # Kod bo'yicha tartiblash
    result.sort(key=lambda x: x["code"])

    return result[:limit]

# ==========================================
# YASHIRIN LOG (TUGMALAR ORQALI) QO'SHISH ROUTER
# ==========================================
class LogInteractionRequest(BaseModel):
    user_message: str
    bot_message: str

@chat_router.post("/log-interaction")
def log_interaction(payload: LogInteractionRequest, session_id: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    """Frontendda bosilgan tugmalar va UI habarlarini tarixga jimgina saqlash"""
    if not session_id:
        return {"status": "error", "detail": "Session topilmadi"}
        
    db_session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    if not db_session:
        db_session = ChatSession(session_id=session_id) 
        db.add(db_session)
        db.commit()
    
    # AGAR SESSIYA PAUZADA BO'LSA - HATTO TUGMALAR HAM YOZILMAYDI
    if db_session.is_paused:
        raise HTTPException(status_code=403, detail="Sessiya to'xtatilgan")
        
    db_session.updated_at = datetime.now(tashkent_tz)
    
    user_msg = ChatMessage(session_id=session_id, sender="user", text=payload.user_message)
    bot_msg = ChatMessage(session_id=session_id, sender="bot", text=payload.bot_message)
    
    db.add(user_msg)
    db.add(bot_msg)
    db.commit()
    
    return {"status": "success"}

# ==========================================
# TUZATILGAN VA AVTOMATIK COOKIE ODIGAN HISTORY ROUTER
# ==========================================
@chat_router.get("/chat-history")
def get_client_chat_history(session_id: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    """Mijoz o'zining brauzer cookie-sidagi session_id bo'yicha tarixini va PAUZA HOLATINI ko'rishi uchun"""
    
    if not session_id:
        return {"status": "empty", "chat_history": [], "is_paused": False}
        
    db_session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    is_paused = db_session.is_paused if db_session else False
        
    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at).all()
    
    if not messages:
        return {"status": "empty", "chat_history": [], "is_paused": is_paused}
        
    chat_history = [{"sender": msg.sender, "text": msg.text} for msg in messages]
    
    # JSON orqali is_paused qismi ham frontendga ketadi
    return {
        "status": "success", 
        "chat_history": chat_history, 
        "is_paused": is_paused
    }
    

# 1. Router turgan papka ichidagi "diagrams" hududini aniqlaymiz
# Bu kod loyiha qayerda ishga tushsa ham aynan shu router.py yonidagi papkani topadi
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DIAGRAMS_DIR = os.path.join(CURRENT_DIR, "diagrams")

@chat_router.get("/diagram-file/{filename}")
async def get_diagram_image(filename: str):
    """Front-end so'ragan chizma rasmini xavfsiz o'qib uzatuvchi endpoint"""
    
    # Xavfsizlik: path traversal (../) hujumlarini oldini olish uchun faqat fayl nomini olamiz
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(DIAGRAMS_DIR, safe_filename)
    
    # 🚀 MANA SHU LOGNI QO'SHING:
    print(f"📷 [GET /diagram-file] Brauzer rasm so'radi: {safe_filename}")
    print(f"📂 Fayl qidirilayotgan yo'l: {file_path}")
    print(f"🔍 Fayl mavjudmi?: {os.path.exists(file_path)}")
    
    # Agar papkadan bunday rasm topilmasa, xato qaytaramiz
    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=404, 
            detail=f"Kechirasiz, '{safe_filename}' nomli chizma fayli diagrams papkasida topilmadi."
        )
        
    # Rasmni to'g'ridan-to'g'ri brauzerga rasm ko'rinishida qaytaramiz
    return FileResponse(file_path)

# ==========================================
# 2. ADMIN BO'LIMI
# ==========================================
class AdminLogin(BaseModel):
    username: str
    password: str
    
def get_current_admin(admin_user: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    if not admin_user:
        raise HTTPException(status_code=401, detail="Tizimga kirmagansiz!")
    
    # MUHIM O'ZGARISH: Bu yerda faqat "admin" roliga ega bo'lganlarni tasdiqlaymiz!
    user = db.query(AdminUser).filter(AdminUser.username == admin_user, AdminUser.role == "admin").first()
    if not user:
        raise HTTPException(status_code=401, detail="Bunday admin topilmadi yoki huquqingiz yo'q!")
    
    return user

# 2. Login qilish API si
@admin_router.post("/login")
@limiter.limit("5/minute")
def admin_login(request: Request, data: AdminLogin, response: Response, db: Session = Depends(get_db)):
    # MUHIM O'ZGARISH: Login va paroldan tashqari, uning roli oddiy "admin" ekanligini ham tekshiramiz!
    user = db.query(AdminUser).filter(
        AdminUser.username == data.username, 
        AdminUser.password == data.password,
        AdminUser.role == "admin"  # <--- Shu qator super adminlarning kirishini to'sadi
    ).first()
    
    if not user:
        raise HTTPException(status_code=401, detail="Login/parol xato yoki siz oddiy admin emassiz!")

    # Agar to'g'ri bo'lsa, brauzerga 1 kunlik (86400 soniya) Cookie o'rnatamiz
    response.set_cookie(key="admin_user", value=user.username, httponly=True, samesite="strict", secure=True)
    return {"status": "success", "username": user.username, "role": user.role}

# 3. Tizimdan chiqish (Logout)
@admin_router.post("/logout")
def admin_logout(response: Response):
    response.delete_cookie("admin_user")
    return {"status": "success"}

# 4. Holatni tekshirish (Sahifa yangilanganda kerak bo'ladi)
@admin_router.get("/me")
def check_auth(admin: AdminUser = Depends(get_current_admin)):
    return {"username": admin.username, "role": admin.role}

# ====================================================
# ADMIN VA SUPER ADMIN PANELI SESSIIYALAR JADVALI API LARI
# ====================================================

@admin_router.get("/sessions")
def get_all_sessions(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    sessions = db.query(ChatSession).order_by(ChatSession.updated_at.desc()).all()
    
    result = []
    for s in sessions:
        # Xabarlarni sana bo'yicha guruhlash
        messages = db.query(ChatMessage).filter(
            ChatMessage.session_id == s.session_id
        ).order_by(ChatMessage.created_at).all()
        
        # Oylar bo'yicha hisoblash: {"2025-06": 30, "2025-08": 30, "2025-09": 43}
        messages_by_month = {}
        for msg in messages:
            if msg.created_at:
                month_key = msg.created_at.strftime("%Y-%m")
                messages_by_month[month_key] = messages_by_month.get(month_key, 0) + 1
        
        result.append({
            "id": s.id,
            "session_id": s.session_id,
            "created_at": s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else "Noqonuniy",
            "updated_at": s.updated_at.strftime("%Y-%m-%d %H:%M:%S") if s.updated_at else "Noqonuniy",
            "is_paused": s.is_paused if hasattr(s, 'is_paused') else False,
            "platform": "web",
            "messages_count": len(messages),
            "messages_by_month": messages_by_month  # ← YANGI
        })
    
    return {"active_sessions": result}

@admin_router.get("/sessions/{session_id}")
def get_session_chat(session_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Tanlangan session_id bosilganda uning butun suhbat tarixini DB dan o'qish"""
    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at).all()
    
    if not messages:
        return {"error": "Bunday suhbat topilmadi!", "chat_history": []}
        
    chat_history = [
        {
            "sender": msg.sender, 
            "text": msg.text, 
            "time": msg.created_at.strftime("%Y-%m-%d %H:%M:%S") if isinstance(msg.created_at, datetime) else str(msg.created_at)
        } for msg in messages
    ]
    return {"session_id": session_id, "chat_history": chat_history}

# SESSIIYANI BUTUNLAY O'CHIRISH API
@admin_router.delete("/sessions/{session_id}")
def delete_session(session_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete()
    db.query(ChatSession).filter(ChatSession.session_id == session_id).delete()
    db.commit()
    return {"status": "success", "message": "Sessiya o'chirildi."}

# SESSIIYANI VAQTINCHA TO'XTATISH (PAUZA) API
@admin_router.put("/sessions/{session_id}/toggle-pause")
def toggle_session_pause(session_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Sessiya topilmadi")
    session.is_paused = not session.is_paused
    db.commit()
    return {"status": "success", "is_paused": session.is_paused}

class URLRequest(BaseModel):
    url: str

@admin_router.post("/scrape")
async def scrape_website(request: URLRequest, admin: AdminUser = Depends(get_current_admin)):
    """URL dan matnni avtomatik yulib olish (Scraping)"""
    try:
        # Saytga so'rov yuborish
        async with httpx.AsyncClient(timeout=15.0) as client:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            response = await client.get(request.url, headers=headers)
            response.raise_for_status()
            
            # HTML ni parse qilish
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 1. Sarlavhani (Title) avtomatik olish
            title_tag = soup.find('title')
            page_title = title_tag.get_text(strip=True) if title_tag else ""
            
            # Saytdagi keraksiz qismlarni (kodlar, menyu, footer) o'chirib tashlaymiz
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.extract()
                
            # Faqat toza matnni ajratib olamiz
            text = soup.get_text(separator='\n')
            
            # Ortiqcha bo'shliq va qatorlarni tozalash
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            clean_text = '\n'.join(lines)
            
            # Title ham qaytariladi
            return {"status": "success", "text": clean_text, "title": page_title}
            
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Saytni o'qib bo'lmadi: {str(e)}")

# ==========================================
# DIAGRAMS HELPER
# ==========================================
import zipfile, io, os, re, uuid, tempfile, subprocess, shutil

DIAGRAMS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagrams")
os.makedirs(DIAGRAMS_DIR, exist_ok=True)


def get_file_stem(filename: str) -> str:
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r'[\\/:*?"<>|]', '', stem).strip()
    return stem


def save_diagram_from_pdf(content: bytes, file_stem: str) -> bool:
    """PDF dan rasm ajratib diagrams/ ga saqlaydi. Qdrant ga yozmaydi."""
    try:
        import fitz
        doc = fitz.open(stream=io.BytesIO(content), filetype="pdf")
        save_path = os.path.join(DIAGRAMS_DIR, f"{file_stem}.png")

        # Ichki rasmlarni qidiramiz
        for page_num in range(len(doc)):
            imgs = doc[page_num].get_images(full=True)
            if imgs:
                xref = imgs[0][0]
                try:
                    base_img = doc.extract_image(xref)
                    with open(save_path, 'wb') as f:
                        f.write(base_img["image"])
                    print(f"PDF rasm saqlandi: {save_path}")
                    return True
                except Exception as img_err:
                    print(f"PDF rasm ajratishda xato: {img_err}")
                    continue

        # Ichki rasm yo'q — sahifani render qilamiz
        page = doc[0]
        pix = page.get_pixmap(dpi=150)
        pix.save(save_path)
        print(f"PDF sahifa render qilindi: {save_path}")
        return True

    except Exception as e:
        print(f"fitz xatosi, LibreOffice bilan urinilmoqda: {e}")

    # Fallback — LibreOffice
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "document.pdf")
            with open(pdf_path, 'wb') as f:
                f.write(content)
            result = subprocess.run(
                ['libreoffice', '--headless', '--convert-to', 'png',
                 '--outdir', tmpdir, pdf_path],
                capture_output=True, text=True, timeout=60
            )
            print(f"LibreOffice: {result.stdout} {result.stderr}")
            converted = os.path.join(tmpdir, "document.png")
            if os.path.exists(converted):
                save_path = os.path.join(DIAGRAMS_DIR, f"{file_stem}.png")
                shutil.copy(converted, save_path)
                print(f"LibreOffice render saqlandi: {save_path}")
                return True
    except Exception as lo_err:
        print(f"LibreOffice xatosi: {lo_err}")

    return False


def chunk_text(text: str, max_chars: int = 100000) -> list[str]:
    """
    Matnni mantiqan gaplarga bo'lib, har bir qismni taxminan 2000 belgidan
    oshmaydigan qilib yig'uvchi aqlli chunker. So'zlar o'rtasidan bo'linib ketmaydi.
    """
    # Matnni avval gaplarga, yangi qatorlarga va xatboshilarga bo'lamiz
    raw_sentences = re.split(r'(?<=[.!?])\s+|\n+|<br/?>|<p>', text)
    chunks = []
    current_chunk = ""
    
    for sentence in raw_sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
            
        # Agar bitta gapning o'zi 2000 dan uzun bo'lsa (noodatiy holat), uni majburan bo'lamiz
        if len(sentence) > max_chars:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
            for i in range(0, len(sentence), max_chars):
                chunks.append(sentence[i:i+max_chars])
            continue
            
        # Agar joriy chunk va yangi gapni qo'shganda 2000 dan oshib ketsa, joriy chunkni saqlaymiz
        if len(current_chunk) + len(sentence) + 1 > max_chars:
            chunks.append(current_chunk.strip())
            current_chunk = sentence
        else:
            current_chunk += " " + sentence if current_chunk else sentence
            
    # Oxirida qolib ketgan matnni ham qo'shamiz
    if current_chunk:
        chunks.append(current_chunk.strip())
        
    return chunks


# ==========================================
# ADMIN UCHUN: KOP FAYLDAN YUKLASH VA BAZAGA YOZISH (AUDIT BILAN)
# ==========================================
@admin_router.post("/upload-file")
async def superadmin_upload_file(
    request: Request,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    try:
        form = await request.form()
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Form ma'lumotlarini o'qishda xatolik. pip install python-multipart"
        )

    files = form.getlist("files")
    if not files:
        files = form.getlist("file")

    valid_files = [f for f in files if getattr(f, "filename", None)]
    if not valid_files:
        raise HTTPException(status_code=400, detail="Yuklash uchun fayllar topilmadi!")

    success_count = 0
    errors = []

    for file in valid_files:
        try:
            filename_lower = file.filename.lower()
            content = await file.read()
            if not content:
                errors.append(f"{file.filename}: fayl bo'sh")
                continue

            file_stem = get_file_stem(file.filename)
            print(f"\n=== Fayl: {file.filename} | {len(content)} bytes ===")

            if filename_lower.endswith('.pdf'):
                print(f"PDF — faqat rasm ajratiladi, Qdrant ga yozilmaydi")
                result = save_diagram_from_pdf(content, file_stem)
                
                existing = os.path.exists(os.path.join(DIAGRAMS_DIR, f"{file_stem}.png"))
                action = "o'zgartirdi" if existing else "qo'shdi"
                status_text = "O'zgargan" if existing else "Yangi"
                
                # Har safar yangi log
                write_audit(
                    db=db,
                    admin_username=admin.username,
                    action=action,
                    title=file.filename,
                    url=file.filename,
                    status=status_text
                )
                
                success_count += 1
                continue

            if filename_lower.endswith(('.doc', '.docx')):
                class FileLike:
                    def __init__(self, b, name):
                        self.filename = name
                        self._bytes = b
                    async def read(self):
                        return self._bytes

                extracted = await extract_text_from_file(FileLike(content, file.filename))
                text  = extracted["text"]
                title = extracted["title"]

                if not text.strip():
                    errors.append(f"{file.filename}: matn topilmadi")
                    continue

                # ====================== QDRANT ORQALI TEKSHIRUV ======================
                try:
                    clean_title = re.sub(r'\.(pdf|docx?)$', '', title, flags=re.IGNORECASE).strip()
                    records, _ = qdrant_client.scroll(
                        collection_name=settings.COLLECTION_NAME,
                        limit=5,
                        with_payload=True,
                        with_vectors=False,
                        scroll_filter=rest.Filter(          # ← scroll_filter emas, filter bo'lishi kerak!
                            should=[  # OR
                                rest.FieldCondition(key="savol", match=rest.MatchValue(value=title)),
                                rest.FieldCondition(key="savol", match=rest.MatchValue(value=clean_title)),
                            ]
                        )
                    )
                    exists_in_qdrant = len(records) > 0
                except Exception as e:
                    print(f"Qdrant tekshiruv xatosi: {e}")
                    exists_in_qdrant = False
                # ===================================================================================

                holat_text = "O'zgargan" if exists_in_qdrant else "Yangi"
                action = "o'zgartirdi" if exists_in_qdrant else "qo'shdi"

                # Avvalgi ma'lumotni tozalash
                try:
                    qdrant_client.delete(
                        collection_name=settings.COLLECTION_NAME,
                        points_selector=rest.Filter(
                            must=[rest.FieldCondition(
                                key="savol",
                                match=rest.MatchValue(value=title)
                            )]
                        )
                    )
                except Exception:
                    pass

                chunks = chunk_text(text)
                points_to_upsert = []
                for i, chunk in enumerate(chunks):
                    chunk_id = str(uuid.uuid5(
                        uuid.NAMESPACE_DNS, f"title_{title}_chunk_{i}"
                    ))
                    vector = embeddings.embed_query(
                        f"passage: Mavzu: {title}.\nMatn: {chunk}"
                    )
                    points_to_upsert.append({
                        "id": chunk_id,
                        "vector": vector,
                        "payload": {
                            "savol": title,
                            "kontekst": chunk,
                            "source": "Hujjat/Fayl",
                            "chunk_index": i,
                            "holat": holat_text,
                            "admin": admin.username
                        }
                    })

                qdrant_client.upsert(
                    collection_name=settings.COLLECTION_NAME,
                    points=points_to_upsert
                )
                
                # Audit log — har yuklashda yangi yozuv yaratiladi
                write_audit(
                    db=db,
                    admin_username=admin.username,
                    action=action,
                    title=title,
                    url="",           # DOCX uchun url bo'sh
                    status=holat_text
                )
                
                success_count += 1
                continue

            errors.append(f"{file.filename}: faqat PDF, DOC, DOCX qabul qilinadi")
        except Exception as e:
            errors.append(f"{file.filename} da xatolik: {str(e)}")
            print(f"Xato {file.filename}: {e}")

    if errors:
        return {
            "status": "partial",
            "message": f"{success_count} ta fayl saqlandi. Xatoliklar: " + " | ".join(errors)
        }

    return {
        "status": "success",
        "message": f"{success_count} ta fayl muvaffaqiyatli saqlandi!"
    }


# ==========================================
# ADMIN QDRANT DELETE (AUDIT BILAN)
# ==========================================
class DeleteQdrantItem(BaseModel):
    id: str

@admin_router.delete("/qdrant/delete")
def delete_from_qdrant(
    request: DeleteQdrantItem,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if not request.id.strip():
        raise HTTPException(status_code=400, detail="O'chirish uchun ID ko'rsatilishi shart!")

    try:
        real_id = int(request.id)
    except ValueError:
        real_id = request.id

    diagrams_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagrams")

    def delete_diagram_by_title(title: str):
        if not title or not os.path.exists(diagrams_dir):
            return
        stem = os.path.splitext(title)[0]
        stem_clean = re.sub(r'[\\/:*?"<>|]', '', stem).strip()
        for ext in ('.png', '.jpg', '.jpeg'):
            img_path = os.path.join(diagrams_dir, f"{stem_clean}{ext}")
            if os.path.exists(img_path):
                os.remove(img_path)
                print(f"Diagram o'chirildi: {img_path}")
                return
        print(f"Diagram topilmadi: {stem_clean}")

    try:
        res = qdrant_client.retrieve(
            collection_name=settings.COLLECTION_NAME,
            ids=[real_id],
            with_payload=True,
            with_vectors=False
        )

        if not res:
            raise HTTPException(status_code=404, detail="O'chirilayotgan ma'lumot topilmadi!")

        target_payload = res[0].payload or {}
        url = target_payload.get("source")
        title = target_payload.get("savol", "")


        if url and str(url).startswith("http"):
            qdrant_client.delete(
                collection_name=settings.COLLECTION_NAME,
                points_selector=rest.Filter(
                    must=[rest.FieldCondition(key="source", match=rest.MatchValue(value=url))]
                )
            )
            delete_diagram_by_title(title)
            write_audit(
                db=db,
                admin_username=admin.username,
                action="o'chirdi",
                title=title or url,
                status="o'chirildi",
                url=url or ""
            )
            return {"status": "success", "message": f"({url}) ga tegishli barcha ma'lumotlar o'chirildi!"}

        else:
            qdrant_client.delete(
                collection_name=settings.COLLECTION_NAME,
                points_selector=rest.PointIdsList(points=[real_id])
            )
            delete_diagram_by_title(title)
            write_audit(
                db=db,
                admin_username=admin.username,
                action="o'chirdi",
                title=title,
                status="o'chirildi",
                url=""
            )
            return {"status": "success", "message": "Tanlangan qism muvaffaqiyatli o'chirildi!"}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"O'chirishda xatolik: {str(e)}")

@admin_router.delete("/qdrant/delete-all")
def delete_all_qdrant(
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    try:
        records, _ = qdrant_client.scroll(
            collection_name=settings.COLLECTION_NAME,
            limit=10000,
            with_payload=True,
            with_vectors=False
        )
        point_ids = [record.id for record in records]

        if point_ids:
            qdrant_client.delete(
                collection_name=settings.COLLECTION_NAME,
                points_selector=rest.PointIdsList(points=point_ids)
            )

        # Diagrams papkasini tozalash
        diagrams_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagrams")
        if os.path.exists(diagrams_dir):
            deleted_count = 0
            for f in os.listdir(diagrams_dir):
                if f.lower().endswith(('.png', '.jpg', '.jpeg')):
                    os.remove(os.path.join(diagrams_dir, f))
                    deleted_count += 1
            print(f"Diagrams dan {deleted_count} ta rasm o'chirildi")
        
        # Super admin uchun umumiy o'chirish
        write_audit(
            db=db,
            admin_username=admin.username,
            action="o'chirdi (barchasi)",
            title="Umumiy baza",
            status="o'chirildi",
            url=""
        )
        
        return {"status": "success", "message": "Barcha ma'lumotlar bazadan butunlay o'chirildi!"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"O'chirishda xatolik: {str(e)}")

class QdrantData(BaseModel):
    title: str
    content: str
    url: str = ""
    admin_username: str = "Noma'lum"

# --- 1. KO'RISH API SI ---
@admin_router.get("/qdrant/list")
def get_qdrant_table_data(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    try:
        records, _ = qdrant_client.scroll(
            collection_name=settings.COLLECTION_NAME, 
            limit=10000, 
            with_payload=True,
            with_vectors=False
        )
        
        table_data = []
        for record in records:
            payload = record.payload or {} 
            point_id = record.id
            
            url = str(payload.get("source") or "Noma'lum manba")
            title = str(payload.get("savol") or "Sarlavha kiritilmagan")
            kontekst = str(payload.get("kontekst") or "")
            holat = str(payload.get("holat") or "Eski")
            boshqargan_admin = str(payload.get("admin") or "Noma'lum")
            
            table_data.append({
                "id": point_id,
                "url": url,
                "title": title,
                "matn_parchasi": kontekst[:150] + "...", 
                "toliq_matn": kontekst,
                "holat": holat,
                "admin": boshqargan_admin
            })
            
        try:
            logs = db.query(KnowledgeLog).order_by(KnowledgeLog.created_at.asc()).all()
            log_order = {log.url: index for index, log in enumerate(logs)}
            table_data.sort(key=lambda x: log_order.get(x.get('url', ''), 999999))
        except Exception as sort_err:
            print(f"Qdrant ma'lumotlarini saralashda xatolik yuz berdi: {sort_err}")
                
        return {"status": "success", "data": table_data}
        
    except Exception as e:
        import traceback
        traceback.print_exc() 
        raise HTTPException(status_code=500, detail=f"Qdrantni o'qishda xatolik: {str(e)}")


@admin_router.post("/qdrant/add")
def add_to_qdrant_db(data: QdrantData, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    exists_in_qdrant = False

    try:
        if data.url and data.url.strip():
            records, _ = qdrant_client.scroll(
                collection_name=settings.COLLECTION_NAME,
                limit=1,
                with_payload=True,
                with_vectors=False,
                scroll_filter=rest.Filter(
                    must=[
                        rest.FieldCondition(
                            key="source",
                            match=rest.MatchValue(value=data.url.strip())
                        )
                    ]
                )
            )
        else:
            records, _ = qdrant_client.scroll(
                collection_name=settings.COLLECTION_NAME,
                limit=1,
                with_payload=True,
                with_vectors=False,
                scroll_filter=rest.Filter(
                    must=[
                        rest.FieldCondition(
                            key="savol",
                            match=rest.MatchValue(value=data.title.strip())
                        )
                    ]
                )
            )

        exists_in_qdrant = len(records) > 0

    except Exception:
        exists_in_qdrant = False


    holat_text = "O'zgargan" if exists_in_qdrant else "Yangi"
    action = "o'zgartirdi" if exists_in_qdrant else "qo'shdi"

    if data.url and data.url.strip():
        qdrant_client.delete(
            collection_name=settings.COLLECTION_NAME,
            points_selector=rest.Filter(
                must=[rest.FieldCondition(key="source", match=rest.MatchValue(value=data.url.strip()))]
            )
        )
    elif data.title and data.title.strip():
        qdrant_client.delete(
            collection_name=settings.COLLECTION_NAME,
            points_selector=rest.Filter(
                must=[rest.FieldCondition(key="savol", match=rest.MatchValue(value=data.title.strip()))]
            )
        )

    chunks = chunk_text(data.content, max_chars=50000)
    points_to_upsert = []
    
    for i, chunk in enumerate(chunks):
        if data.url and data.url.strip():
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{data.url.strip()}_chunk_{i}"))
            source_name = data.url.strip()
        elif data.title and data.title.strip():
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"title_{data.title.strip()}_chunk_{i}"))
            source_name = "Hujjat/Qo'lda kiritilgan"
        else:
            chunk_id = str(uuid.uuid4())
            source_name = "Noma'lum manba"
            
        vector_text = (
            f"passage: Mavzu: {data.title}.\nMatn: {chunk}"
            if data.title and data.title.strip()
            else f"passage: {chunk}"
        )
        vector = embeddings.embed_query(vector_text)
        
        points_to_upsert.append({
            "id": chunk_id,
            "vector": vector,
            "payload": {
                "savol": data.title,
                "kontekst": chunk, 
                "source": source_name,
                "chunk_index": i,
                "holat": holat_text,        
                "admin": admin.username     
            }
        })
        
    qdrant_client.upsert(
        collection_name=settings.COLLECTION_NAME,
        points=points_to_upsert
    )

    write_audit(
    db=db,
    admin_username=admin.username,
    action=action,
    title=data.title or data.url or "Noma'lum",
    status=holat_text,
    url=data.url.strip() if data.url else ""
    )

    return {"status": "success", "message": f"Ma'lumot {len(chunks)} ta qismga bo'linib, Qdrantga saqlandi!"}


# ==========================================
# 3. SUPER ADMIN BO'LIMI
# ==========================================

# Pydantic modellar
class AdminCreate(BaseModel):
    username: str
    password: str

class AdminUpdate(BaseModel):
    new_password: str

class SuperAdminLogin(BaseModel):
    username: str
    password: str

class SuperAdminUpdate(BaseModel):
    new_username: str
    new_password: str

# ==========================================
# SUPER ADMIN HIMOYA (LOGIN / LOGOUT / SETTINGS)
# ==========================================

def get_current_superadmin(superadmin_user: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    if not superadmin_user:
        raise HTTPException(status_code=401, detail="Tizimga kirmagansiz!")
    
    user = db.query(AdminUser).filter(AdminUser.username == superadmin_user, AdminUser.role == "superadmin").first()
    if not user:
        raise HTTPException(status_code=401, detail="Ruxsat etilmagan foydalanuvchi!")
    
    return user

# Login (o'zgarmagan)
@superadmin_router.post("/login")
@limiter.limit("5/minute")
def superadmin_login(request: Request, data: SuperAdminLogin, response: Response, db: Session = Depends(get_db)):
    existing_superadmin = db.query(AdminUser).filter(AdminUser.role == "superadmin").first()
    
    if not existing_superadmin:
        new_super = AdminUser(username=data.username, password=data.password, role="superadmin")
        db.add(new_super)
        db.commit()
        user = new_super
    else:
        user = db.query(AdminUser).filter(
            AdminUser.username == data.username, 
            AdminUser.password == data.password, 
            AdminUser.role == "superadmin"
        ).first()
        if not user:
            raise HTTPException(status_code=401, detail="Login yoki parol noto'g'ri!")

    response.set_cookie(key="superadmin_user", value=user.username, httponly=True, samesite="strict", secure=True, max_age=86400)
    return {"status": "success", "username": user.username}

@superadmin_router.post("/logout")
def superadmin_logout(response: Response):
    response.delete_cookie("superadmin_user")
    return {"status": "success"}

@superadmin_router.get("/me")
def check_superadmin_auth(admin: AdminUser = Depends(get_current_superadmin)):
    return {"username": admin.username, "role": admin.role}

@superadmin_router.post("/settings/update")
def update_superadmin_settings(data: SuperAdminUpdate, response: Response, admin: AdminUser = Depends(get_current_superadmin), db: Session = Depends(get_db)):
    admin.username = data.new_username
    admin.password = data.new_password
    db.commit()
    
    response.set_cookie(key="superadmin_user", value=admin.username, httponly=True, samesite="strict", secure=True)
    return {"status": "success", "message": "Ma'lumotlar muvaffaqiyatli yangilandi!"}

# ====================================================
# SUPER ADMIN PANELI SESSIIYALAR JADVALI API LARI
# ====================================================

@superadmin_router.get("/sessions")
def get_all_sessions_superadmin(admin: AdminUser = Depends(get_current_superadmin), db: Session = Depends(get_db)):
    sessions = db.query(ChatSession).order_by(ChatSession.updated_at.desc()).all()
    
    result = []
    for s in sessions:
        messages = db.query(ChatMessage).filter(
            ChatMessage.session_id == s.session_id
        ).order_by(ChatMessage.created_at).all()
        
        messages_by_month = {}
        for msg in messages:
            if msg.created_at:
                month_key = msg.created_at.strftime("%Y-%m")
                messages_by_month[month_key] = messages_by_month.get(month_key, 0) + 1
        
        result.append({
            "id": s.id,
            "session_id": s.session_id,
            "created_at": s.created_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(s, 'created_at') and s.created_at else "Mavjud emas",
            "updated_at": s.updated_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(s, 'updated_at') and s.updated_at else "Mavjud emas",
            "is_paused": s.is_paused if hasattr(s, 'is_paused') else False,
            "platform": "web",
            "messages_count": len(messages),
            "messages_by_month": messages_by_month
        })
    
    return {"active_sessions": result}

@superadmin_router.get("/sessions/{session_id}")
def get_session_chat_superadmin(session_id: str, admin: AdminUser = Depends(get_current_superadmin), db: Session = Depends(get_db)):
    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at).all()
    
    if not messages:
        raise HTTPException(status_code=404, detail="Bunday suhbat topilmadi!")
        
    chat_history = [
        {
            "sender": msg.sender, 
            "text": msg.text, 
            "time": msg.created_at.strftime("%Y-%m-%d %H:%M:%S") if isinstance(msg.created_at, datetime) else str(msg.created_at)
        } for msg in messages
    ]
    return {"session_id": session_id, "chat_history": chat_history}

@superadmin_router.delete("/sessions/{session_id}")
def delete_session_superadmin(session_id: str, admin: AdminUser = Depends(get_current_superadmin), db: Session = Depends(get_db)):
    db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete()
    db.query(ChatSession).filter(ChatSession.session_id == session_id).delete()
    db.commit()
    return {"status": "success", "message": "Sessiya tizimdan to'liq o'chirildi."}

@superadmin_router.put("/sessions/{session_id}/toggle-pause")
def toggle_session_pause_superadmin(session_id: str, admin: AdminUser = Depends(get_current_superadmin), db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Sessiya topilmadi")
    session.is_paused = not session.is_paused
    db.commit()
    return {"status": "success", "is_paused": session.is_paused}

# ----------------------------------------------------
# ADMINLARNI BOSHQARISH
# ----------------------------------------------------
@superadmin_router.post("/admins/add")
def add_admin(data: AdminCreate, db: Session = Depends(get_db)):
    existing_user = db.query(AdminUser).filter(AdminUser.username == data.username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Bu login band! Boshqa username tanlang.")
    
    new_admin = AdminUser(username=data.username, password=data.password, role="admin")
    db.add(new_admin)
    db.commit()
    return {"status": "success", "message": f"Admin '{data.username}' muvaffaqiyatli yaratildi!"}

@superadmin_router.get("/admins/list")
def get_admins(db: Session = Depends(get_db)):
    admins = db.query(AdminUser).filter(AdminUser.role == "admin").all()
    return {"admins": [{"id": a.id, "username": a.username, "created_at": a.created_at} for a in admins]}

@superadmin_router.put("/admins/{admin_id}/reset-password")
def reset_admin_password(admin_id: int, data: AdminUpdate, db: Session = Depends(get_db)):
    admin = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if not admin:
        raise HTTPException(status_code=404, detail="Admin topilmadi!")
    
    admin.password = data.new_password
    db.commit()
    return {"status": "success", "message": f"'{admin.username}' ning paroli yangilandi!"}

@superadmin_router.delete("/admins/{admin_id}/delete")
def delete_admin(admin_id: int, db: Session = Depends(get_db)):
    admin = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if not admin:
        raise HTTPException(status_code=404, detail="Admin topilmadi!")
    
    db.delete(admin)
    db.commit()
    return {"status": "success", "message": "Admin o'chirildi!"}

@superadmin_router.post("/scrape")
async def scrape_website_superadmin(request: URLRequest, admin: AdminUser = Depends(get_current_superadmin)):
    
    try:
    
        async with httpx.AsyncClient(timeout=15.0) as client:
    
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            response = await client.get(request.url, headers=headers)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            title_tag = soup.find('title')
            page_title = title_tag.get_text(strip=True) if title_tag else ""
            
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.extract()
                
            text = soup.get_text(separator='\n')
            
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            clean_text = '\n'.join(lines)
            
            return {"status": "success", "text": clean_text, "title": page_title}
            
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Saytni o'qib bo'lmadi: {str(e)}")

# ==========================================
# SUPER ADMIN UCHUN: KOP FAYLDAN YUKLASH (AUDIT BILAN)
# ==========================================
@superadmin_router.post("/upload-file")
async def superadmin_upload_file(
    request: Request,
    admin: AdminUser = Depends(get_current_superadmin),
    db: Session = Depends(get_db)
):
    try:
        form = await request.form()
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Form ma'lumotlarini o'qishda xatolik. pip install python-multipart"
        )

    files = form.getlist("files")
    if not files:
        files = form.getlist("file")

    valid_files = [f for f in files if getattr(f, "filename", None)]
    if not valid_files:
        raise HTTPException(status_code=400, detail="Yuklash uchun fayllar topilmadi!")

    success_count = 0
    errors = []

    for file in valid_files:
        try:
            filename_lower = file.filename.lower()
            content = await file.read()
            if not content:
                errors.append(f"{file.filename}: fayl bo'sh")
                continue

            file_stem = get_file_stem(file.filename)
            print(f"\n=== Fayl: {file.filename} | {len(content)} bytes ===")

            if filename_lower.endswith('.pdf'):
                print(f"PDF — faqat rasm ajratiladi, Qdrant ga yozilmaydi")
                result = save_diagram_from_pdf(content, file_stem)
                
                existing = os.path.exists(os.path.join(DIAGRAMS_DIR, f"{file_stem}.png"))
                action = "o'zgartirdi" if existing else "qo'shdi"
                status_text = "O'zgargan" if existing else "Yangi"
                
                # Har safar yangi log
                write_audit(
                    db=db,
                    admin_username=admin.username,
                    action=action,
                    title=file.filename,
                    url=file.filename,
                    status=status_text
                )
                
                success_count += 1
                continue

            if filename_lower.endswith(('.doc', '.docx')):
                class FileLike:
                    def __init__(self, b, name):
                        self.filename = name
                        self._bytes = b
                    async def read(self):
                        return self._bytes

                extracted = await extract_text_from_file(FileLike(content, file.filename))
                text  = extracted["text"]
                title = extracted["title"]

                if not text.strip():
                    errors.append(f"{file.filename}: matn topilmadi")
                    continue

                # ====================== QDRANT ORQALI TEKSHIRUV ======================
                try:
                    clean_title = re.sub(r'\.(pdf|docx?)$', '', title, flags=re.IGNORECASE).strip()
                    records, _ = qdrant_client.scroll(
                        collection_name=settings.COLLECTION_NAME,
                        limit=5,
                        with_payload=True,
                        with_vectors=False,
                        scroll_filter=rest.Filter(          # ← scroll_filter emas, filter bo'lishi kerak!
                            should=[  # OR
                                rest.FieldCondition(key="savol", match=rest.MatchValue(value=title)),
                                rest.FieldCondition(key="savol", match=rest.MatchValue(value=clean_title)),
                            ]
                        )
                    )
                    exists_in_qdrant = len(records) > 0
                except Exception as e:
                    print(f"Qdrant tekshiruv xatosi: {e}")
                    exists_in_qdrant = False
                # ===================================================================================

                holat_text = "O'zgargan" if exists_in_qdrant else "Yangi"
                action = "o'zgartirdi" if exists_in_qdrant else "qo'shdi"

                # Avvalgi ma'lumotni tozalash
                try:
                    qdrant_client.delete(
                        collection_name=settings.COLLECTION_NAME,
                        points_selector=rest.Filter(
                            must=[rest.FieldCondition(
                                key="savol",
                                match=rest.MatchValue(value=title)
                            )]
                        )
                    )
                except Exception:
                    pass

                chunks = chunk_text(text)
                points_to_upsert = []
                for i, chunk in enumerate(chunks):
                    chunk_id = str(uuid.uuid5(
                        uuid.NAMESPACE_DNS, f"title_{title}_chunk_{i}"
                    ))
                    vector = embeddings.embed_query(
                        f"passage: Mavzu: {title}.\nMatn: {chunk}"
                    )
                    points_to_upsert.append({
                        "id": chunk_id,
                        "vector": vector,
                        "payload": {
                            "savol": title,
                            "kontekst": chunk,
                            "source": "Hujjat/Fayl",
                            "chunk_index": i,
                            "holat": holat_text,
                            "admin": admin.username
                        }
                    })

                qdrant_client.upsert(
                    collection_name=settings.COLLECTION_NAME,
                    points=points_to_upsert
                )
                
                # Audit log — har yuklashda yangi yozuv yaratiladi
                write_audit(
                    db=db,
                    admin_username=admin.username,
                    action=action,
                    title=title,
                    url="",           # DOCX uchun url bo'sh
                    status=holat_text
                )
                
                success_count += 1
                continue

            errors.append(f"{file.filename}: faqat PDF, DOC, DOCX qabul qilinadi")
        except Exception as e:
            errors.append(f"{file.filename} da xatolik: {str(e)}")
            print(f"Xato {file.filename}: {e}")

    if errors:
        return {
            "status": "partial",
            "message": f"{success_count} ta fayl saqlandi. Xatoliklar: " + " | ".join(errors)
        }

    return {
        "status": "success",
        "message": f"{success_count} ta fayl muvaffaqiyatli saqlandi!"
    }

# ====================================================
# SUPER ADMIN QDRANT BAZANI BOSHQARISH (AUDIT BILAN)
# ====================================================

@superadmin_router.get("/qdrant/list")
def get_superadmin_qdrant_table_data(admin: AdminUser = Depends(get_current_superadmin), db: Session = Depends(get_db)):
    try:
        records, _ = qdrant_client.scroll(
            collection_name=settings.COLLECTION_NAME, 
            limit=10000, 
            with_payload=True,
            with_vectors=False
        )
        
        table_data = []
        for record in records:
            payload = record.payload or {} 
            point_id = record.id
            
            url = str(payload.get("source") or "Noma'lum manba")
            title = str(payload.get("savol") or "Sarlavha kiritilmagan")
            kontekst = str(payload.get("kontekst") or "")
            holat = str(payload.get("holat") or "Eski")
            boshqargan_admin = str(payload.get("admin") or "Noma'lum")
            
            table_data.append({
                "id": point_id,
                "url": url,
                "title": title,
                "matn_parchasi": kontekst[:150] + "...", 
                "toliq_matn": kontekst,
                "holat": holat,
                "admin": boshqargan_admin
            })
            
        try:
            logs = db.query(KnowledgeLog).order_by(KnowledgeLog.created_at.asc()).all()
            log_order = {log.url: index for index, log in enumerate(logs)}
            table_data.sort(key=lambda x: log_order.get(x.get('url', ''), 999999))
        except Exception as sort_err:
            print(f"Qdrant ma'lumotlarini saralashda xatolik yuz berdi: {sort_err}")
                
        return {"status": "success", "data": table_data}
        
    except Exception as e:
        import traceback
        traceback.print_exc() 
        raise HTTPException(status_code=500, detail=f"Qdrantni o'qishda xatolik: {str(e)}")


@superadmin_router.post("/qdrant/add")
def add_to_qdrant_superadmin(data: QdrantData, admin: AdminUser = Depends(get_current_superadmin), db: Session = Depends(get_db)):
    exists_in_qdrant = False

    try:
        if data.url and data.url.strip():
            records, _ = qdrant_client.scroll(
                collection_name=settings.COLLECTION_NAME,
                limit=1,
                with_payload=True,
                with_vectors=False,
                scroll_filter=rest.Filter(
                    must=[
                        rest.FieldCondition(
                            key="source",
                            match=rest.MatchValue(value=data.url.strip())
                        )
                    ]
                )
            )
        else:
            records, _ = qdrant_client.scroll(
                collection_name=settings.COLLECTION_NAME,
                limit=1,
                with_payload=True,
                with_vectors=False,
                scroll_filter=rest.Filter(
                    must=[
                        rest.FieldCondition(
                            key="savol",
                            match=rest.MatchValue(value=data.title.strip())
                        )
                    ]
                )
            )

        exists_in_qdrant = len(records) > 0

    except Exception:
        exists_in_qdrant = False


    holat_text = "O'zgargan" if exists_in_qdrant else "Yangi"
    action = "o'zgartirdi" if exists_in_qdrant else "qo'shdi"

    if data.url and data.url.strip():
        qdrant_client.delete(
            collection_name=settings.COLLECTION_NAME,
            points_selector=rest.Filter(
                must=[rest.FieldCondition(key="source", match=rest.MatchValue(value=data.url.strip()))]
            )
        )
    elif data.title and data.title.strip():
        qdrant_client.delete(
            collection_name=settings.COLLECTION_NAME,
            points_selector=rest.Filter(
                must=[rest.FieldCondition(key="savol", match=rest.MatchValue(value=data.title.strip()))]
            )
        )

    chunks = chunk_text(data.content, max_chars=50000)
    points_to_upsert = []
    
    for i, chunk in enumerate(chunks):
        if data.url and data.url.strip():
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{data.url.strip()}_chunk_{i}"))
            source_name = data.url.strip()
        elif data.title and data.title.strip():
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"title_{data.title.strip()}_chunk_{i}"))
            source_name = "Hujjat/Qo'lda kiritilgan"
        else:
            chunk_id = str(uuid.uuid4())
            source_name = "Noma'lum manba"
            
        vector_text = (
            f"passage: Mavzu: {data.title}.\nMatn: {chunk}"
            if data.title and data.title.strip()
            else f"passage: {chunk}"
        )
        vector = embeddings.embed_query(vector_text)
        
        points_to_upsert.append({
            "id": chunk_id,
            "vector": vector,
            "payload": {
                "savol": data.title,
                "kontekst": chunk, 
                "source": source_name,
                "chunk_index": i,
                "holat": holat_text,        
                "admin": admin.username     
            }
        })
        
    qdrant_client.upsert(
        collection_name=settings.COLLECTION_NAME,
        points=points_to_upsert
    )

    write_audit(
    db=db,
    admin_username=admin.username,
    action=action,
    title=data.title or data.url or "Noma'lum",
    status=holat_text,
    url=data.url.strip() if data.url else ""
    )

    return {"status": "success", "message": f"Ma'lumot {len(chunks)} ta qismga bo'linib, Qdrantga saqlandi!"}


@superadmin_router.delete("/qdrant/delete-all")
def superadmin_delete_all_qdrant(
    admin: AdminUser = Depends(get_current_superadmin),
    db: Session = Depends(get_db)
):
    try:
        records, _ = qdrant_client.scroll(
            collection_name=settings.COLLECTION_NAME,
            limit=10000,
            with_payload=True,
            with_vectors=False
        )
        point_ids = [record.id for record in records]

        if point_ids:
            qdrant_client.delete(
                collection_name=settings.COLLECTION_NAME,
                points_selector=rest.PointIdsList(points=point_ids)
            )

        # Diagrams papkasini tozalash
        diagrams_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagrams")
        if os.path.exists(diagrams_dir):
            deleted_count = 0
            for f in os.listdir(diagrams_dir):
                if f.lower().endswith(('.png', '.jpg', '.jpeg')):
                    os.remove(os.path.join(diagrams_dir, f))
                    deleted_count += 1
            print(f"Diagrams dan {deleted_count} ta rasm o'chirildi")
        
        # Super admin uchun umumiy o'chirish
        write_audit(
            db=db,
            admin_username=admin.username,
            action="o'chirdi (barchasi)",
            title="Umumiy baza",
            status="o'chirildi",
            url=""
        )
        
        return {"status": "success", "message": "Barcha ma'lumotlar bazadan butunlay o'chirildi!"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"O'chirishda xatolik: {str(e)}")

@superadmin_router.delete("/qdrant/delete")
def superadmin_delete_from_qdrant(
    request: DeleteQdrantItem,
    admin: AdminUser = Depends(get_current_superadmin),
    db: Session = Depends(get_db)
):
    if not request.id.strip():
        raise HTTPException(status_code=400, detail="O'chirish uchun ID ko'rsatilishi shart!")

    try:
        real_id = int(request.id)
    except ValueError:
        real_id = request.id

    diagrams_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagrams")

    def delete_diagram_by_title(title: str):
        if not title or not os.path.exists(diagrams_dir):
            return
        stem = os.path.splitext(title)[0]
        stem_clean = re.sub(r'[\\/:*?"<>|]', '', stem).strip()
        for ext in ('.png', '.jpg', '.jpeg'):
            img_path = os.path.join(diagrams_dir, f"{stem_clean}{ext}")
            if os.path.exists(img_path):
                os.remove(img_path)
                print(f"Diagram o'chirildi: {img_path}")
                return
        print(f"Diagram topilmadi: {stem_clean}")

    try:
        res = qdrant_client.retrieve(
            collection_name=settings.COLLECTION_NAME,
            ids=[real_id],
            with_payload=True,
            with_vectors=False
        )

        if not res:
            raise HTTPException(status_code=404, detail="O'chirilayotgan ma'lumot topilmadi!")

        target_payload = res[0].payload or {}
        url = target_payload.get("source")
        title = target_payload.get("savol", "")


        if url and str(url).startswith("http"):
            qdrant_client.delete(
                collection_name=settings.COLLECTION_NAME,
                points_selector=rest.Filter(
                    must=[rest.FieldCondition(key="source", match=rest.MatchValue(value=url))]
                )
            )
            delete_diagram_by_title(title)
            write_audit(
                db=db,
                admin_username=admin.username,
                action="o'chirdi",
                title=title or url,
                status="o'chirildi",
                url=url or ""
            )
            return {"status": "success", "message": f"({url}) ga tegishli barcha ma'lumotlar o'chirildi!"}

        else:
            qdrant_client.delete(
                collection_name=settings.COLLECTION_NAME,
                points_selector=rest.PointIdsList(points=[real_id])
            )
            delete_diagram_by_title(title)
            write_audit(
                db=db,
                admin_username=admin.username,
                action="o'chirdi",
                title=title,
                status="o'chirildi",
                url=""
            )
            return {"status": "success", "message": "Tanlangan qism muvaffaqiyatli o'chirildi!"}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"O'chirishda xatolik: {str(e)}")


@superadmin_router.delete("/qdrant/logs/clear-all")
def clear_all_logs(admin: AdminUser = Depends(get_current_superadmin), db: Session = Depends(get_db)):
    """Barcha loglarni butunlay o'chirish"""
    try:
        deleted_count = db.query(KnowledgeLog).delete()
        db.commit()
        
        print(f"🧹 BARCHA LOG LAR TOZALANDI: {deleted_count} ta yozuv o'chirildi")
        
        return {
            "status": "success", 
            "message": f"Barcha loglar tozalandi ({deleted_count} ta yozuv o'chirildi)."
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Loglarni tozalashda xatolik: {str(e)}")


@superadmin_router.get("/qdrant/logs/filter")
def filter_logs(
    title: Optional[str] = Query(None),
    admin: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    query = db.query(KnowledgeLog)

    if title:
        query = query.filter(KnowledgeLog.title.ilike(f"%{title}%"))
        
    if admin:
        # Admin nomini ham ilike qilish xavfsizroq (katta-kichik harf muammosi bo'lmaydi)
        query = query.filter(KnowledgeLog.added_by.ilike(admin))
        
    if status:
        # Apostroflarni normallashtiramiz (agar har xil yozilgan bo'lsa)
        normalized_status = status.replace("`", "'")
        # ilike yordamida "o'chirildi" va "O'chirildi" variantlarini birdek topadi
        query = query.filter(KnowledgeLog.status.ilike(normalized_status))

    logs = query.order_by(KnowledgeLog.created_at.desc()).all()

    return {"logs": [{
        "id": log.id,
        "url": log.url,
        "title": log.title,
        "added_by": log.added_by,
        "status": log.status,
        "created_at": log.created_at  # Front-end shu kalitni o'qiyotganini tekshiring
    } for log in logs]}

# QDRANT LOGS

@superadmin_router.get("/qdrant/logs")
def get_qdrant_logs(
    title: Optional[str] = Query(None),
    admin: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    query = db.query(KnowledgeLog)

    # 1. Sarlavha bo'yicha qidirish (Katta-kichik harfni ajratmaydi)
    if title:
        query = query.filter(KnowledgeLog.title.ilike(f"%{title}%"))
        
    # 2. Mas'ul admin bo'yicha filtrlash
    if admin:
        query = query.filter(KnowledgeLog.added_by.ilike(admin))
        
    # 3. Amal turi (Status) bo'yicha filtrlash (Apostrof va registrlardan xavfsiz)
    if status:
        clean_status = status.replace("`", "'").strip()
        query = query.filter(KnowledgeLog.status.ilike(clean_status))

    # Eng yangi loglar har doim tepada keladi
    logs = query.order_by(KnowledgeLog.created_at.desc()).all()
    
    return {"logs": [{
        "id": log.id,
        "url": log.url,
        "title": log.title,
        "added_by": log.added_by,
        "status": log.status,
        "created_at": log.created_at.strftime("%Y-%m-%d %H:%M:%S") if log.created_at else None
    } for log in logs]}