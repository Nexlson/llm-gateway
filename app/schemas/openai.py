from __future__ import annotations

import time
from typing import Iterable


def models_list_response(pool_names: Iterable[str], owned_by: str = "llm-gateway") -> dict:
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": created, "owned_by": owned_by}
            for name in pool_names
        ],
    }
