import json, sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.events_store import (EventStore, ItemTooLarge, PartitionThrottled,
                              StoreUnavailable, MAX_ITEM_BYTES, PARTITION_WCU)


def _item_of_size(n):
    base = len(json.dumps({"p": ""}, separators=(",", ":")).encode())
    return {"p": "x" * (n - base)}


def test_normal_put_get_query():
    s = EventStore(clock=lambda: 0.0)
    s.put("dev#1", "2026-01-01T00:00:00Z", {"type": "TEMP", "v": 21.5})
    s.put("dev#1", "2026-01-01T00:00:01Z", {"type": "TEMP", "v": 21.7})
    assert s.get("dev#1", "2026-01-01T00:00:00Z")["v"] == 21.5
    assert len(s.query("dev#1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")) == 2


def test_limit_hot_partition_throttles_and_sharding_mitigates():
    s = EventStore(clock=lambda: 0.0)  # reloj congelado: mismo segundo
    for i in range(PARTITION_WCU):
        s.put("hot", f"e{i}", {"i": i})
    with pytest.raises(PartitionThrottled):
        s.put("hot", "overflow", {"i": -1})
    for i in range(PARTITION_WCU):  # mitigación: 4 shards absorben la misma carga
        s.put(f"hot#{i % 4}", f"e{i}", {"i": i})


def test_limit_item_at_max_size_boundary():
    s = EventStore(clock=lambda: 0.0)
    s.put("k", "ok", _item_of_size(MAX_ITEM_BYTES))            # exacto: acepta
    with pytest.raises(ItemTooLarge):
        s.put("k", "big", _item_of_size(MAX_ITEM_BYTES + 1))   # +1 byte: rechaza
    assert s.get("k", "big") is None                            # sin escritura parcial


def test_declared_failure_store_unavailable_no_silent_loss():
    s = EventStore(clock=lambda: 0.0)
    s.available = False
    with pytest.raises(StoreUnavailable):
        s.put("k", "s", {"a": 1})
    s.available = True
    assert s.get("k", "s") is None