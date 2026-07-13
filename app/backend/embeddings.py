"""
BRB Bank Chatbot - Embeddings Module
Multilingual E5 modelini boshqarish
"""

from sentence_transformers import SentenceTransformer
import torch

class EmbeddingModel:
    def __init__(self, model_name: str = "intfloat/multilingual-e5-large-instruct"):
        print(f"Embedding model yuklanmoqda: {model_name}")
        self.model = SentenceTransformer(model_name)
        self.model.eval()                    # Evaluation rejimiga o'tkazish
        print("Embedding model muvaffaqiyatli yuklandi!")

    def get_embedding(self, text: str):
        """Matndan embedding vektorni qaytaradi"""
        if not text or not text.strip():
            return None
        
        # Embedding yaratish
        embedding = self.model.encode(
            text,
            convert_to_tensor=True,
            normalize_embeddings=True,      # Cosine similarity uchun yaxshi
            show_progress_bar=False
        )
        
        # Tensor ni list ga aylantirish
        return embedding.cuda().numpy().tolist()


# Global embedding instance (bitta marta yuklanadi)
embedder = EmbeddingModel()

def get_embedding(text: str):
    """Tashqaridan chaqirish uchun qulay funksiya"""
    return embedder.get_embedding(text)


print("Embeddings modul muvaffaqiyatli yuklandi!")
