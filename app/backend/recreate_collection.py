import sys, os
sys.path.insert(0, '/app')

from backend.config import settings
from backend.rag import embeddings
from qdrant_client import QdrantClient, models

host = settings.QDRANT_HOST
if not host.startswith("http"):
    host = f"http://{host}"

client = QdrantClient(url=host)
collection = settings.COLLECTION_NAME

print("Embedding o'lchami aniqlanmoqda...")
dim = len(embeddings.embed_query("test"))
print(f"O'lcham: {dim}")

existing = [c.name for c in client.get_collections().collections]
if collection in existing:
    print(f"'{collection}' o'chirilmoqda...")
    client.delete_collection(collection)
    print("O'chirildi.")

print(f"'{collection}' TurboQuant 4-bit bilan yaratilmoqda...")
client.create_collection(
    collection_name=collection,
    vectors_config=models.VectorParams(
        size=dim,
        distance=models.Distance.COSINE,
    ),
    quantization_config=models.TurboQuantization(
        turbo=models.TurboQuantQuantizationConfig(
            always_ram=True,
            bits=models.TurboQuantBitSize.BITS4,
        ),
    ),
)
print("Tayyor! Endi admin paneldan fayllarni qayta yuklang.")
