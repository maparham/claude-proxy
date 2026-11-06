"""Ask IndexNow search engines (Bing, Yandex, Seznam, …) to crawl every URL in the home host's sitemap.

    uv run python scripts/indexnow.py [https://rahkar.pro]

Run it after a deploy that adds or changes a page. The host must already serve /<INDEXNOW_KEY>.txt."""
import re
import sys
from urllib.parse import urlsplit

import httpx

from claude_proxy.web import INDEXNOW_KEY

base = (sys.argv[1] if len(sys.argv) > 1 else "https://rahkar.pro").rstrip("/")
host = urlsplit(base).hostname
urls = re.findall(r"<loc>([^<]+)</loc>", httpx.get(f"{base}/sitemap.xml", timeout=30).raise_for_status().text)
r = httpx.post("https://api.indexnow.org/indexnow", timeout=30, json={
    "host": host, "key": INDEXNOW_KEY, "keyLocation": f"{base}/{INDEXNOW_KEY}.txt", "urlList": urls})
print(r.status_code, f"{len(urls)} URLs", r.text[:300])
sys.exit(0 if r.status_code in (200, 202) else 1)
