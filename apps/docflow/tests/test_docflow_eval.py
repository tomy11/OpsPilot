import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))


async def test_docflow_eval_gate():
    from docflow_eval import run_eval

    m = (await run_eval()).metrics()
    assert m["unsafe_auto_posts"] == 0
    assert m["issue_recall"] == 1.0
    assert m["routing_accuracy"] == 1.0
    assert m["field_accuracy"] >= 0.95
