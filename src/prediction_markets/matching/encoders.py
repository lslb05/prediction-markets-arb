"""The two model interfaces the matching pipeline depends on.

Both are Protocols, so nothing above this module imports torch. That is what
lets the test suite and CI exercise the whole matching pipeline against
deterministic fakes instead of downloading three gigabytes of checkpoints to
assert that a threshold works.

The concrete implementations live behind the loader functions and import
`sentence_transformers` at call time, which is only installed with the `ml`
extra -- see [project.optional-dependencies] in pyproject.toml.
"""

from collections.abc import Sequence
from typing import Protocol


class Encoder(Protocol):
    """Turns market text into a vector in the column's 1024-dimension space."""

    @property
    def name(self) -> str:
        """The checkpoint identifier, stored beside every vector it produces."""

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    """Scores a pair of market descriptions for referring to the same event.

    A bi-encoder embeds each text alone, so it can only measure whether two
    questions are about the same topic. A cross-encoder reads both at once,
    which is what separates 'Fed cuts in March' from 'Fed cuts in June' --
    near-identical as vectors, opposite as contracts.
    """

    @property
    def name(self) -> str: ...

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]: ...


def load_encoder(model: str) -> Encoder:
    return _SentenceTransformerEncoder(model)


def load_reranker(model: str) -> Reranker:
    return _CrossEncoderReranker(model)


class _SentenceTransformerEncoder:
    def __init__(self, model: str) -> None:
        from sentence_transformers import SentenceTransformer

        self._name = model
        self._model = SentenceTransformer(model, device="cpu")

    @property
    def name(self) -> str:
        return self._name

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        # Normalised at the source so the cosine distance stored in Postgres
        # is the same number the model would report.
        vectors = self._model.encode(
            list(texts), normalize_embeddings=True, batch_size=32
        )
        return [[float(value) for value in vector] for vector in vectors]


class _CrossEncoderReranker:
    def __init__(self, model: str) -> None:
        from sentence_transformers import CrossEncoder

        self._name = model
        self._model = CrossEncoder(model, device="cpu")

    @property
    def name(self) -> str:
        return self._name

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        return [float(score) for score in self._model.predict([list(p) for p in pairs])]
