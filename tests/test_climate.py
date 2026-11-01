import asyncio

import main


def test_region_deleted_during_tick_does_not_republish_its_state(remote):
    climate = remote.climate
    region = climate.new_region()
    region["name"] = "Bedroom"
    remote.store.data["regions"].append(region)

    async def tick(r, now):
        remote.store.data["regions"].remove(r)  # the user deletes it while commands are being sent
        await climate.remove(r)

    climate._tick = tick

    async def go():
        try:
            await asyncio.wait_for(climate.run(), 0.3)
        except asyncio.TimeoutError:
            pass
    asyncio.run(go())
    state = [e for e in remote.ha.log if isinstance(e, tuple) and e[0] == f"irremote/climate/{region['id']}/state"]
    assert state[-1][1] == ""  # the retained state stays cleared
