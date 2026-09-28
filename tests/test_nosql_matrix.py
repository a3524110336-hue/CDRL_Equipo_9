import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
M = json.loads((ROOT / "artifacts" / "nosql-matrix.json").read_text(encoding="utf-8"))


def test_weights_sum_to_one_and_totals_consistent():
    assert abs(sum(c["weight"] for c in M["criteria"]) - 1) < 1e-9
    for st, total in M["totals"].items():
        assert total == round(sum(c["weight"] * c["scores"][st] for c in M["criteria"]), 3)
    assert M["totals"][M["selected"]] == max(M["totals"].values())


def test_matrix_matches_adr_004_totals():
    assert M["totals"] == {"dynamodb": 4.3, "document": 3.85, "graph": 2.2, "object": 2.75}
    assert M["selected"] == "dynamodb"
    assert M["margin_vs_runner_up"] == 0.45
    adr = (ROOT / "docs" / "ADR-004-decision-nosql.md").read_text(encoding="utf-8", errors="replace")
    assert "4,30" in adr and "3,85" in adr


def test_no_tie_and_selected_is_unique_winner():
    assert M["tie"] is False
    assert M["tied_stores"] == [M["selected"]]


def test_every_criterion_is_falsifiable():
    for c in M["criteria"]:
        assert c["kind"] in {"measurement", "hypothesis"}
        assert c["claim"].strip() and c["falsified_if"].strip() and c["evidence"].strip()


def test_measurements_support_criteria():
    m = M["measurements"]
    assert m["hot_4_shards_accepted"] > m["hot_1_shard_accepted"]
    assert m["event_p99_bytes"] < m["max_item_bytes"]
    assert m["boundary_rejects_plus1_without_partial_write"] is True
    assert m["failure_declared_without_partial_write"] is True
    assert m["compose_pins_dynamodb_local_digest"] is True


def test_discarded_alternative_is_declared():
    d = M["discarded_alternative"]
    assert d["store"] != M["selected"]
    assert d["store"] == "document"
    assert "COMPLETAR" not in d["store"] + d["reason"]
    assert "H2" in d["reason"]