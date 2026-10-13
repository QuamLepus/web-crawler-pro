"""Advanced AI Layer — autonomous agents, RAG, curation & synthesis.

Subpackages/modules:
    * :mod:`src.ai.agents.agent_crawler`  — LangChain ReAct agent crawler
      (heuristic offline brain + Stealth-Engine-backed fetching ladder).
    * :mod:`src.ai.agents.rag_chain`      — ChromaDB / Qdrant / in-memory RAG
      retrieval-augmented QA over crawled corpora.
    * :mod:`src.ai.agents.data_curator`   — GPT-4 LLM-as-judge quality scoring.
    * :mod:`src.ai.agents.synthetic`      — synthetic data generation pipeline.
    * :mod:`src.ai.vector_search`         — semantic search (ChromaDB).
    * :mod:`src.ai.llm_parser`            — schema-driven LLM HTML extraction.

Heavy deps (langchain / chromadb / openai) are lazily imported; every module
ships an offline fallback so CI runs hermetically.
"""

from src.ai.agents import (
    AgentCrawler,
    CrawlAction,
    DataCurator,
    QualityScore,
    RAGAnswer,
    RAGChain,
    SyntheticDataGenerator,
    SyntheticExample,
    build_rag_chain,
    crawl_site,
    curate_records,
    generate_paraphrases,
    generate_qa_pairs,
)
from src.ai.llm_parser import LLMParser, slim_html
from src.ai.vector_search import SemanticSearch

__all__ = [
    "AgentCrawler",
    "CrawlAction",
    "DataCurator",
    "LLMParser",
    "QualityScore",
    "RAGAnswer",
    "RAGChain",
    "SemanticSearch",
    "SyntheticDataGenerator",
    "SyntheticExample",
    "build_rag_chain",
    "crawl_site",
    "curate_records",
    "generate_paraphrases",
    "generate_qa_pairs",
    "slim_html",
]
