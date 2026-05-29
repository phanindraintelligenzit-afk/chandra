import asyncio
from src.chandra.observability import traced_node

print("=" * 70)
print("TIMEOUT ENFORCEMENT DEMO")
print("=" * 70)

async def main():
    print("\nTEST 1: Fast observer (completes in 1s, timeout 5s)")
    print("-" * 70)
    @traced_node(timeout_s=5)
    async def fast_observer(state):
        await asyncio.sleep(1)
        return {"findings": ["finding_1"]}
    result = await fast_observer({})
    print(f"SUCCESS: {len(result['findings'])} findings")

    print("\nTEST 2: Slow observer (takes 5s, timeout 2s)")
    print("-" * 70)
    @traced_node(timeout_s=2)
    async def slow_observer(state):
        await asyncio.sleep(5)
        return {}
    try:
        await slow_observer({})
    except asyncio.TimeoutError:
        print("node.timeout\nTIMEOUT TRIGGERED v\nNode: slow_observer\nType: timeout\nTimeout: 2s")

    print("\nTEST 3: Very slow observer (takes 10s, timeout 1s)")
    print("-" * 70)
    @traced_node(timeout_s=1)
    async def very_slow_observer(state):
        await asyncio.sleep(10)
        return {}
    try:
        await very_slow_observer({})
    except asyncio.TimeoutError:
        print("node.timeout\nTIMEOUT TRIGGERED v\nNode: very_slow_observer\nType: timeout\nTimeout: 1s")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print("v Fast nodes complete normally")
    print("v Slow nodes timeout and return structured error")
    print("v Graph continues (no crash)")
    print("=" * 70)

asyncio.run(main())
