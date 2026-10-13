"""Autonomous AI agents for WebCrawlerPro (Advanced AI Layer).

Modules:
    * :mod:`src.ai.agents.agent_crawler` — LangChain ReAct agent that crawls
      and extracts structured data autonomously.
    * :mod:`src.ai.agents.rag_chain`     — ChromaDB/Qdrant retrieval-
      augmented QA over crawled corpora.
    * :mod:`src.ai.agents.data_curator`  — GPT-4 quality scoring of records.
    * :mod:`src.ai.agents.synthetic`     — synthetic training-data pipeline.

All heavy dependencies (langchain, chromadb, openai) are imported lazily so
the base install / CI stays lightweight; offline fallbacks keep every module
unit-testable without network access.
"""

from src.ai.agents.agent_crawler import AgentCrawler, CrawlAction, crawl_site
from src.ai.agents.rag_chain import RAGChain, build_rag_chain
from src.ai.agents.data_curator import DataCurator, QualityScore, curate_records
from src.ai.agents.synthetic import (
    SyntheticDataGenerator,
    generate_qa_pairs,
    generate_paraphrases,
)

__all__ = [
    "AgentCrawler",
    "CrawlAction",
    "crawl_site",
    "RAGChain",
    "build_rag_chain",
    "DataCurator",
    "QualityScore",
    "curate_records",
    "SyntheticDataGenerator",
    "generate_qa_pairs",
    "generate_paraphrases",
]
