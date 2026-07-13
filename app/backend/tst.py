import sys
import os
os.environ["QDRANT_HOST"] = "http://localhost:6333"  # ← shu qatorni qo'shing

sys.path.insert(0, '/home/abdulboriy/brb-bank-chatbot/app')

from backend.rag import search_knowledge_base

context = search_knowledge_base("A1.2.3.10 Korporativ biznes mijozlariga kredit mahsuloti(Qo'mita qarori asosida)")
print("=== KONTEKST ===")
print(context)
print("=== KONTEKST TUGADI ===")