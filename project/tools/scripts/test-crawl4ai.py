"""Sanity check for crawl4ai."""
import asyncio
import sys

try:
    from crawl4ai import AsyncWebCrawler
except ImportError:
    print("✗ crawl4ai not installed. Run ./tools/install.sh first.")
    sys.exit(1)


async def main():
    print("Testing crawl4ai on https://example.com...")
    try:
        async with AsyncWebCrawler(headless=True, verbose=False) as crawler:
            result = await crawler.arun(url="https://example.com")
    except Exception as e:
        print(f"✗ Crawl failed: {e}")
        print("  If this mentions Playwright/Chromium, run: playwright install chromium")
        sys.exit(1)

    if not result.success:
        print(f"✗ Crawl returned not-success: {result.error_message}")
        sys.exit(1)

    md = result.markdown or ""
    if len(md) < 50:
        print(f"✗ Got only {len(md)} chars of markdown. Something is wrong.")
        sys.exit(1)

    print(f"✓ Got {len(md)} chars of markdown. First 200 chars:")
    print("  " + md[:200].replace("\n", " ").strip())
    print()
    print("✓ crawl4ai works")


if __name__ == "__main__":
    asyncio.run(main())
