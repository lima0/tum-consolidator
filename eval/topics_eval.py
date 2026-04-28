#!/usr/bin/env python3
"""
Eval harness for topic extraction (Layer 3 LLM task).

Runs extract_topics() over all labeled examples, computes per-example
precision/recall/F1, prints aggregate results, and shows failure cases.

Usage:
    python eval/topics_eval.py [--no-cache]
"""

import json
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # initialises DB / cache
from intelligence.topics import extract_topics

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

LABELED_PATH = Path(__file__).parent / "topics_labeled.json"


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def run_eval(no_cache: bool = False) -> None:
    examples = json.loads(LABELED_PATH.read_text())
    print(f"Evaluating {len(examples)} labeled examples…\n")

    if no_cache:
        # Wipe llm_cache so every prediction is a fresh API call
        db.get_db().execute("DELETE FROM llm_cache WHERE prompt_hash LIKE 'topics_v1%'")
        db.get_db().commit()
        print("Cache cleared.\n")

    total_precision = total_recall = total_f1 = 0.0
    failures: list[dict] = []

    for ex in examples:
        course   = ex["course_code"]
        summary  = ex["summary"]
        correct  = {t.lower() for t in ex["correct_topics"]}

        predicted = extract_topics(course, summary)
        pred_set  = {t.lower() for t in predicted}

        tp = len(pred_set & correct)
        precision = tp / len(pred_set) if pred_set else 0.0
        recall    = tp / len(correct)  if correct  else 0.0
        f1        = _f1(precision, recall)

        total_precision += precision
        total_recall    += recall
        total_f1        += f1

        status = "✓" if f1 >= 0.5 else "✗"
        print(f"  {status} [{ex['id']}]")
        print(f"      predicted: {predicted}")
        print(f"      correct:   {sorted(ex['correct_topics'])}")
        print(f"      P={precision:.2f}  R={recall:.2f}  F1={f1:.2f}")

        if f1 < 0.5:
            failures.append({
                "id":        ex["id"],
                "course":    course,
                "predicted": predicted,
                "correct":   sorted(ex["correct_topics"]),
                "precision": round(precision, 2),
                "recall":    round(recall, 2),
                "f1":        round(f1, 2),
            })

    n = len(examples)
    avg_p  = total_precision / n
    avg_r  = total_recall    / n
    avg_f1 = total_f1        / n

    print(f"\n{'='*55}")
    print(f"  Macro avg  P={avg_p:.3f}  R={avg_r:.3f}  F1={avg_f1:.3f}")
    print(f"  Failures   {len(failures)}/{n}")
    print(f"{'='*55}\n")

    if failures:
        print("Failure analysis:")
        for f in failures:
            print(f"\n  [{f['id']}] F1={f['f1']}")
            print(f"    Predicted: {f['predicted']}")
            print(f"    Correct:   {f['correct']}")
            # Root cause heuristic
            missed = set(f["correct"]) - set(p.lower() for p in f["predicted"])
            extra  = set(p.lower() for p in f["predicted"]) - set(f["correct"])
            if missed:
                print(f"    Missed:    {sorted(missed)}")
            if extra:
                print(f"    Extra:     {sorted(extra)}")


if __name__ == "__main__":
    no_cache = "--no-cache" in sys.argv
    run_eval(no_cache=no_cache)
