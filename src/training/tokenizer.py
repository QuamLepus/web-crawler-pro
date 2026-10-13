"""LLM-grade tokenization pipeline (Hugging Face ``tokenizers`` + SentencePiece BPE).

Produces the artifact that training jobs actually consume: a stream of
integer token ids with document-boundary handling.

Two backends:
* ``tokenizers`` (rust) - fast, used when installed; supports GPT-2 style BPE
  and Llama-style SentencePiece (via conversion or direct vocab loading).
* Pure-python fallback - whitespace+regex tokenizer producing stable ids from
  a hash vocabulary. Deterministic, dependency-free, good enough for counting
  tokens at scale in CI/dev (never for real training).

Also implements **sequence packing**: concatenate documents up to
``seq_len`` (default 4096) with EOS separators, emitting fixed-width rows —
the standard way to feed causal-LM training without padding waste.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Iterator, Sequence

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"'s|'t|'re|'ve|'m|'ll|'d| ?\w+| ?[^\s\w]+|\s+")


@dataclass
class TokenizerSpec:
    """What tokenizer to build/load. Persisted as JSON next to shards."""

    name: str = "gpt2-bpe"
    vocab_size: int = 50_257
    model_type: str = "bpe"            # bpe | wordpiece | unigram (sentencepiece)
    special_tokens: tuple[str, ...] = ("<|endoftext|>", "<|bos|>", "<|eos|>")
    pretrained_path: str | None = None  # local dir or HF hub id

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "vocab_size": self.vocab_size,
                "model_type": self.model_type,
                "special_tokens": list(self.special_tokens),
                "pretrained_path": self.pretrained_path,
            }
        )


class _HashFallbackTokenizer:
    """Deterministic hashed-BPE-ish fallback (dev/CI only)."""

    def __init__(self, vocab_size: int = 50_257) -> None:
        self.vocab_size = vocab_size
        self.eos_id = 0

    def encode_ids(self, text: str) -> list[int]:
        ids: list[int] = []
        for tok in _WORD_RE.findall(text):
            if not tok.strip():
                continue
            h = hashlib.blake2b(tok.encode("utf-8"), digest_size=6).digest()
            ids.append(1 + int.from_bytes(h, "big") % (self.vocab_size - 1))
        return ids

    def count(self, text: str) -> int:
        return len(self.encode_ids(text))


class TokenizerEngine:
    """Unified wrapper over HF tokenizers / sentencepiece / fallback."""

    def __init__(self, spec: TokenizerSpec | None = None) -> None:
        self.spec = spec or TokenizerSpec()
        self._backend = None
        self._hf_tokenizer = None
        self._sp_model = None
        self._fallback: _HashFallbackTokenizer | None = None
        self._load()

    # -- construction --------------------------------------------------------- #
    @classmethod
    def train_bpe(
        cls,
        corpus_files: Sequence[str],
        out_path: str,
        *,
        vocab_size: int = 50_257,
        min_frequency: int = 2,
    ) -> "TokenizerEngine":
        """Train a fresh BPE tokenizer on local text files (one doc per line).

        Uses ``tokenizers`` if available; otherwise writes a JSON manifest so
        callers know training was skipped in this environment.
        """
        try:
            from tokenizers import Regex, Tokenizer, decoders, pre_tokenizers, trainers

            tok = Tokenizer.models.BPE(unk_token=None)
            fast = Tokenizer(tok)
            fast.pre_tokenizer = pre_tokenizers.Split(
                Regex(r"'s|'t|'re|'ve|'m|'ll|'d| ?[^\s]+"), behavior="isolated"
            )
            fast.decoder = decoders.ByteLevel()
            trainer = trainers.BpeTrainer(
                vocab_size=vocab_size,
                min_frequency=min_frequency,
                special_tokens=["<|endoftext|>", "<|bos|>", "<|eos|>"],
                initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
            )
            fast.train_from_iterator(_iter_lines(corpus_files), trainer)
            fast.save(out_path)
            logger.info("trained BPE vocab=%d -> %s", fast.get_vocab_size(), out_path)
        except ImportError:
            manifest = {"status": "skipped-no-tokenizers", "corpus": list(corpus_files)}
            with open(out_path + ".manifest.json", "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
            logger.warning("tokenizers not installed; wrote manifest only")
        return cls(TokenizerSpec(name="custom-bpe", vocab_size=vocab_size, pretrained_path=out_path))

    def _load(self) -> None:
        path = self.spec.pretrained_path
        if path and os.path.exists(path):
            try:
                from tokenizers import Tokenizer as HFTokenizer

                self._hf_tokenizer = HFTokenizer.from_file(path)
                self._backend = "hf"
                return
            except ImportError:
                pass
        if path and path.endswith(".model"):  # sentencepiece protobuf
            try:
                import sentencepiece as spm

                m = spm.SentencePieceProcessor()
                m.Load(path)
                self._sp_model = m
                self._backend = "sentencepiece"
                return
            except ImportError:
                pass
        if path and "/" in path and not os.path.exists(path):
            # Treat as HF hub id (e.g. "gpt2", "EleutherAI/gpt-neox-20b")
            try:
                from tokenizers import Tokenizer as HFTokenizer

                self._hf_tokenizer = HFTokenizer.from_pretrained(path)
                self._backend = "hf-hub"
                return
            except Exception as exc:  # noqa: BLE001 offline environments
                logger.debug("hub load failed (%s); using fallback", exc)
        self._fallback = _HashFallbackTokenizer(self.spec.vocab_size)
        self._backend = "fallback"

    # -- runtime API ------------------------------------------------------------ #
    @property
    def eos_id(self) -> int:
        if self._hf_tokenizer is not None:
            tid = self._hf_tokenizer.token_to_id("<|eos|>")
            if tid is not None:
                return tid
            return self._hf_tokenizer.token_to_id("<|endoftext|>") or 0
        if self._sp_model is not None:
            return self._sp_model.eos_id()
        assert self._fallback is not None
        return self._fallback.eos_id

    def encode_ids(self, text: str) -> list[int]:
        if self._hf_tokenizer is not None:
            return self._hf_tokenizer.encode(text, add_special_tokens=False).ids
        if self._sp_model is not None:
            return self._sp_model.Encode(text)
        assert self._fallback is not None
        return self._fallback.encode_ids(text)

    def decode(self, ids: Sequence[int]) -> str:
        if self._hf_tokenizer is not None:
            return self._hf_tokenizer.decode(list(ids))
        if self._sp_model is not None:
            return self._sp_model.Decode(list(ids))
        raise NotImplementedError("fallback tokenizer cannot decode ids")

    def count(self, text: str) -> int:
        return len(self.encode_ids(text))

    # -- packing ------------------------------------------------------------------ #
    def pack_documents(
        self, docs: Iterator[str] | Sequence[str], seq_len: int = 4096
    ) -> Iterator[list[int]]:
        """Concatenate documents into fixed ``seq_len`` rows separated by EOS.

        Standard atlas/the-stack packing: no padding between docs; the final
        partial row is dropped (training frameworks expect full rows) unless
        ``docs`` ends mid-row, in which case it is emitted padded with EOS.
        """
        buffer: list[int] = []
        eos = self.eos_id
        for doc in docs:
            ids = self.encode_ids(doc)
            ids.append(eos)  # boundary marker so model learns doc transitions
            buffer.extend(ids)
            while len(buffer) >= seq_len:
                yield buffer[:seq_len]
                buffer = buffer[seq_len:]
        if buffer:
            yield buffer + [eos] * (seq_len - len(buffer))

    def stats(self) -> dict:
        return {"backend": self._backend, "spec": json.loads(self.spec.to_json())}


def _iter_lines(files: Sequence[str]):
    for fp in files:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield line.rstrip("\n")
