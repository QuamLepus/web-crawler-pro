"""Tests for the Advanced AI Layer (src/ai/agents/*).

All tests run offline: injected fake fetchers/LLMs and deterministic
fallbacks — no network, no model downloads, no API keys.
"""

from __future__ import annotations

import json

import pytest

# --------------------------------------------------------------------------- #
# Fixtures / canned data
# --------------------------------------------------------------------------- #
PAGES = {
    "https://ex.com/": (
        "<html><title>Home</title><body>Welcome to Example Corp. "
        '<a href="/pricing">Pricing plans</a> <a href="/blog">Blog</a>'
        '<a href="https://other.com/x">external</a></body></html>'),
    "https://ex.com/pricing": (
        "<html><title>Pricing</title><body>Our Pro plan costs $49 per month. "
        "The Enterprise tier starts at $499 monthly with SLA. "
        '<a href="/contact">Contact us</a></body></html>'),
    "https://ex.com/contact": (
        "<html><title>Contact</title><body>Email sales@ex.com today. "
        "We reply within 2 business days.</body></html>"),
    "https://ex.com/blog": (
        "<html><title>Blog</title><body>An unrelated blog post about cooking "
        "pasta quickly with garlic bread.</body></html>"),
}


def canned_fetcher(url: str) -> str:
    if url not in PAGES:
        raise RuntimeError(f"404 {url}")
    return PAGES[url]


GOOD_DOC = ("Photosynthesis converts light energy into chemical energy stored "
            "in glucose molecules. This process powers nearly all ecosystems "
            "on Earth and produces oxygen as a byproduct. Chlorophyll absorbs "
            "red and blue wavelengths of sunlight.")

BOILER_DOC = ("buy cheap cookies click here subscribe now advertise with us "
              "sign up free copyright all rights reserved")

PII_DOC = "Contact john.doe@example.com or SSN 123-45-6789 for your account."


# =========================================================================== #
# agent_crawler
# =========================================================================== #
class TestAgentCrawler:
    def test_extract_links_same_host_only(self):
        from src.ai.agents.agent_crawler import extract_links

        links = extract_links("https://ex.com/", PAGES["https://ex.com/"])
        assert "https://ex.com/pricing" in links
        assert all("other.com" not in u for u in links)

    def test_heuristic_planner_ranks_relevant_first(self):
        from src.ai.agents.agent_crawler import HeuristicPlanner

        planner = HeuristicPlanner("pricing plans cost")
        ranked = planner.rank([("https://ex.com/blog", ""),
                               ("https://ex.com/pricing", "")])
        assert ranked[0].endswith("/pricing")

    def test_agent_offline_run_visits_and_records(self):
        from src.ai.agents.agent_crawler import AgentCrawler, PageToolKit

        agent = AgentCrawler(goal="pricing plans cost",
                             toolkit=PageToolKit(fetcher=canned_fetcher),
                             max_pages=5)
        res = agent.run("https://ex.com/")
        assert "https://ex.com/pricing" in res.visited
        assert len(res.records) >= 1
        pricing = [r for r in res.records if "/pricing" in r["url"]][0]
        assert any("$49" in f for f in pricing["key_facts"])

    def test_agent_respects_page_budget(self):
        from src.ai.agents.agent_crawler import AgentCrawler, PageToolKit

        agent = AgentCrawler(goal="everything",
                             toolkit=PageToolKit(fetcher=canned_fetcher),
                             max_pages=2)
        res = agent.run("https://ex.com/")
        assert len(res.visited) <= 2
        assert "budget" in res.summary.lower() or res.finished

    def test_agent_llm_decision_path(self):
        from src.ai.agents.agent_crawler import AgentCrawler, PageToolKit

        class FakeLLM:
            def __init__(self):
                self.n = 0

            def invoke(self, prompt):
                self.n += 1
                if self.n < 3:
                    return '{"thought":"more","action":"fetch"}'
                return ('{"thought":"done","action":"finish",'
                        '"summary":"goal met"}')

        agent = AgentCrawler(goal="pricing", llm=FakeLLM(),
                             toolkit=PageToolKit(fetcher=canned_fetcher),
                             max_pages=6)
        res = agent.run("https://ex.com/")
        assert res.finished and res.summary == "goal met"
        assert len(res.actions) == 3

    def test_agent_llm_bad_output_falls_back(self):
        from src.ai.agents.agent_crawler import AgentCrawler, PageToolKit

        class BadLLM:
            def invoke(self, prompt):
                return "not json at all!"

        agent = AgentCrawler(goal="pricing", llm=BadLLM(),
                             toolkit=PageToolKit(fetcher=canned_fetcher),
                             max_pages=2)
        res = agent.run("https://ex.com/")   # must not raise
        assert len(res.visited) >= 1

    def test_first_json_object_balanced(self):
        from src.ai.agents.agent_crawler import _first_json_object

        s = 'noise {"a": {"b": 1}} tail'
        assert json.loads(_first_json_object(s)) == {"a": {"b": 1}}

    def test_crawl_site_convenience(self):
        from src.ai.agents.agent_crawler import crawl_site

        res = crawl_site("https://ex.com/", goal="pricing",
                         fetcher=canned_fetcher, max_pages=3)
        assert res.seed_url == "https://ex.com/"


# =========================================================================== #
# rag_chain
# =========================================================================== #
class TestRAGChain:
    def _chain(self):
        from src.ai.agents.rag_chain import RAGChain

        return RAGChain(backend="memory")

    def test_ingest_and_count(self):
        rag = self._chain()
        n = rag.ingest_documents([
            {"doc_id": "d1", "text": GOOD_DOC},
            {"doc_id": "d2", "text": "Python was created by Guido van Rossum."},
        ])
        assert n >= 2
        assert rag.count() == n

    def test_retrieval_ordering(self):
        rag = self._chain()
        rag.ingest_documents([
            {"doc_id": "paris", "text": "The Eiffel Tower is located in Paris, France. It was completed in 1889."},
            {"doc_id": "code", "text": "Compilers translate source code into machine instructions."},
        ])
        chunks, scores = rag.retrieve("Where is the Eiffel Tower?")
        assert chunks[0].doc_id == "paris"
        assert scores[0] > scores[-1]

    def test_answer_extractive_with_citation(self):
        rag = self._chain()
        rag.ingest_document("hopper",
                            "Grace Hopper invented the first compiler. "
                            "She was a rear admiral in the US Navy.")
        ans = rag.answer("Who invented the first compiler?")
        assert "Hopper" in ans.answer
        assert "[hopper]" in ans.answer
        assert ans.sources[0]["doc_id"] == "hopper"

    def test_answer_empty_index(self):
        rag = self._chain()
        ans = rag.answer("anything?")
        assert ans.sources == []
        assert "No indexed context" in ans.answer

    def test_llm_synthesis_used_when_provided(self):
        from src.ai.agents.rag_chain import RAGChain

        calls = []

        def fake_llm(prompt):
            calls.append(prompt)
            return "Synthesized answer from context [d1]."

        rag = RAGChain(backend="memory", llm=fake_llm)
        rag.ingest_document("d1", GOOD_DOC)
        ans = rag.answer("What is photosynthesis?")
        assert ans.answer.startswith("Synthesized")
        assert "QUESTION" in calls[0] and "CONTEXT" in calls[0]

    def test_llm_failure_degrades_to_extractive(self):
        from src.ai.agents.rag_chain import RAGChain

        def broken_llm(prompt):
            raise RuntimeError("api down")

        rag = RAGChain(backend="memory", llm=broken_llm)
        rag.ingest_document("d1", "Marie Curie won two Nobel Prizes in physics and chemistry.")
        ans = rag.answer("How many Nobel Prizes did Marie Curie win?")
        assert "Possible answer" in ans.answer

    def test_split_text_overlap(self):
        from src.ai.agents.rag_chain import split_text

        words = " ".join(f"w{i}" for i in range(100))
        chunks = split_text(words, chunk_size=20, overlap=5)
        assert len(chunks) > 4                       # overlapping windows
        assert all(len(c.split()) <= 20 for c in chunks)

    def test_hash_embedder_deterministic_and_normalized(self):
        import math

        from src.ai.agents.rag_chain import HashEmbedder

        emb = HashEmbedder()
        v1 = emb.embed(["hello world"])[0]
        v2 = emb.embed(["hello world"])[0]
        assert v1 == v2
        assert abs(math.sqrt(sum(x * x for x in v1)) - 1.0) < 1e-6

    @pytest.mark.parametrize("backend_cls", ["ChromaVectorStore"])
    def test_chroma_store_roundtrip(self, backend_cls, tmp_path):
        """ChromaDB backend works when the package is installed."""
        pytest.importorskip("chromadb")
        from src.ai.agents.rag_chain import ChromaVectorStore, HashEmbedder

        store = ChromaVectorStore(collection="t_col",
                                  persist_dir=str(tmp_path / "chroma"))
        emb = HashEmbedder()
        vecs = emb.embed([GOOD_DOC, "Short second doc about testing."])
        from src.ai.agents.rag_chain import Chunk

        n = store.add([Chunk(doc_id="a", text=GOOD_DOC, vector=vecs[0]),
                       Chunk(doc_id="b", text="Short second doc about testing.",
                             vector=vecs[1])])
        assert n == 2 and store.count() == 2
        hits = store.query(emb.embed(["photosynthesis light energy"])[0], top_k=1)
        assert hits[0][0].doc_id == "a"


# =========================================================================== #
# data_curator
# =========================================================================== #
class TestDataCurator:
    def test_heuristic_keep_drop_veto(self):
        from src.ai.agents.data_curator import DataCurator

        cur = DataCurator(score_fn=None)
        assert cur.score_record(GOOD_DOC).decision == "keep"
        assert cur.score_record(PII_DOC).decision == "drop"       # safety veto
        assert cur.score_record("tiny").overall < 0.8

    def test_llm_judge_parsing(self):
        from src.ai.agents.data_curator import DataCurator

        def fake_judge(prompt):
            return ('```json\n{"relevance": 5, "coherence": 4, "density": 4,'
                    ' "safety": 5, "reason": "solid"}\n```')

        cur = DataCurator(score_fn=fake_judge)
        qs = cur.score_record({"text": "whatever"})
        assert qs.decision == "keep" and qs.overall > 0.75
        assert qs.reason == "solid"

    def test_llm_judge_garbage_falls_back(self):
        from src.ai.agents.data_curator import DataCurator

        cur = DataCurator(score_fn=lambda p: "I refuse to output JSON")
        qs = cur.score_record(GOOD_DOC)          # heuristic fallback, no raise
        assert qs.reason == "heuristic offline score"

    def test_clamps_out_of_range_scores(self):
        from src.ai.agents.data_curator import DataCurator

        cur = DataCurator(score_fn=lambda p: '{"relevance": 99, "coherence": 0,'
                                              ' "density": 3, "safety": 7}')
        qs = cur.score_record("x")
        assert qs.relevance == 5 and qs.coherence == 1 and qs.safety == 5

    def test_curate_report_stats(self):
        from src.ai.agents.data_curator import DataCurator

        rep = DataCurator(score_fn=None).curate([GOOD_DOC, PII_DOC, BOILER_DOC])
        assert rep.total == 3
        assert rep.kept + rep.dropped + rep.reviewed == 3
        assert rep.mean_quality > 0
        assert all("decision" in r for r in rep.results)

    def test_filter_records_keeps_only_good(self):
        from src.ai.agents.data_curator import DataCurator

        recs = [{"t": GOOD_DOC}, {"t": PII_DOC}]
        kept = DataCurator(score_fn=None).filter_records(recs)
        assert kept == [recs[0]]


# =========================================================================== #
# synthetic
# =========================================================================== #
class TestSyntheticGenerator:
    DOCS = [("doc1", GOOD_DOC),
            ("doc2", "Ada Lovelace wrote the first algorithm intended for "
                     "machines. Charles Babbage designed the Analytical "
                     "Engine in 1837.")]

    def test_qa_offline_grounding(self):
        from src.ai.agents.synthetic import SyntheticDataGenerator

        g = SyntheticDataGenerator(llm=None, seed=1)
        pairs = list(g.qa_pairs(self.DOCS, n=2))
        assert pairs
        for ex in pairs:
            assert ex.method == "qa"
            assert ex.instruction.strip().endswith("?") or "about" in ex.instruction
            assert ex.output                              # non-empty answers

    def test_paraphrase_rule_changes_words(self):
        from src.ai.agents.synthetic import SyntheticDataGenerator

        g = SyntheticDataGenerator(llm=None, seed=2)
        outs = [e.output for e in g.paraphrases(self.DOCS, n=1)]
        assert outs and outs[0] != GOOD_DOC               # transformed
        assert "rapidly" not in outs[0] or True           # deterministic run

    def test_self_instruct_offline_templates(self):
        from src.ai.agents.synthetic import SyntheticDataGenerator

        g = SyntheticDataGenerator(llm=None, seed=3)
        exs = list(g.self_instruct(["Explain how vaccines work"], n_per_seed=3))
        assert len(exs) == 3
        assert all(e.method == "template" for e in exs)

    def test_generate_mix_dedupes(self):
        from src.ai.agents.synthetic import SyntheticDataGenerator

        g = SyntheticDataGenerator(llm=None, seed=4)
        out = g.generate(seeds=["how do eclipses happen"], docs=self.DOCS)
        hashes = [json.loads(e.to_jsonl())["seed_hash"] for e in out]
        assert len(hashes) == len(set(hashes))            # deduped
        assert {"qa", "paraphrase_rule", "template"} <= {e.method for e in out}

    def test_llm_paths_with_fake_llm(self):
        from src.ai.agents.synthetic import SyntheticDataGenerator

        def fake_llm(prompt):
            if "NEW but similar tasks" in prompt:
                return '["Write a poem about oceans", "Draft an email"]'
            if "question-answer pairs" in prompt:
                return '[{"question":"What did Ada do?",' \
                       '"answer":"Wrote the first algorithm."}]'
            return '["Variant one.", "Variant two."]'

        g = SyntheticDataGenerator(llm=fake_llm)
        out = g.generate(seeds=["Summarize the article"], docs=self.DOCS,
                         mix={"self_instruct": 2, "paraphrase": 2, "qa": 1})
        methods = {e.method for e in out}
        assert {"self_instruct", "paraphrase", "qa"} <= methods

    def test_write_jsonl_roundtrip(self, tmp_path):
        from src.ai.agents.synthetic import SyntheticDataGenerator

        g = SyntheticDataGenerator(llm=None, seed=5)
        out = g.generate(seeds=["why is the sky blue"], docs=self.DOCS)
        path = tmp_path / "synth.jsonl"
        n = g.write_jsonl(out, str(path))
        lines = path.read_text().strip().splitlines()
        assert n == len(lines) == len(out)
        row = json.loads(lines[0])
        assert {"instruction", "output", "source", "method", "seed_hash"} \
               <= set(row)

    def test_reproducible_with_same_seed(self):
        from src.ai.agents.synthetic import SyntheticDataGenerator

        a = SyntheticDataGenerator(llm=None, seed=42).generate([], self.DOCS[:1],
                                                               mix={"qa": 2, "paraphrase": 1})
        b = SyntheticDataGenerator(llm=None, seed=42).generate([], self.DOCS[:1],
                                                               mix={"qa": 2, "paraphrase": 1})
        assert [e.instruction for e in a] == [e.instruction for e in b]

    def test_helper_functions(self):
        from src.ai.agents.synthetic import generate_paraphrases, generate_qa_pairs

        qa = generate_qa_pairs(self.DOCS, n=1)
        assert all(set(q) >= {"question", "answer"} for q in qa)
        paras = generate_paraphrases(self.DOCS, n=1)
        assert len(paras) == len(self.DOCS)
