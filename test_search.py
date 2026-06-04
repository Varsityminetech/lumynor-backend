from googlesearch import search
try:
    print("Testing Google Search...")
    results = search("latest fitness tech 2024", num_results=3, advanced=True)
    found = False
    for r in results:
        print(f"FOUND: {r.title}")
        found = True
    if not found:
        print("No results found via Google.")
except Exception as e:
    print(f"Google Error: {e}")

from langchain_community.tools import DuckDuckGoSearchRun
try:
    print("\nTesting DuckDuckGo...")
    ddg = DuckDuckGoSearchRun()
    res = ddg.invoke("latest fitness tech 2024")
    print(f"DDG RESULTS: {res[:200]}...")
except Exception as e:
    print(f"DDG Error: {e}")
