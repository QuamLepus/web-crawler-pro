"""Elasticsearch bulk indexer."""

from __future__ import annotations

import os


class ElasticsearchExporter:
    def __init__(self, hosts: str | None = None, index: str = "crawl-items"):
        self.hosts = [hosts or os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")]
        self.index = index
        self._es = None

    @property
    def es(self):
        if self._es is None:
            from elasticsearch import Elasticsearch

            self._es = Elasticsearch(self.hosts, request_timeout=30)
            if not self._es.indices.exists(index=self.index):
                self._es.indices.create(index=self.index, mappings={
                    "properties": {
                        "url": {"type": "keyword"},
                        "title": {"type": "text"},
                        "text": {"type": "text"},
                        "fetched_at": {"type": "date"},
                        "status_code": {"type": "integer"},
                        "depth": {"type": "integer"},
                    }
                })
        return self._es

    def index_item(self, item: dict) -> str:
        doc_id = item.get("url", "").replace("/", "_")[:200]
        self.es.index(index=self.index, id=doc_id, document=item,
                      refresh=False)
        return doc_id

    def bulk_index(self, items: list[dict]) -> int:
        from elasticsearch.helpers import bulk

        actions = [{"_index": self.index, "_id": i["url"][:200], "_source": i}
                   for i in items if i.get("url")]
        success, _ = bulk(self.es, actions, raise_on_error=False)
        return success
