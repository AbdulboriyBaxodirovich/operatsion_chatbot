from langchain_huggingface import HuggingFaceEmbeddings
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
import httpx
from backend.config import settings
import re
from backend.prompts import SYSTEM_PROMPT
from typing import List, Tuple, Optional
import os

# 1. E5-large modelini yuklash
print(f"Embedding model ({settings.EMBEDDING_MODEL}) yuklanmoqda...")
embeddings = HuggingFaceEmbeddings(
    model_name=settings.EMBEDDING_MODEL, 
    encode_kwargs={
        "normalize_embeddings": True,
        "batch_size": 64
    }
)

# 2. vLLM modelini ulash
llm = ChatOpenAI(
    model=settings.MODEL_NAME,
    base_url=settings.VLLM_URL,
    api_key="EMPTY",  
    temperature=0.1, 
    max_tokens=2048,
    model_kwargs={"top_p": 0.9}
)

 
def extract_relevant_chunks(text: str, query: str, max_sentences: int = 3) -> str:
    """
    Savolga eng mos kelgan qismlarni va ularning ma'nosi yo'qolmasligi uchun 
    o'zidan oldingi va keyingi qatorlarni (chunklarni) ham qo'shib ajratib oladi.
    """
    # 1. Matnni qismlarga ajratish (eski logikamiz)
    raw_sentences = re.split(r'(?<=[.!?])\s+|\n+|\r+|<br/?>|<p>| {3,}', text)
    
    sentences = []
    for s in raw_sentences:
        cleaned = s.strip()
        if len(cleaned) > 10:
            if len(cleaned) <= 1500:
                sentences.append(cleaned)
            else:
                for i in range(0, len(cleaned), 1500):
                    part = cleaned[i:i+1500]
                    if len(part) > 10:
                        sentences.append(part)
            
    # 2. Savoldagi so'zlarni ajratib olish
    query_words = set(re.findall(r'\w+', query.lower()))
    
    # MUHIM O'ZGARISH: Endi biz faqat matnni emas, uning INKDESINI (tartib raqamini) ham saqlaymiz
    scored_sentences = []
    for i, sentence in enumerate(sentences):
        sentence_words = set(re.findall(r'\w+', sentence.lower()))
        score = len(query_words.intersection(sentence_words))
        
        if score > 0 or len(sentences) <= max_sentences:
            scored_sentences.append((score, i)) # i - bu gapning tartib raqami
    
    if not scored_sentences:
        return " ... ".join(sentences[:max_sentences])
    
    # 3. Eng yuqori ball olgan qismlarni saralash
    scored_sentences.sort(key=lambda x: x[0], reverse=True)
    
    # 4. Eng zo'r `max_sentences` ta qismning indekslarini (tartib raqamlarini) olamiz
    best_indices = [item[1] for item in scored_sentences[:max_sentences]]
    
    # 5. O'zidan oldingi va keyingi qismlarni (qo'shnilarni) yig'ish
    # Set (to'plam) ishlatamiz, chunki ikkita zo'r gap yonma-yon bo'lsa, takrorlanib qolmasligi kerak
    context_indices = set()
    for idx in best_indices:
        if idx > 0:
            context_indices.add(idx - 1)  # Oldingi qismni qo'shish
        context_indices.add(idx)          # O'zini qo'shish
        if idx < len(sentences) - 1:
            context_indices.add(idx + 1)  # Keyingi qismni qo'shish
            
    # 6. Indekslarni o'sish tartibida joylashtiramiz (matn ketma-ketligi buzilmasligi uchun)
    sorted_indices = sorted(list(context_indices))
    
    # 7. Tartiblangan indekslar asosida yakuniy matnni yig'amiz
    best_sentences = [sentences[i] for i in sorted_indices]
    
    return " \n... ".join(best_sentences)

def search_knowledge_base(query: str, k: int = 2) -> tuple[str, Optional[str]]:
    """HTTPX yordamida Qdrant API'ga to'g'ridan-to'g'ri (REST) murojaat qilish va (kontekst, top_mavzu) qaytarish"""
    try:
        # 1. Mijoz savolini E5 uchun formatlab vektorga aylantiramiz
        e5_query = f"query: {query}"
        query_vector = embeddings.embed_query(e5_query)
        
        # 2. Qdrant URL manzilini to'g'rilash
        host = settings.QDRANT_HOST
        if not host.startswith("http"):
            host = f"http://{host}"
        url = f"{host.rstrip('/')}/collections/{settings.COLLECTION_NAME}/points/search"
        
        # 3. Qdrant'ga to'g'ridan-to'g'ri JSON so'rov yuboramiz
        payload = {
            "vector": query_vector,
            "limit": k,
            "with_payload": True
        }
        
        with httpx.Client() as client:
            response = client.post(url, json=payload, timeout=15.0)
            response.raise_for_status()
            data = response.json()
            
        kontekstlar = []
        hits = data.get("result", [])
        
        # =========================================================================
        # ✨ YANGI QO'SHIMCHA: CHIZMA UCHUN TOP-1 MAVZUNI (SAVOLNI) ANIQLASH
        # =========================================================================
        top_topic = None
        if hits:
            best_hit = hits[0]
            best_score = best_hit.get("score", 0.0)
            
            # Chegara ball (Threshold): Foydalanuvchi "rahmat" yoki "salom" deb yozganda 
            # past score'li umuman boshqa chizma ochilib ketmasligi uchun tekshiramiz.
            # E5-large modeli uchun 0.70 va undan yuqori ball juda ishonchli hisoblanadi.
            if best_score >= 0.70:
                top_topic = best_hit.get("payload", {}).get("savol", None)
        # =========================================================================
        
        # 4. Qaytib kelgan natijalarni ajratib olamiz (Sizning original RAG logikangiz)
        for hit in hits:
            hit_payload = hit.get("payload", {})
            
            topilgan_savol = hit_payload.get("savol", "Noma'lum savol")
            asl_kontekst = hit_payload.get("kontekst", "")
            manba = hit_payload.get("source", "Manba yo'q")
            score = hit.get("score", 0.0)
            
            if asl_kontekst:
                # ==========================================
                # AQLLI TEKSHIRUV: Matn qaysi avlodga tegishli?
                # ==========================================
                if len(asl_kontekst) <= 35000:
                    
                    # 1. YANGI AVLOD (Qisqa va tayyor chunklar)
                    tayyor_matn = f"[Manba: {manba}]\n{asl_kontekst}"
                    kontekstlar.append(tayyor_matn)
                    
                else:
                    # 2. ESKI AVLOD (20,000 belgilik ulkan matnlar)
                    qisqa_kontekst = extract_relevant_chunks(asl_kontekst, query, max_sentences=3)
                    tayyor_matn = f"[Manba: {manba}]\n{qisqa_kontekst}"
                    kontekstlar.append(tayyor_matn)
            
            print(f"📌 Score: {score:.3f} | Uzunlik: {len(asl_kontekst)} | 🔗 {manba}")
            print(f"📌 Savol: {topilgan_savol} | Score: {score:.3f} | 🔗 {manba}")
            
        context_text = "\n\n---\n\n".join(kontekstlar) if kontekstlar else "Qo'shimcha ma'lumot topilmadi."
        
        # RAG uchun matnni va chizma tizimi uchun top mavzuni birgalikda qaytaramiz
        return context_text, top_topic
        
    except Exception as e:
        print(f"Qdrant REST API qidiruv xatosi: {e}")
        # Xatolik yuz berganda RAG buzilmasligi uchun bo'sh matn va None qaytariladi
        return "", None

def ask_vllm_model(user_message: str, context: str, chat_history: list) -> str:
    """VLLM ga mijoz savoli va qidirib topilgan kontekstni yuborish (Token limit bilan)"""
    
    # MUHIM YECHIM: Agar kontekst juda uzun bo'lsa, uni kesib tashlaymiz
    # 15000 ta belgi taxminan 3000-4000 tokenni tashkil qiladi (model bemalol o'qiydi)
    max_chars = 35000
    if len(context) > max_chars:
        print(f"Kontekst juda uzun ({len(context)} belgi), kesilmoqda...")
        context = context[:max_chars]  # Oxirgi qismni saqlaymiz, chunki u eng relevant bo'lishi mumkin

    instruction = f"{SYSTEM_PROMPT}\n\nKONTEKST:\n{context}\n\nSAVOL: {user_message}"

    messages = []
    
    # Chat tarixini saqlaymiz
    for msg in chat_history:
        # msg.sender emas, msg["role"] orqali o'qiladi
        role = "human" if msg["role"] == "user" else "assistant"
        text = msg["content"]  # msg.text emas, msg["content"]
        
    messages.append(("human", instruction))
    
    prompt = ChatPromptTemplate.from_messages(messages)
    chain = prompt | llm
    
    try:
        response = chain.invoke({}) 
        return response.content
    except Exception as e:
        print(f"vLLM API xatosi: {e}")
        return "Uzr, ayni vaqtda bot tarmog'ida uzilish mavjud."