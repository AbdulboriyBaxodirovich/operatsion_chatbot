from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from backend.config import settings
from backend.routers import chat_router, admin_router, superadmin_router
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from fastapi.middleware.cors import CORSMiddleware
from backend.limiter import limiter 

app = FastAPI(
    title="BRB Bank Chatbot API",
    description="BRB Bank uchun RAG arxitekturali chatbot va Admin API",
    version="1.0.0"
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

origins = [
    "http://localhost:8080"    # DIQQAT: Sayt internetga chiqqanda shuni qo'shasiz!
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,        # 1. Tansoqchining VIP ro'yxati ("*" qilib qo'yish o'ta xavfli!)
    allow_credentials=True,       # 2. Sessiya (Cookie)lar ishlashi uchun ruxsat berish
    allow_methods=["GET", "POST", "PUT", "DELETE"], # 3. Faqat shu turdagi amallarga ruxsat
    allow_headers=["Content-Type", "Accept", "Authorization", "X-Requested-With"],          # 4. Brauzer yuboradigan qo'shimcha ma'lumotlarga ruxsat
)

# Routerlarni ulash
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(superadmin_router)

# ---------------------------------------------------------
# Yozilgan HTML fayllarni FastAPI orqali ulash
# ---------------------------------------------------------
app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/")
def serve_chat_ui():
    """Asosiy sahifaga kirganda index.html ni ochish"""
    return FileResponse("frontend/index.html")

@app.get("/admin-panel")
def serve_admin_ui():
    """Admin sahifasiga kirganda admin.html ni ochish"""
    return FileResponse("frontend/admin.html")
# ---------------------------------------------------------

@app.get("/superadmin-panel")
def serve_superadmin_ui():
    """Super Admin sahifasiga kirganda superadmin.html ni ochish"""
    return FileResponse("frontend/superadmin.html")

@app.get("/health")
def health_check():
    """Tizim ishlashini tekshirish uchun"""
    return {
        "status": "Tizim ishlamoqda", 
        "vllm_url": settings.VLLM_URL,
        "qdrant_host": settings.QDRANT_HOST
    }