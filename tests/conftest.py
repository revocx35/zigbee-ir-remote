import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "zigbee_ir_remote" / "app"))
import main  # noqa: E402


class FakeHA:
    def __init__(self):
        self.log = []
        self.connected = asyncio.Event()
        self.connected.set()

    async def mqtt_publish(self, topic, payload, retain=False):
        self.log.append((topic, payload, retain))

    async def unsubscribe(self, sid):
        pass

    async def call(self, payload, callback=None, timeout=15):
        t = payload["type"]
        if callback:
            return 1
        if t == "config/entity_registry/list":
            return [{"entity_id": "sensor.ir_learned_ir_code", "unique_id": "x_learned_ir_code", "device_id": "B1"}]
        if t == "config/device_registry/list":
            return [{"id": "B1", "name": "IR"}]
        if t == "get_config":
            return {"unit_system": {"temperature": "°C"}}
        if t == "config/area_registry/list":
            return []
        if t == "call_service":
            self.log.append(payload)
        return None


@pytest.fixture
def remote(tmp_path):
    r = main.IRRemote(FakeHA(), main.Store(tmp_path / "r.json"), dict(main.DEFAULT_OPTIONS))
    r.climate = main.Climate(r)
    return r
