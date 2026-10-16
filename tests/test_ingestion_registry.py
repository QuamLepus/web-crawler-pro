"""Feed / sitemap parsing regressions.

Found while wiring up topic discovery: ``SeedProvider`` expands a user-supplied
seed through ``/sitemap.xml`` and got **zero** URLs back, for every sitemap.
The cause was in :data:`src.ingestion.registry._LOC_RE`, which meant every
``DataSourceKind.SITEMAP`` source silently produced nothing at all.

RSS 2.0 was broken the same way (``<link>https://…</link>`` has no ``href``),
so :func:`parse_rss_urls` could only ever read Atom feeds.
"""

from __future__ import annotations

from src.ingestion.registry import parse_rss_urls, parse_sitemap

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://rtings.com/laptop</loc></url>
  <url><loc>https://rtings.com/headphone</loc></url>
</urlset>"""

SITEMAP_WITH_CDATA = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc><![CDATA[https://rtings.com/monitor]]></loc></url>
</urlset>"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://rtings.com/sitemap-posts.xml</loc></sitemap>
  <sitemap><loc>https://rtings.com/sitemap-pages.xml</loc></sitemap>
</sitemapindex>"""

RSS2 = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><link>https://blog.example/post-a</link></item>
  <item><link>https://blog.example/post-b</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><link href="https://atom.example/1"/></entry>
</feed>"""


def test_sitemap_returns_every_loc():
    """The regression itself: this used to return ``([], [])``."""
    leaves, children = parse_sitemap(SITEMAP, "https://rtings.com/sitemap.xml")
    assert leaves == ["https://rtings.com/laptop",
                      "https://rtings.com/headphone"]
    assert children == []


def test_sitemap_handles_cdata_wrapped_locs():
    leaves, _ = parse_sitemap(SITEMAP_WITH_CDATA,
                              "https://rtings.com/sitemap.xml")
    assert leaves == ["https://rtings.com/monitor"]


def test_sitemap_index_yields_children_not_leaves():
    leaves, children = parse_sitemap(SITEMAP_INDEX,
                                     "https://rtings.com/sitemap.xml")
    assert leaves == []
    assert children == ["https://rtings.com/sitemap-posts.xml",
                        "https://rtings.com/sitemap-pages.xml"]


def test_relative_locs_are_dropped():
    """``parse_rss_urls`` only accepts absolute http(s) URLs.

    The sitemap spec requires absolute ``<loc>`` values anyway, and dropping
    relatives keeps ``javascript:`` / ``data:`` style junk out of the frontier.
    """
    relative = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>/laptop</loc></url></urlset>"""
    leaves, _ = parse_sitemap(relative, "https://rtings.com/sitemap.xml")
    assert leaves == []


def test_rss2_plain_link_elements_are_read():
    assert parse_rss_urls(RSS2) == ["https://blog.example/post-a",
                                    "https://blog.example/post-b"]


def test_atom_link_href_still_works():
    assert parse_rss_urls(ATOM) == ["https://atom.example/1"]


def test_non_urls_are_dropped():
    junk = "<urlset><url><loc>javascript:alert(1)</loc></url></urlset>"
    assert parse_rss_urls(junk) == []
