import datetime, json, random, re, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.events_store_model import (EventStore, ItemTooLarge, PartitionThrottled,
                                    StoreUnavailable, MAX_ITEM_BYTES, PARTITION_WCU)

rng = random.Random(42)  # semilla fija: siempre da lo mismo
size = lambda o: len(json.dumps(o, separators=(",", ":")).encode())

# 1) Tamano de eventos sinteticos
events = [{"device": f"dev{rng.randint(1, 50)}", "type": rng.choice(["TEMP", "HUM", "ALERT"]),
           "v": rng.random() * 100, "tags": ["t"] * rng.randint(0, 40)} for _ in range(5000)]
sizes = sorted(size(e) for e in events)
p99 = sizes[int(len(sizes) * 0.99)]

# 2) Particion caliente: 4*WCU escrituras en el mismo segundo, 1 clave vs 4 shards
def accepted(shards):
    s, ok = EventStore(clock=lambda: 0.0), 0
    for i in range(4 * PARTITION_WCU):
        try:
            s.put(f"hot#{i % shards}", f"e{i}", {"i": i})
            ok += 1
        except PartitionThrottled:
            pass
    return ok


hot_1, hot_4 = accepted(1), accepted(4)

# 3) Borde de tamano
item = lambda n: {"p": "x" * (n - size({"p": ""}))}
s = EventStore(clock=lambda: 0.0)
s.put("k", "a", item(MAX_ITEM_BYTES))
try:
    s.put("k", "b", item(MAX_ITEM_BYTES + 1))
    boundary_ok = False
except ItemTooLarge:
    boundary_ok = s.get("k", "b") is None

# 4) Fallo declarado
s = EventStore(clock=lambda: 0.0)
s.available = False
try:
    s.put("k", "a", {})
    failure_ok = False
except StoreUnavailable:
    s.available = True
    failure_ok = s.get("k", "a") is None

# 5) C5: el compose fija dynamodb-local por digest (medicion sobre el repo real)
compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8", errors="replace")
pinned = bool(re.search(r"amazon/dynamodb-local:[\w.\-]+@sha256:[0-9a-f]{64}", compose))

M = {"event_p50_bytes": statistics.median(sizes), "event_p99_bytes": p99,
     "max_item_bytes": MAX_ITEM_BYTES, "partition_wcu": PARTITION_WCU,
     "hot_1_shard_accepted": hot_1, "hot_4_shards_accepted": hot_4,
     "writes_attempted": 4 * PARTITION_WCU,
     "boundary_rejects_plus1_without_partial_write": boundary_ok,
     "failure_declared_without_partial_write": failure_ok,
     "compose_pins_dynamodb_local_digest": pinned}

LABELS = {"dynamodb": "DynamoDB (column / particion + rango)",
          "document": "Document (MongoDB / DocumentDB)",
          "graph": "Graph (Neo4j / Neptune)",
          "object": "Object (S3 + Parquet + Athena)"}

# Pesos y puntajes: los de la tabla de docs/ADR-004-decision-nosql.md (puntuacion 1-5).
C = [
 ("C1", 0.30, "hypothesis",
  "Q1/Q3 (ultima N por equipo, orden temporal) se resuelven con particion + rango sin sort posterior",
  "una consulta Q1/Q3 requiere sort o scan posterior (H1 del ADR-004)", "Q1/Q3 en src/queries.py; H1",
  {"dynamodb": 5, "document": 4, "graph": 2, "object": 1}),
 ("C2", 0.15, "hypothesis",
  "Q2 (agregacion por ventana) es el punto debil de DynamoDB; el ADR lo declara",
  "H2 se refuta con numeros duros: la agregacion en servidor pasa a ganar la matriz", "Q2 en src/queries.py; H2",
  {"dynamodb": 2, "document": 5, "graph": 2, "object": 5}),
 ("C3", 0.15, "measurement",
  f"Escritura append-only sostenida: con 4 shards se aceptan {hot_4} de {4 * PARTITION_WCU} escrituras vs {hot_1} con una sola particion (modelo)",
  "hot_4_shards_accepted <= hot_1_shard_accepted, o H3 del ADR-004 se refuta", "H3; tests/test_events_store.py",
  {"dynamodb": 5, "document": 4, "graph": 2, "object": 3}),
 ("C4", 0.10, "hypothesis",
  "Alta idempotente y convergencia mediante ConditionExpression (ADR-002)",
  "un alta duplicada crea una segunda fila (H4 del ADR-004)", "ADR-002 + ConditionExpression; H4",
  {"dynamodb": 4, "document": 4, "graph": 3, "object": 1}),
 ("C5", 0.15, "measurement",
  "docker-compose.yml fija dynamodb-local por digest y make verify lo levanta",
  "compose_pins_dynamodb_local_digest es false", "docker-compose.yml; ADR-000",
  {"dynamodb": 5, "document": 3, "graph": 2, "object": 4}),
 ("C6", 0.10, "measurement",
  "Un fallo del store se declara y un item sobre el limite se rechaza, ambos sin escritura parcial",
  "falla test_declared_failure_store_unavailable_no_silent_loss o test_limit_item_at_max_size_boundary",
  "Prueba de fallo declarado de M04",
  {"dynamodb": 4, "document": 3, "graph": 3, "object": 4}),
 ("C7", 0.05, "hypothesis",
  "make verify ya levanta DynamoDB Local; otro motor anade un servicio y una ruta de fallo",
  "el compose necesita servicios adicionales para DynamoDB", "scripts/verify_base.sh",
  {"dynamodb": 4, "document": 3, "graph": 2, "object": 3}),
]
assert abs(sum(c[1] for c in C) - 1) < 1e-9
crit = [{"id": i, "weight": w, "kind": k, "claim": cl, "falsified_if": f, "evidence": ev, "scores": sc}
        for i, w, k, cl, f, ev, sc in C]
tot = {st: round(sum(c["weight"] * c["scores"][st] for c in crit), 3) for st in LABELS}

ranked = sorted(tot.values(), reverse=True)
best = ranked[0]
winners = sorted(st for st, t in tot.items() if t == best)
assert len(winners) == 1, "hay empate: documentar regla de desempate"
selected = winners[0]
margin = round(ranked[0] - ranked[1], 3)

out = {"schema": "nosql-matrix/2",
       "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
       "synthetic_fixtures": True, "seed": 42,
       "source_of_weights": "docs/ADR-004-decision-nosql.md (tabla de criterios, puntuacion 1-5)",
       "note": "El emulador en proceso modela limites de DynamoDB; DynamoDB Local no simula throttling por particion.",
       "scope_note": "Las mediciones respaldan C3, C5 y C6 sobre el modelo de DynamoDB. Los puntajes de los otros stores son juicio documentado en el ADR, no medicion.",
       "stores": LABELS, "measurements": M, "criteria": crit, "totals": tot,
       "tie": False, "tied_stores": winners, "tie_break": None,
       "selected": selected, "margin_vs_runner_up": margin,
       "discarded_alternative": {
           "store": "document",
           "reason": "Es la que mas cerca quedo (margen estrecho). Gana C2 con claridad porque $group agrega en servidor, pero pierde en C5: el compose ya fija dynamodb-local por digest y Learner Lab expone DynamoDB, asi que un segundo motor anade servicio, cliente y ruta de fallo a make verify sin cambiar Q1/Q3. Se reabre si H2 se refuta con ventanas de agregacion grandes."}}

(ROOT / "artifacts").mkdir(exist_ok=True)
(ROOT / "artifacts" / "nosql-matrix.json").write_text(
    json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps(tot), "->", selected, "margen", margin)