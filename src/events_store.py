import json, os, time
from collections import defaultdict

# Límites tomados de la documentación de DynamoDB (verificar antes de defender):
# 400 KB por ítem, 1000 escrituras/s por partición.
MAX_ITEM_BYTES = int(os.getenv("STORE_MAX_ITEM_BYTES", 400 * 1024))
PARTITION_WCU = int(os.getenv("STORE_PARTITION_WCU", 1000))


class ItemTooLarge(Exception): ...
class PartitionThrottled(Exception): ...
class StoreUnavailable(Exception): ...


class EventStore:
    """Emulador determinista en proceso para eventos CDRL (sin red ni credenciales)."""

    def __init__(self, clock=time.monotonic):
        self._data = defaultdict(dict)
        self._writes = defaultdict(list)
        self._clock = clock
        self.available = True

    def put(self, pk, sk, item):
        if not self.available:
            raise StoreUnavailable("store no disponible; escritura NO aplicada")
        size = len(json.dumps(item, separators=(",", ":")).encode())
        if size > MAX_ITEM_BYTES:
            raise ItemTooLarge(f"{size} > {MAX_ITEM_BYTES}")
        now = self._clock()
        recent = [t for t in self._writes[pk] if now - t < 1.0]
        if len(recent) >= PARTITION_WCU:
            raise PartitionThrottled(f"partición {pk} excede {PARTITION_WCU}/s")
        recent.append(now)
        self._writes[pk] = recent
        self._data[pk][sk] = item

    def get(self, pk, sk):
        if not self.available:
            raise StoreUnavailable("store no disponible")
        return self._data[pk].get(sk)

    def query(self, pk, sk_from="", sk_to="\uffff"):
        return [v for k, v in sorted(self._data[pk].items()) if sk_from <= k <= sk_to]