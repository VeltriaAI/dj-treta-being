"""Eval score tracker — saves results as JSON baseline for regression detection.

Usage:
    # Run evals and save scores:
    pytest tests/eval_*.py -v --override-ini="addopts=" | python tests/eval_score.py save

    # Or use the pytest plugin (auto-saves after each run):
    pytest tests/eval_*.py --eval-score

    # Compare against baseline:
    python tests/eval_score.py compare
"""

import json
import sys
from datetime import datetime
from pathlib import Path

SCORES_DIR = Path(__file__).parent / "scores"
BASELINE_FILE = SCORES_DIR / "baseline.json"
LATEST_FILE = SCORES_DIR / "latest.json"


def parse_pytest_output(lines: list[str]) -> dict:
    """Parse pytest -v output into structured results."""
    results = {"tests": {}, "summary": {}, "timestamp": datetime.now().isoformat()}

    import re
    for line in lines:
        line = line.strip()
        # Match: tests/eval_dj_agent.py::test_dj01_schedule_at_breakdown PASSED  [  3%]
        m = re.match(r".*?::(\S+)\s+(PASSED|FAILED)", line)
        if m:
            test_id = m.group(1)
            status = "pass" if m.group(2) == "PASSED" else "fail"
            # Extract category from test name (e.g., test_dj01 -> DJ)
            category = test_id.split("_")[1][:2].upper() if "_" in test_id else "??"
            results["tests"][test_id] = {"status": status, "category": category}

    total = len(results["tests"])
    passed = sum(1 for t in results["tests"].values() if t["status"] == "pass")
    results["summary"] = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "score": round(passed / total * 100, 1) if total > 0 else 0,
    }

    # Category breakdown
    categories = {}
    for t in results["tests"].values():
        cat = t["category"]
        if cat not in categories:
            categories[cat] = {"passed": 0, "total": 0}
        categories[cat]["total"] += 1
        if t["status"] == "pass":
            categories[cat]["passed"] += 1
    for cat in categories:
        categories[cat]["score"] = round(
            categories[cat]["passed"] / categories[cat]["total"] * 100, 1
        )
    results["summary"]["categories"] = categories

    return results


def save_scores(results: dict, as_baseline: bool = False):
    """Save score results to JSON file."""
    SCORES_DIR.mkdir(exist_ok=True)

    LATEST_FILE.write_text(json.dumps(results, indent=2))
    print(f"Saved: {LATEST_FILE}")

    if as_baseline or not BASELINE_FILE.exists():
        BASELINE_FILE.write_text(json.dumps(results, indent=2))
        print(f"Saved baseline: {BASELINE_FILE}")


def compare_scores():
    """Compare latest scores against baseline."""
    if not BASELINE_FILE.exists():
        print("No baseline found. Run with 'save' first.")
        return
    if not LATEST_FILE.exists():
        print("No latest scores. Run evals first.")
        return

    baseline = json.loads(BASELINE_FILE.read_text())
    latest = json.loads(LATEST_FILE.read_text())

    b_score = baseline["summary"]["score"]
    l_score = latest["summary"]["score"]
    delta = l_score - b_score

    print(f"\n{'='*50}")
    print(f"EVAL SCORE COMPARISON")
    print(f"{'='*50}")
    print(f"Baseline: {b_score}% ({baseline['summary']['passed']}/{baseline['summary']['total']})")
    print(f"Latest:   {l_score}% ({latest['summary']['passed']}/{latest['summary']['total']})")
    print(f"Delta:    {'+' if delta >= 0 else ''}{delta:.1f}%")
    print()

    # Category comparison
    b_cats = baseline["summary"].get("categories", {})
    l_cats = latest["summary"].get("categories", {})
    all_cats = sorted(set(list(b_cats.keys()) + list(l_cats.keys())))

    print(f"{'Category':<12} {'Baseline':>10} {'Latest':>10} {'Delta':>8}")
    print(f"{'-'*42}")
    for cat in all_cats:
        b = b_cats.get(cat, {}).get("score", 0)
        l = l_cats.get(cat, {}).get("score", 0)
        d = l - b
        flag = " ⚠" if d < -5 else ""
        print(f"{cat:<12} {b:>9.1f}% {l:>9.1f}% {d:>+7.1f}%{flag}")

    print()
    # Regressions
    regressions = []
    for test_id, data in latest["tests"].items():
        if data["status"] == "fail":
            b_data = baseline["tests"].get(test_id, {})
            if b_data.get("status") == "pass":
                regressions.append(test_id)

    if regressions:
        print(f"REGRESSIONS ({len(regressions)}):")
        for r in regressions:
            print(f"  - {r}")
        print()
        if delta < -10:
            print("FAIL: Score dropped more than 10%")
            sys.exit(1)
    else:
        print("No regressions detected.")

    print(f"{'='*50}")


def print_summary(results: dict):
    """Print a nice summary table."""
    s = results["summary"]
    print(f"\n{'='*50}")
    print(f"EVAL RESULTS — {results['timestamp'][:19]}")
    print(f"{'='*50}")
    print(f"Score: {s['score']}% ({s['passed']}/{s['total']})")
    print()

    cats = s.get("categories", {})
    print(f"{'Category':<12} {'Score':>8} {'Detail':>12}")
    print(f"{'-'*34}")
    for cat in sorted(cats.keys()):
        c = cats[cat]
        print(f"{cat:<12} {c['score']:>7.1f}% {c['passed']}/{c['total']:>3}")
    print(f"{'='*50}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: pytest ... | python tests/eval_score.py save [--baseline]")
        print("       python tests/eval_score.py compare")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "save":
        lines = sys.stdin.readlines()
        results = parse_pytest_output(lines)
        as_baseline = "--baseline" in sys.argv
        save_scores(results, as_baseline=as_baseline)
        print_summary(results)
    elif cmd == "compare":
        compare_scores()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
