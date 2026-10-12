"""LLM-powered intelligent parser (LangChain).

Given raw HTML and a desired JSON schema, the LLM extracts structured
data WITHOUT hand-written CSS selectors. Uses an "HTML slimming" step
first to keep token costs down.

Requires: langchain, langchain-openai (or any ChatModel), OPENAI_API_KEY.
"""

from __future__ import annotations

import json
import os
import re

PROMPT_TEMPLATE = """You are a web data extraction engine. \
From the HTML below, extract data matching this JSON schema:
{schema}

Return ONLY valid JSON matching the schema. If a field is not present, use null.

HTML:
<html>{html}</html>
"""


def slim_html(html: str, max_chars: int = 12000) -> str:
    """Strip scripts/styles/comments and compress whitespace to save tokens."""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html,
                  flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    html = re.sub(r">\s+<", "><", html)
    return html[:max_chars]


class LLMParser:
    def __init__(self, model: str | None = None, temperature: float = 0.0):
        self.model_name = model or os.getenv("LLM_MODEL", "gpt-4o-mini")
        self.temperature = temperature
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            from langchain_openai import ChatOpenAI  # lazy import

            self._llm = ChatOpenAI(model=self.model_name,
                                   temperature=self.temperature)
        return self._llm

    def parse(self, html: str, schema: dict) -> dict:
        """Extract structured JSON from HTML according to `schema`."""
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import JsonOutputParser

        prompt = ChatPromptTemplate.from_template(PROMPT_TEMPLATE)
        chain = prompt | self.llm | JsonOutputParser()
        return chain.invoke({"schema": json.dumps(schema, indent=2),
                             "html": slim_html(html)})

    def parse_many(self, pages: list[tuple[str, dict]]) -> list[dict]:
        """Batch helper: [(html, schema), ...] -> [extracted, ...]."""
        return [self.parse(html, schema) for html, schema in pages]
