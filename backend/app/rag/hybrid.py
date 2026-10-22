"""Keyword ranking (BM25) and rank fusion (RRF) for hybrid retrieval. Pure functions, no I/O."""

import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable

# Spanish function words, written without accents (tokens are accent-folded first).
STOPWORDS_ES = frozenset(
    """a al algo como con cual cuales cuando cuanto de del desde donde dos el ella ellas
    ellos en entre era es esa ese eso esta este esto estan estar hay la las le les lo los
    mas me mi mis muy no o para pero por porque que quien quienes se sea ser si sin sobre
    su sus te tiene tienen un una unas uno unos y ya""".split()
)

RRF_K = 60  # standard constant for reciprocal rank fusion


def tokenize(text: str) -> list[str]:
    """Lowercase, drop accents, keep words longer than one letter, drop stopwords."""
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return [t for t in re.findall(r"\w+", folded) if len(t) > 1 and t not in STOPWORDS_ES]


def bm25_scores(
    query: list[str], documents: list[list[str]], k1: float = 1.5, b: float = 0.75
) -> list[float]:
    """Okapi BM25 score of each tokenized document for the tokenized query."""
    n = len(documents)
    if n == 0 or not query:
        return [0.0] * n
    avg_len = sum(len(d) for d in documents) / n or 1.0
    counts = [Counter(d) for d in documents]
    terms = set(query)
    doc_freq = {t: sum(1 for c in counts if t in c) for t in terms}
    scores: list[float] = []
    for counter, doc in zip(counts, documents, strict=True):
        length_norm = 1 - b + b * len(doc) / avg_len
        score = 0.0
        for term in terms:
            tf = counter.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * length_norm)
        scores.append(score)
    return scores


def keyword_order(query: str, documents: list[str], top_k: int) -> list[int]:
    """Indexes of the `top_k` documents with a BM25 score for the query, best first.

    CPU-bound over the whole corpus: call it through asyncio.to_thread, never on the loop.
    """
    scores = bm25_scores(tokenize(query), [tokenize(text) for text in documents])
    best = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: -scores[i])
    return best[:top_k]


def reciprocal_rank_fusion(rankings: Iterable[list[str]], k: int = RRF_K) -> list[str]:
    """Merge ranked id lists. Ties are broken by id so the order is deterministic."""
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] += 1 / (k + rank)
    return sorted(scores, key=lambda item: (-scores[item], item))
