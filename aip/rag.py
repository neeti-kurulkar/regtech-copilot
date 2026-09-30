"""A reference RAG pipeline, written as seven named stages.

Lab 4 asks you to build your own from a skeleton. This file exists so that
Labs 5-7 have a working baseline to improve on, and so you have something to
compare your implementation against.

The reason it is structured as seven explicit stages is the reason for the
whole of Lab 5: when a RAG system gives a wrong answer, "RAG is bad" is not a
diagnosis. Exactly one of these stages usually failed, and each has a
different fix:

    1. ingest    the document was never in the corpus            -> data
    2. chunk     the answer was split across two chunks          -> chunking
    3. embed     the query and passage words do not overlap      -> hybrid/HyDE
    4. retrieve  the right chunk existed but ranked 14th         -> k, rerank
    5. rerank    the right chunk was in the pool but discarded   -> reranker
    6. generate  the right chunk was in context and was ignored  -> prompt
    7. present   correct answer, wrong or missing citation       -> output contract

Trace every stage (this file does) and the diagnosis takes a minute instead
of an afternoon.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from aip import tracing
from aip.guards import UNTRUSTED_SYSTEM_CLAUSE, delimit_untrusted, enforce_citations
from aip.llm import chat, stream_chat
from aip.retrieval import Hit, Retriever, format_context

ANSWER_SYSTEM = f"""\
You answer questions using ONLY the numbered sources provided.

Rules, in priority order:
1. If the sources do not contain the answer, reply exactly:
   "I don't have enough information in the provided sources to answer that."
   Do not guess, and do not fall back on general knowledge.
2. Every factual sentence must end with a citation of the source(s) that
   support it, in the form [1] or [2][5].
3. Never cite a number that was not given to you.
4. If sources disagree, say so and cite both.
5. Be concise. Two or three sentences unless the question needs more.

{UNTRUSTED_SYSTEM_CLAUSE}
"""


@dataclass
class RagAnswer:
    question: str
    answer: str
    hits: list[Hit] = field(default_factory=list)
    citations_valid: bool = True
    invalid_citations: list[int] = field(default_factory=list)
    refused: bool = False
    stages: dict[str, Any] = field(default_factory=dict)
    # [regtech] Lab 4 Part B bookkeeping: what validation found, how many
    # corrective regenerations it took, and whether we failed closed.
    problems: list[str] = field(default_factory=list)
    repairs: int = 0
    fallback: bool = False
    budget_doublings: int = 0

    @property
    def cited_indices(self) -> list[int]:
        import re

        return sorted({int(m) for m in re.findall(r"\[(\d+)\]", self.answer)
                       if 1 <= int(m) <= len(self.hits)})

    @property
    def cited_doc_ids(self) -> list[str]:
        import re

        idx = sorted({int(m) for m in re.findall(r"\[(\d+)\]", self.answer)})
        return [self.hits[i - 1].doc_id for i in idx if 1 <= i <= len(self.hits)]


REFUSAL = "I don't have enough information in the provided sources to answer that."


def is_refusal(text: str) -> bool:
    """[regtech] True if the answer *is* the refusal (a partial answer that ends
    by declining one part is not a refusal)."""
    return text.strip().startswith(REFUSAL[:40])


def validate_answer(text: str, n_sources: int, finish_reason: str | None = None) -> list[str]:
    """[regtech] Lab 4 Part B2 as code. Returns the problems found (empty = valid).

    Checks: non-empty; not truncated; every [n] refers to a supplied source;
    a non-refusal carries at least one citation.
    """
    stripped = text.strip()
    if not stripped:
        return ["the answer was empty"]
    problems = []
    if finish_reason == "length":
        problems.append("the answer was cut off (token limit); write a shorter complete answer")
    if is_refusal(stripped):
        return problems
    ok, invalid = enforce_citations(stripped, n_sources)
    if invalid:
        problems.append(f"it cites sources that do not exist: {invalid} (only [1]..[{n_sources}] were given)")
    elif not ok:
        problems.append("it has no citations; every factual sentence must end with a citation like [1]")
    return problems


class RagPipeline:
    """Compose a retriever, an optional reranker, and a generator.

        pipe = RagPipeline(retriever, reranker=reranker, k=20, final_k=5)
        out  = pipe.answer("What is the claim window for out-patient cover?")
    """

    def __init__(
        self,
        retriever: Retriever,
        *,
        reranker: Any | None = None,
        k: int = 12,
        final_k: int = 5,
        tier: str = "MAIN",
        max_context_chars: int = 8000,
        query_transform: Callable[[str], str] | None = None,
        system: str = ANSWER_SYSTEM,
        max_repairs: int = 0,
        fail_closed: bool = False,
        context_label: Callable[[Hit], str] | None = None,
        max_tokens: int = 600,
    ):
        """[regtech] `max_repairs`: corrective regenerations when validate_answer
        finds a problem. `fail_closed`: if still invalid after the repairs,
        return REFUSAL instead of the flawed answer. `context_label`: per-source
        header for format_context. Defaults reproduce the original behaviour."""
        self.retriever = retriever
        self.reranker = reranker
        self.k = k
        self.final_k = final_k
        self.tier = tier
        self.max_context_chars = max_context_chars
        self.query_transform = query_transform
        self.system = system
        self.max_repairs = max_repairs
        self.fail_closed = fail_closed
        self.context_label = context_label
        self.max_tokens = max_tokens

    # -- stage 3/4 -------------------------------------------------------
    def retrieve(self, question: str) -> list[Hit]:
        q = self.query_transform(question) if self.query_transform else question
        with tracing.trace("rag.retrieve", k=self.k, transformed=q != question) as s:
            hits = self.retriever.search(q, k=self.k)
            s["n_hits"] = len(hits)
            s["top_doc"] = hits[0].doc_id if hits else None
        return hits

    # -- stage 5 ---------------------------------------------------------
    def rerank(self, question: str, hits: Sequence[Hit]) -> list[Hit]:
        if not self.reranker:
            return list(hits)[: self.final_k]
        with tracing.trace("rag.rerank", n_in=len(hits), n_out=self.final_k):
            return self.reranker.rerank(question, hits, k=self.final_k)

    # -- stage 6/7 -------------------------------------------------------
    def generate(self, question: str, hits: Sequence[Hit]) -> str:
        return self._generate(question, hits)["text"].strip()

    def _generate(self, question: str, hits: Sequence[Hit],
                  previous: str | None = None, problems: Sequence[str] = ()) -> dict[str, Any]:
        """[regtech] One generation; with `previous`/`problems` it is a corrective
        turn that shows the model its own answer and what was wrong with it.

        Truncation is not a content problem, so it is not "repaired": the same
        request is retried once at double the token budget, the convention
        aip.llm.structured and aip.evals.llm_judge already follow. Reasoning
        models spend part of max_tokens on invisible thinking, so a budget that
        looks generous can still cut the visible answer off.
        """
        res = self._generate_once(question, hits, previous, problems, self.max_tokens)
        if res.get("finish_reason") == "length":
            tracing.event("rag.truncated_retry", max_tokens=self.max_tokens * 2)
            res = self._generate_once(question, hits, previous, problems, self.max_tokens * 2)
            res["budget_doubled"] = True
        return res

    def _messages(self, question: str, hits: Sequence[Hit], previous: str | None = None,
                  problems: Sequence[str] = ()) -> list[dict[str, str]]:
        context = delimit_untrusted(
            format_context(hits, max_chars=self.max_context_chars, label=self.context_label)
        )
        prompt = f"{context}\n\nQuestion: {question}\n\nAnswer with citations:"
        messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
        if previous is not None:
            messages += [
                {"role": "assistant", "content": previous},
                {"role": "user", "content": "Your answer was rejected because " + "; and ".join(problems)
                 + ". Rewrite it following every rule in your instructions."},
            ]
        return messages

    def _generate_once(self, question: str, hits: Sequence[Hit], previous: str | None,
                       problems: Sequence[str], max_tokens: int) -> dict[str, Any]:
        messages = self._messages(question, hits, previous, problems)
        with tracing.trace("rag.generate", n_sources=len(hits), tier=self.tier, repair=previous is not None):
            return chat(messages, system=self.system, tier=self.tier,
                        temperature=0.0, max_tokens=max_tokens, return_full=True)

    def stream(self, question: str) -> Iterator[dict[str, Any]]:
        """[regtech] Lab 7 B3, "stream, then verdict". Yields events:

            {"type": "sources", "hits": [...]}       after retrieval, before any text
            {"type": "token", "text": "..."}         the first draft, as it is generated
            {"type": "verdict", "answer": RagAnswer, "retracted": bool}

        Citations cannot be checked until the draft is complete, and by then it has been shown. So the
        draft is validated at the end exactly as answer_from_hits would; if it fails, the usual repair /
        fail-closed path runs (not streamed) and the verdict carries the replacement with retracted=True.
        The client must act on the verdict (replace the draft), which is the price of a fast first token.
        """
        hits = self.retrieve(question)
        final = self.rerank(question, hits)
        yield {"type": "sources", "hits": final}
        s = stream_chat(self._messages(question, final), system=self.system, tier=self.tier,
                        temperature=0.0, max_tokens=self.max_tokens)
        for delta in s:
            yield {"type": "token", "text": delta}
        draft = s.result["text"].strip()
        problems = validate_answer(draft, len(final), s.result.get("finish_reason"))
        if not problems:
            ok, invalid = enforce_citations(draft, len(final))
            refused = is_refusal(draft)
            ans = RagAnswer(question=question, answer=draft, hits=final, citations_valid=ok or refused,
                            invalid_citations=invalid, refused=refused,
                            stages={"final": [h.doc_id for h in final], "final_k": len(final), "streamed": True})
            yield {"type": "verdict", "answer": ans, "retracted": False, "ttft_ms": s.ttft_ms, "cached": s.cached}
            return
        tracing.event("rag.stream_retracted", problems=problems)
        # re-run the non-streaming path from the draft: repair with the draft as `previous`, or fail closed
        ans = self._repair_from(question, final, draft, problems, stages={"streamed": True})
        yield {"type": "verdict", "answer": ans, "retracted": True, "ttft_ms": s.ttft_ms, "cached": s.cached}

    def _repair_from(self, question: str, final: list[Hit], text: str, problems: list[str],
                     stages: dict[str, Any] | None = None, doubled: int = 0) -> RagAnswer:
        """Validate -> repair -> (fail closed), starting from a first draft that has already been checked."""
        repairs = 0
        while problems and repairs < self.max_repairs:
            repairs += 1
            tracing.event("rag.repair", attempt=repairs, problems=problems)
            res = self._generate(question, final, previous=text, problems=problems)
            doubled += int(bool(res.get("budget_doubled")))
            text = res["text"].strip()
            problems = validate_answer(text, len(final), res.get("finish_reason"))
        fallback = bool(problems) and self.fail_closed
        if fallback:
            tracing.event("rag.fail_closed", problems=problems)
            text = REFUSAL
        ok, invalid = enforce_citations(text, len(final))
        refused = is_refusal(text)
        return RagAnswer(question=question, answer=text, hits=final, citations_valid=ok or refused,
                         invalid_citations=invalid, refused=refused,
                         stages={**(stages or {}), "final": [h.doc_id for h in final], "final_k": len(final)},
                         problems=problems, repairs=repairs, fallback=fallback, budget_doublings=doubled)

    def answer_from_hits(self, question: str, hits: Sequence[Hit],
                         stages: dict[str, Any] | None = None) -> RagAnswer:
        """[regtech] Generate -> validate -> repair -> (fail closed) over the given hits.

        Split out of answer() so the same generator can be run on gold context
        (Lab 4 E2: the generation ceiling vs. what retrieval actually supplies).
        """
        final = list(hits)
        res = self._generate(question, final)
        text = res["text"].strip()
        problems = validate_answer(text, len(final), res.get("finish_reason"))
        return self._repair_from(question, final, text, problems, stages=stages,
                                 doubled=int(bool(res.get("budget_doubled"))))

    def answer(self, question: str) -> RagAnswer:
        with tracing.trace("rag.answer", question=question[:120]):
            hits = self.retrieve(question)
            final = self.rerank(question, hits)
            return self.answer_from_hits(question, final, stages={
                "retrieved": [h.doc_id for h in hits], "retrieve_k": self.k})


# --------------------------------------------------------------------------
# Query transforms (Lab 5)
# --------------------------------------------------------------------------
def hyde(question: str, tier: str = "SMALL") -> str:
    """Hypothetical Document Embeddings.

    Ask the model to *write the passage that would answer the question*, then
    embed that instead of the question. It works because questions and
    passages live in different regions of embedding space: "how long do I have
    to file?" shares almost no vocabulary with "Claims must be submitted
    within 30 days of discharge." A hypothetical answer does.

    Gao et al. (2022), arxiv.org/abs/2212.10496. Costs one extra small-model
    call per query — measure whether it earns it.
    """
    draft = chat(
        f"Write a short factual paragraph that would answer this question, in the "
        f"style of a policy document. Invent plausible specifics; accuracy does not "
        f"matter, only phrasing.\n\nQuestion: {question}",
        tier=tier, max_tokens=180, temperature=0.0,
    )
    return f"{question}\n{draft}"


def multi_query(question: str, n: int = 3, tier: str = "SMALL") -> list[str]:
    """Generate paraphrases; retrieve for each; fuse with RRF.

    Buys recall when users phrase things unpredictably. Costs one small call
    plus n retrievals per query.
    """
    from aip.llm import chat as _chat
    from aip.llm import extract_json

    out = _chat(
        f"Rewrite this question in {n} different ways that a search engine would "
        f'match differently. Reply as a JSON list of strings.\n\n{question}',
        tier=tier, max_tokens=250, temperature=0.3,
    )
    try:
        variants = [str(v) for v in extract_json(out)][:n]
    except ValueError:
        variants = []
    return [question, *variants]
