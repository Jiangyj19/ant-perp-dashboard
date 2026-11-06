"""Explicitly selected official-source watches; content changes never edit conversions."""
import hashlib
import time

import httpx


class RuleWatcher:
    def __init__(self, store):
        self.store = store
        self.last_run = 0
        self.client = httpx.AsyncClient(timeout=15, follow_redirects=False, headers={
            "User-Agent": "ANTMonitor/1.0 read-only official-rule-watch"})

    async def poll(self, config):
        now = int(time.time() * 1000)
        if now - self.last_run < 300000:
            return
        self.last_run = now
        for source in config.get("rules", {}).get("sources", []):
            if not source.get("enabled", False):
                continue
            url = source["url"]
            try:
                # Redirects are not followed, preventing an official URL from redirecting to private hosts.
                async with self.client.stream("GET", url) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 2000000:
                            raise ValueError("document exceeds limit")
                body = content.decode("utf-8", errors="replace")
                digest = hashlib.sha256(content).hexdigest()
                old = self.store.db.execute("SELECT digest FROM rule_documents WHERE url=?", (url,)).fetchone()
                if old and old[0] != digest:
                    self.store.event("rule_change", {"code": "RULE_SOURCE_CHANGED", "severity": "critical",
                        "title": "官方规则来源内容变更，待人工复核", "message": "页面哈希发生变化，可能包括动态内容；不代表已确认规则修改。换算参数未自动更新。",
                        "source": url, "value": digest, "threshold": old[0], "formula": "SHA256(官方页面原文) 与上次抓取比较"})
                    self.store.set("rule_unreviewed", True)
                self.store.db.execute("INSERT OR REPLACE INTO rule_documents VALUES (?,?,?,?)", (url, now, digest, body))
                self.store.db.commit()
            except Exception:
                self.store.set("rule_fetch_status:" + url, {"status": "failed", "timestamp": now})

    async def close(self):
        await self.client.aclose()
