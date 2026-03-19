from __future__ import annotations

from app.models.enums import MemoryType
from app.models.memory import MemoryItem
from app.services.memory import MemoryService, cosine_similarity
from app.services.prompt import PromptService


class FakeOpenAIProvider:
    enabled = False


def test_memory_heuristic_extraction(settings):
    service = MemoryService(settings, FakeOpenAIProvider(), PromptService(settings))
    facts = service._heuristic_facts("I like jasmine tea and quiet mornings.")
    assert len(facts) == 1
    assert "jasmine tea" in facts[0]["content"]


def test_python_vector_retrieval_fallback(settings):
    service = MemoryService(settings, FakeOpenAIProvider(), PromptService(settings))
    item_a = MemoryItem(memory_type=MemoryType.fact, content="likes tea", importance_score=0.5, embedding_vector=[1.0, 0.0])
    item_b = MemoryItem(memory_type=MemoryType.fact, content="likes chess", importance_score=0.5, embedding_vector=[0.0, 1.0])
    results = service._retrieve_python([item_a, item_b], [0.9, 0.1], top_k=2, threshold=0.1)
    assert results[0].memory.content == "likes tea"
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
