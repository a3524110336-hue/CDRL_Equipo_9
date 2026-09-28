import json
from pathlib import Path

M = json.loads((Path(__file__).resolve().parents[1] / "artifacts" / "nosql-matrix.json")
               .read_text(encoding="utf-8"))


def test_weights_sum_to_one_and_totals_consistent():
    assert abs(sum(c["weight"] for c in M["criteria"]) - 1) < 1e-9
    for st, total in M["totals"].items():
        assert total == round(sum(c["weight"] * c["scores"][st] for c in M["criteria"]), 3)
    assert M["totals"][M["selected"]] == max(M["totals"].values())


def test_tie_is_resolved_by_written_rule():
    if M["tie"]:
        assert M["selected"] in M["tied_stores"]
        assert M["tie_break"]["rule"].strip() and M["tie_break"]["falsified_if"].strip()


def test_every_criterion_is_falsifiable():
    for c in M["criteria"]:
        assert c["kind"] in {"measurement", "hypothesis"}
        assert c["claim"].strip() and c["falsified_if"].strip()


def test_discarded_alternative_is_declared():
    d = M["discarded_alternative"]
    assert d["store"] != M["selected"]
    assert "COMPLETAR" not in d["store"] + d["reason"]