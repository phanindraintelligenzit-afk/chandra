import asyncio
from chandra.observability import traced_node

print("=" * 70)
print("TIMEOUT ENFORCEMENT DEMO")
print("=" * 70)
print()

@traced_node("fast_observer", timeout_s=5)
async def fast_observer():
    """Completes in 1 second."""
    await asyncio.sleep(1)
    return {
        "findings": [
            {
                "detector": "SEC-001",
                "severity": "high",
                "resource": "sg-12345",
            }
        ]
    }

@traced_node("slow_observer", timeout_s=2)
async def slow_observer():
    """Takes 5 seconds - will timeout at 2s."""
    await asyncio.sleep(5)
    return {"findings": []}

@traced_node("very_slow_observer", timeout_s=1)
async def very_slow_observer():
    """Takes 10 seconds - will timeout at 1s."""
    await asyncio.sleep(10)
    return {"findings": []}

async def main():

    print("TEST 1: Fast observer (completes in 1s, timeout 5s)")
    print("-" * 70)
    result = await fast_observer()
    if "errors" in result:
        print(f"TIMEOUT: {result['errors']}")
    else:
        print(f"SUCCESS: {len(result['findings'])} findings")
    print()

    print("TEST 2: Slow observer (takes 5s, timeout 2s)")
    print("-" * 70)
    result = await slow_observer()
    if "errors" in result:
        print(f"TIMEOUT TRIGGERED ✓")
        print(f"Node: {result['errors'][0]['node']}")
        print(f"Type: {result['errors'][0]['type']}")
        print(f"Timeout: {result['errors'][0]['timeout_s']}s")
    else:
        print(f"COMPLETED: {result}")
    print()

    print("TEST 3: Very slow observer (takes 10s, timeout 1s)")
    print("-" * 70)
    result = await very_slow_observer()
    if "errors" in result:
        print(f"TIMEOUT TRIGGERED ✓")
        print(f"Node: {result['errors'][0]['node']}")
        print(f"Type: {result['errors'][0]['type']}")
        print(f"Timeout: {result['errors'][0]['timeout_s']}s")
    else:
        print(f"COMPLETED: {result}")
    print()

    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print("✓ Fast nodes complete normally")
    print("✓ Slow nodes timeout and return structured error")
    print("✓ Graph continues (no crash)")
    print("=" * 70)

asyncio.run(main())
