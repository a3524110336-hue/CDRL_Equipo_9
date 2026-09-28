import datetime, json, random, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.events_store_model import (EventStore, ItemTooLarge, PartitionThrottled,
                              StoreUnavailable, MAX_ITEM_BYTES, PARTITION_WCU)

rng = random.Random(42)  # semilla fija: siempre da lo mismo
size = lambda o: len(json.dumps(o, separators=(",", ":")).encode())

# 1) Tamaño de eventos sintéticos
events = [{"device": f"dev{rng.randint(1, 50)}", "type": rng.choice(["TEMP", "HUM", "ALERT"]),
           "v": rng.random() * 100, "tags": ["t"] * rng.randint(0, 40)} for _ in range(5000)]
sizes = sorted(size(e) for e in events)
p99 = sizes[int(len(sizes) * 0.99)]

# 2) Partición caliente: 4*WCU escrituras en el mismo segundo, 1 clave vs 4 shards
def accepted(shards):
    s, ok = EventStore(clock=lambda: 0.0), 0
    for i in range(4 * PARTITION_WCU):
        try:
            s.put(f"hot#{i % shards}", f"e{i}", {"i": i}); ok += 1
        except PartitionThrottled:
            pass
    return ok
hot_1, hot_4 = accepted(1), accepted(4)

# 3) Borde de tamaño
item = lambda n: {"p": "x" * (n - size({"p": ""}))}
s = EventStore(clock=lambda: 0.0)
s.put("k", "a", item(MAX_ITEM_BYTES))
try:
    s.put("k", "b", item(MAX_ITEM_BYTES + 1)); boundary_ok = False
except ItemTooLarge:
    boundary_ok = s.get("k", "b") is None

# 4) Fallo declarado
s = EventStore(clock=lambda: 0.0); s.available = False
try:
    s.put("k", "a", {}); failure_ok = False
except StoreUnavailable:
    s.available = True
    failure_ok = s.get("k", "a") is None

M = {"event_p50_bytes": statistics.median(sizes), "event_p99_bytes": p99,
     "max_item_bytes": MAX_ITEM_BYTES, "partition_wcu": PARTITION_WCU,
     "hot_1_shard_accepted": hot_1, "hot_4_shards_accepted": hot_4,
     "writes_attempted": 4 * PARTITION_WCU,
     "boundary_rejects_plus1_without_partial_write": boundary_ok,
     "failure_declared_without_partial_write": failure_ok}

# Pesos (suman 1.0) y puntajes 1-5: JUICIO del equipo, no medición. Ajústalos con el ADR.
C = [
 ("query_fit", 0.30, "hypothesis", "El acceso dominante es por (dispositivo, rango de tiempo)",
  "más del 10% de las consultas del ADR requieren travesías multi-salto",
  {"document": 4, "graph": 2, "column": 4, "object": 1}),
 ("item_size", 0.15, "measurement", f"El p99 del evento ({p99} B) cabe en {MAX_ITEM_BYTES} B",
  "p99 mayor al límite de ítem", {"document": 4, "graph": 3, "column": 4, "object": 5}),
 ("hot_partition", 0.20, "measurement",
  f"Con sharding se aceptan {hot_4} de {4 * PARTITION_WCU} escrituras vs {hot_1} con una sola clave",
  "hot_4_shards_accepted <= hot_1_shard_accepted", {"document": 3, "graph": 2, "column": 4, "object": 4}),
 ("consistency", 0.10, "hypothesis", "Lecturas fuertes por clave bastan para los eventos CDRL",
  "un caso de uso del ADR exige transacciones multi-ítem", {"document": 4, "graph": 4, "column": 3, "object": 3}),
 ("cost", 0.15, "hypothesis", "El costo por millón de escrituras es menor en document/object que en graph",
  "una estimación con precios vigentes lo contradice", {"document": 3, "graph": 2, "column": 3, "object": 5}),
 ("failure", 0.10, "measurement", "El fallo del store se declara sin escritura parcial",
  "test_declared_failure_store_unavailable_no_silent_loss falla", {"document": 4, "graph": 3, "column": 3, "object": 4}),
]
assert abs(sum(c[1] for c in C) - 1) < 1e-9
crit = [{"id": i, "weight": w, "kind": k, "claim": cl, "falsified_if": f, "scores": sc}
        for i, w, k, cl, f, sc in C]
tot = {st: round(sum(c["weight"] * c["scores"][st] for c in crit), 3)
       for st in ("document", "graph", "column", "object")}

best = max(tot.values())
winners = sorted(st for st, t in tot.items() if t == best)
TIE_BREAK = {
    "chosen": "document",
    "rule": "Menor riesgo operativo y mayor reproducibilidad: docker-compose.yml ya incluye "
            "DynamoDB Local (imagen fijada por digest), por lo que un document store se "
            "verifica en make verify sin servicios adicionales.",
    "falsified_if": "un column store con emulador local se levanta en make verify sin "
                    "cambiar puertos, credenciales ni tiempos de arranque",
}
if len(winners) > 1:
    assert TIE_BREAK["chosen"] in winners
    selected = TIE_BREAK["chosen"]
else:
    selected = winners[0]

out = {"schema": "nosql-matrix/1",
       "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
       "synthetic_fixtures": True, "seed": 42,
       "note": "El emulador modela límites de DynamoDB; DynamoDB Local no simula throttling por partición.",
       "scope_note": "Las mediciones validan el modelo de un document store. Los puntajes de "
                     "graph, column y object en criterios measurement son juicio del equipo, no medición.",
       "measurements": M, "criteria": crit, "totals": tot,
       "tie": len(winners) > 1, "tied_stores": winners,
       "tie_break": TIE_BREAK if len(winners) > 1 else None,
       "selected": selected,
       "discarded_alternative": {
           "store": "column",
           "reason": "Empató con document (3.65). Se descartó por riesgo operativo: no hay "
                     "emulador de column store en docker-compose.yml y añadirlo complicaría "
                     "make verify (puertos, arranque, credenciales), mientras DynamoDB Local "
                     "ya está integrado y fijado por digest."}}

(ROOT / "artifacts").mkdir(exist_ok=True)
(ROOT / "artifacts" / "nosql-matrix.json").write_text(
    json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps(tot), "->", selected)