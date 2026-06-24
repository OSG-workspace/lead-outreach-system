"""Sanity check for ddgs (DuckDuckGo search)."""
import sys

try:
    from ddgs import DDGS
except ImportError:
    print("✗ ddgs not installed. Run ./tools/install.sh first.")
    sys.exit(1)

print("Testing DuckDuckGo search...")
try:
    with DDGS() as ddgs:
        results = list(ddgs.text("dental clinics New York", max_results=5))
except Exception as e:
    print(f"✗ Search failed: {e}")
    sys.exit(1)

if len(results) < 1:
    print("✗ Got 0 results. DDG may be temporarily blocking your IP.")
    sys.exit(1)

print(f"✓ Got {len(results)} results. Sample:")
for r in results[:3]:
    print(f"  - {r.get('title', '?')[:60]}")
    print(f"    {r.get('href', '?')}")

print()
print("✓ DDG search works")
