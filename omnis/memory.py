import os
import time
import uuid
import logging
from typing import Any

logger = logging.getLogger("omnis.memory")

try:
    import chromadb
    from chromadb.config import Settings
    # 尝试显式导入 onnxruntime 确认其可用
    import onnxruntime
    HAS_CHROMADB = True
except ImportError as e:
    logger.warning(f"ChromaDB 或其依赖导入失败: {e}")
    HAS_CHROMADB = False

class EpisodicMemory:
    def __init__(self, persist_directory: str = "./omnis_chroma_db"):
        if not HAS_CHROMADB:
            return
            
        self.persist_directory = persist_directory
        os.makedirs(self.persist_directory, exist_ok=True)
        try:
            # 初始化 ChromaDB 客户端 (本地持久化)
            self.client = chromadb.PersistentClient(path=self.persist_directory)
            # 获取或创建名为 "omnis_episodic_memory" 的集合
            self.collection = self.client.get_or_create_collection(
                name="omnis_episodic_memory",
                metadata={"hnsw:space": "cosine"} # 使用余弦相似度
            )
        except Exception as e:
            logger.error(f"初始化 ChromaDB 失败: {e}")
            self.client = None
            self.collection = None

    def add_memory(self, session_id: str, agent_name: str, tick: int, location: str, intent: str, content: str):
        """将角色的一条经历存入向量数据库"""
        if getattr(self, 'collection', None) is None:
            return
            
        memory_id = f"{session_id}_{agent_name}_{tick}_{uuid.uuid4().hex[:8]}"
        
        try:
            self.collection.add(
                documents=[content],
                metadatas=[{
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "tick": tick,
                    "location": location,
                    "intent": intent,
                    "timestamp": time.time()
                }],
                ids=[memory_id]
            )
        except Exception as e:
            logger.error(f"Error adding memory to ChromaDB: {e}")

    def retrieve_memory(self, session_id: str, agent_name: str, query_text: str, n_results: int = 3) -> list[str]:
        """根据查询文本检索该角色最相关的历史经历"""
        if getattr(self, 'collection', None) is None:
            return []
            
        try:
            results = self.collection.query(
                query_texts=[query_text],
                n_results=n_results,
                where={
                    "$and": [
                        {"session_id": {"$eq": session_id}},
                        {"agent_name": {"$eq": agent_name}}
                    ]
                }
            )
            
            if results and results['documents'] and results['documents'][0]:
                return results['documents'][0]
            return []
        except Exception as e:
            logger.error(f"Error retrieving memory from ChromaDB: {e}")
            return []

# 全局单例
_episodic_memory_instance = None

def get_episodic_memory() -> EpisodicMemory:
    global _episodic_memory_instance
    if _episodic_memory_instance is None:
        db_path = os.getenv("OMNIS_CHROMA_DB_PATH", "./omnis_chroma_db")
        _episodic_memory_instance = EpisodicMemory(persist_directory=db_path)
    return _episodic_memory_instance
