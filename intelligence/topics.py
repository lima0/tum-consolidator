"""
Topic extraction — Layer 3 LLM task.

For each PDF summary, extract 3-5 topic tags from a per-course controlled
vocabulary. Result is cached by SHA256(course_code + summary_text).

LLM earns its seat here: controlled vocab prevents free-form tag drift,
making downstream linking and eval possible.
"""

import json
import logging
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import db

load_dotenv()
log = logging.getLogger(__name__)

# Per-course controlled vocabularies.
# Keys: course shortname (or prefix). Values: sorted topic list.
VOCAB: dict[str, list[str]] = {
    "gad26": [
        "arrays", "asymptotic analysis", "AVL trees", "backtracking",
        "binary search", "binary search trees", "complexity analysis",
        "depth-first search", "divide and conquer", "dynamic programming",
        "graph algorithms", "hash tables", "heaps", "linked lists",
        "merge sort", "minimum spanning trees", "priority queues",
        "quicksort", "recursion", "red-black trees", "shortest paths",
        "sorting", "stacks and queues", "time complexity",
    ],
    "EIST26": [
        "abstract factory", "adapter pattern", "agile", "builder pattern",
        "class diagrams", "code smells", "composite pattern",
        "decorator pattern", "dependency injection", "design patterns",
        "facade pattern", "factory method", "GRASP principles",
        "inheritance", "interface segregation", "MVC", "observer pattern",
        "polymorphism", "proxy pattern", "refactoring", "responsibility",
        "sequence diagrams", "singleton", "software architecture",
        "SOLID principles", "strategy pattern", "template method",
        "testing", "UML", "use case diagrams",
    ],
    "gra26caps": [
        "ABI conventions", "assembler", "bitwise operations",
        "C programming", "calling conventions", "data types",
        "function calls", "git", "heap memory", "instruction set",
        "linker", "memory layout", "pointers", "recursion in assembly",
        "registers", "stack frames", "structs", "system calls",
        "undefined behavior", "x86-64",
    ],
    "FPV": [
        "algebraic data types", "currying", "fold", "functors",
        "higher-order functions", "Haskell", "IO monad", "lazy evaluation",
        "list comprehension", "map and filter", "monads",
        "pattern matching", "polymorphism", "pure functions",
        "QuickCheck", "recursion", "type classes", "type inference",
        "type system",
    ],
    "LA": [
        "basis", "column space", "determinants", "diagonalization",
        "dot product", "eigenvalues", "eigenvectors", "Gaussian elimination",
        "inner product spaces", "kernel", "linear independence",
        "linear maps", "linear systems", "matrices", "matrix multiplication",
        "null space", "orthogonality", "projections", "rank",
        "row reduction", "span", "vector spaces",
    ],
    # Fallback for unknown courses
    "_default": [
        "algorithms", "complexity", "data structures", "foundations",
        "mathematics", "programming", "proofs", "theory",
    ],
}


def _vocab_for(course_code: str) -> list[str]:
    for prefix, vocab in VOCAB.items():
        if prefix == "_default":
            continue
        if course_code.lower().startswith(prefix.lower()) or prefix.lower() in course_code.lower():
            return vocab
    return VOCAB["_default"]


def extract_topics(course_code: str, summary_text: str) -> list[str]:
    """
    Return 3-5 topic tags for a PDF summary. Cached by content hash.
    Never returns free-form tags — only terms from the controlled vocab.
    """
    vocab = _vocab_for(course_code)
    cache_key = db.hash_prompt(f"topics_v1|{course_code}|{summary_text}")

    cached = db.cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    vocab_str = ", ".join(vocab)
    prompt = (
        f"Extract 3 to 5 topic tags from this course material summary.\n"
        f"Only use terms from this controlled vocabulary: {vocab_str}\n"
        f"Return ONLY a JSON array of strings. No explanation.\n"
        f"Example: [\"sorting\", \"complexity analysis\"]\n\n"
        f"Summary:\n{summary_text}"
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = next((b.text for b in response.content if hasattr(b, "text")), "[]")

        # Extract JSON array even if model adds surrounding text
        import re
        m = re.search(r'\[.*?\]', raw, re.DOTALL)
        if not m:
            log.warning("topics: no JSON array in response: %r", raw[:100])
            return []

        tags = json.loads(m.group(0))

        # Filter to vocab only (model can hallucinate)
        vocab_lower = {v.lower(): v for v in vocab}
        filtered = [vocab_lower[t.lower()] for t in tags if t.lower() in vocab_lower]

        db.cache_set(cache_key, json.dumps(filtered))
        return filtered

    except Exception as e:
        log.warning("topics: extraction failed: %s", e)
        return []


def topics_for_pdf(course_code: str, pdf_summary: str) -> str:
    """Return comma-separated topic string for use in Calendar event notes."""
    tags = extract_topics(course_code, pdf_summary)
    return ", ".join(tags) if tags else ""
