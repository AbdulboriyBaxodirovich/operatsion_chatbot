import httpx
from backend.config import settings
from backend.prompts import REWRITE_QUERY_PROMPT
# VLLM manzilini o'zingizning sozlamalaringizga qarab qo'yasiz
# Agar settings.py faylingiz bo'lsa: from config import VLLM_URL qilsangiz ham bo'ladi.

async def rewrite_query(chat_history: list, current_query: str) -> str:
    """Suhbat tarixiga qarab savolni to'liq shaklga keltiradi"""
    
    if not chat_history:
        return current_query

    # Oxirgi 4 ta xabarni olamiz
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in chat_history[-10:]])
    
    prompt = f"{REWRITE_QUERY_PROMPT}\n\nTarix:\n{history_text}\n\nYangi savol: {current_query}\n\nTo'liq shakli:"
    
    full_url = f"{settings.VLLM_URL.rstrip('/')}/chat/completions"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(full_url, json={
                "model": settings.MODEL_NAME, # O'zingizning model nomingizni yozing
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 150
            })
            resp.raise_for_status()
            data = resp.json()
            rewritten_query = data['choices'][0]['message']['content'].strip()
            
            print(f"Asl savol: {current_query} | To'g'rilangan: {rewritten_query}")
            return rewritten_query
    except Exception as e:
        print(f"Savolni to'g'rilashda xatolik: {e}")
        return current_query # Agar xatolik bo'lsa, dastur to'xtab qolmasligi uchun asl savolni qaytaramiz