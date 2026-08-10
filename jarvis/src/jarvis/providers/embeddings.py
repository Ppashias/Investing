"""Embedding providers.

Embeddings sit behind the same kind of abstraction as completions, for the same
reason: the choice of embedding model is an operational decision, and it
changes. Unlike completions, though, the choice is *load-bearing for stored
data* — vectors from different models are not comparable, so switching models
invalidates every stored vector. That is why :class:`Embedding` rows carry
their model name and retrieval filters on it.

Two implementations ship:

* :class:`OpenAICompatibleEmbeddingProvider` — the real one. The
  ``/v1/embeddings`` wire format is shared by OpenAI, Ollama, LM Studio,
  llama.cpp and vLLM, so one adapter covers both the hosted and the fully
  local case. Anthropic has no embeddings API, which is why the default
  completion provider cannot serve this.

* :class:`LexicalEmbeddingProvider` — the fallback, and the one that needs
  care. It is a genuine, deterministic vectoriser (hashed word and character
  n-grams, TF-weighted, L2-normalised), and similarity search over it genuinely
  works. What it is **not** is semantic: it can tell that "dark mode" matches
  "dark mode interface", and it cannot tell that "dark mode" matches "black
  theme". It exists so JARVIS degrades to lexical retrieval instead of no
  retrieval when no embedding endpoint is configured, and so tests are
  deterministic and offline.

  Every surface that reports the active provider reports ``semantic: false``
  for it, because a system that silently substitutes keyword matching for
  semantic search while claiming semantic search is lying by omission.
"""

from __future__ import annotations

import abc
import hashlib
import math
import re
from dataclasses import dataclass

import httpx

from jarvis.errors import ProviderError, ProviderNotConfiguredError, ProviderTimeoutError
from jarvis.logging import get_logger, register_secret_value
from jarvis.secrets import Secret

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EmbeddingInfo:
    key: str
    model: str
    dim: int
    #: False means lexical-only. Callers use this to decide how much to trust a
    #: similarity score, and the UI uses it to tell the user what they have.
    semantic: bool
    description: str


class EmbeddingProvider(abc.ABC):
    """Turns text into vectors."""

    @property
    @abc.abstractmethod
    def info(self) -> EmbeddingInfo: ...

    @abc.abstractmethod
    def is_configured(self) -> bool: ...

    @abc.abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch. Order of results matches order of inputs."""

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]


# ── OpenAI-compatible ────────────────────────────────────────────────────────


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """``POST {base_url}/embeddings``.

    Batches are sent whole; the endpoint accepts a list and returns one vector
    per input, which is both faster and cheaper than one request per text.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: Secret | None = None,
        model: str = "text-embedding-3-small",
        dim: int = 1536,
        timeout: float = 60.0,
        key: str = "openai_compat",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dim = dim
        self._timeout = timeout
        self._key = key
        if api_key:
            register_secret_value(api_key.reveal())

    @property
    def info(self) -> EmbeddingInfo:
        return EmbeddingInfo(
            key=self._key,
            model=self._model,
            dim=self._dim,
            semantic=True,
            description=f"{self._model} via {self._base_url}",
        )

    def is_configured(self) -> bool:
        return bool(self._base_url)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.is_configured():
            raise ProviderNotConfiguredError("No embedding endpoint configured")

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key.reveal()}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/embeddings",
                    headers=headers,
                    json={"model": self._model, "input": texts},
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"Embedding endpoint timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Embedding endpoint unreachable: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"Embedding endpoint returned {response.status_code}: "
                f"{response.text[:400]}"
            )

        payload = response.json()
        rows = payload.get("data") or []
        if len(rows) != len(texts):
            raise ProviderError(
                f"Embedding endpoint returned {len(rows)} vectors for "
                f"{len(texts)} inputs"
            )
        # The API documents an ``index`` field and does not guarantee order.
        rows = sorted(rows, key=lambda r: r.get("index", 0))
        vectors = [list(map(float, r["embedding"])) for r in rows]

        actual = len(vectors[0]) if vectors else self._dim
        if actual != self._dim:
            # Not fatal — record the truth rather than the configuration, since
            # the stored dimension has to match what was stored.
            log.warning(
                "embedding_dim_mismatch", configured=self._dim, actual=actual,
                model=self._model,
            )
            self._dim = actual
        return vectors


# ── lexical fallback ─────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[a-z0-9]+")
_LEXICAL_DIM = 512


class LexicalEmbeddingProvider(EmbeddingProvider):
    """Deterministic hashed-n-gram vectoriser. Lexical, **not** semantic.

    Words are hashed into a fixed-width vector with sublinear term-frequency
    weighting, and character 4-grams are hashed alongside at lower weight so
    that morphological variants and typos still overlap ("preference" /
    "preferences"). The result is L2-normalised, so cosine similarity is a
    plain dot product.

    This is the standard hashing-vectoriser construction. Its ceiling is real:
    two texts that share no character sequences score zero however related they
    are. Treat its scores as evidence of lexical overlap and nothing more —
    which is exactly how :mod:`jarvis.memory.retrieval` weights it when
    ``semantic`` is false.
    """

    def __init__(self, *, dim: int = _LEXICAL_DIM) -> None:
        self._dim = dim

    @property
    def info(self) -> EmbeddingInfo:
        return EmbeddingInfo(
            key="lexical",
            model=f"lexical-hash-{self._dim}",
            dim=self._dim,
            semantic=False,
            description=(
                "Local hashed n-gram vectoriser. Lexical overlap only — it "
                "cannot match paraphrases. Configure an embedding endpoint "
                "for semantic retrieval."
            ),
        )

    def is_configured(self) -> bool:
        return True  # nothing to configure; that is the point

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectorise(text) for text in texts]

    def _vectorise(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        lowered = text.lower()
        words = _WORD_RE.findall(lowered)

        counts: dict[str, float] = {}
        for word in words:
            counts[word] = counts.get(word, 0.0) + 1.0
        # Word bigrams: cheap phrase sensitivity, so "dark mode" is not just
        # "dark" plus "mode".
        for a, b in zip(words, words[1:]):
            bigram = f"{a}_{b}"
            counts[bigram] = counts.get(bigram, 0.0) + 1.0

        for term, count in counts.items():
            # Sublinear TF: the tenth occurrence of a word says much less than
            # the second, and without this a repeated word dominates the vector.
            vector[self._bucket(term)] += 1.0 + math.log(count)

        compact = "".join(words)
        for i in range(len(compact) - 3):
            vector[self._bucket(f"#{compact[i:i + 4]}")] += 0.25

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]

    def _bucket(self, term: str) -> int:
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "little") % self._dim


# ── selection ────────────────────────────────────────────────────────────────


def build_embedding_provider(settings) -> EmbeddingProvider:  # type: ignore[no-untyped-def]
    """Pick an embedding provider from configuration.

    Prefers a configured endpoint; falls back to lexical with a warning that
    names the consequence, because "semantic search is not actually semantic"
    is not something anyone should have to discover from result quality.
    """
    from jarvis.config import get_secret

    base_url = settings.embedding_base_url or settings.openai_base_url
    if base_url:
        api_key = get_secret(settings.embedding_api_key_name) or get_secret(
            settings.openai_api_key_name
        )
        provider = OpenAICompatibleEmbeddingProvider(
            base_url=base_url,
            api_key=api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            timeout=settings.provider_timeout_seconds,
        )
        log.info(
            "embedding_provider_selected",
            provider=provider.info.key,
            model=provider.info.model,
            semantic=True,
        )
        return provider

    log.warning(
        "embedding_provider_fallback",
        reason="no embedding endpoint configured",
        consequence="retrieval is lexical only; paraphrases will not match",
    )
    return LexicalEmbeddingProvider()
