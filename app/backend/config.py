import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    VLLM_URL: str = os.getenv("VLLM_URL", "http://host.docker.internal:8050/v1")
    QDRANT_HOST: str = os.getenv("QDRANT_HOST", "http://qdrant:6333")
    POSTGRES_HOST: str = os.getenv("POSTGRES_HOST", "postgres")
    POSTGRES_DB: str = os.getenv("POSTGRES_DB", "brb_chatbot")
    POSTGRES_USER: str = os.getenv("POSTGRES_USER", "brb_user")
    POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "brb_password123")
    
    MODEL_NAME: str = "brb_bank_model"
    EMBEDDING_MODEL: str = "intfloat/multilingual-e5-large"
    COLLECTION_NAME: str = "collection2"

settings = Settings()
