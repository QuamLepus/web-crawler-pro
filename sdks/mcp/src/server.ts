#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { WebIntelligenceClient } from "@webintelligence/sdk";
import { z } from "zod";

function result(value: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }] };
}

function failure(error: unknown) {
  return { content: [{ type: "text" as const,
    text: error instanceof Error ? error.message : "Web Intelligence request failed" }],
    isError: true };
}

export function buildServer(client: WebIntelligenceClient) {
  const server = new McpServer({ name: "web-intelligence-os", version: "0.1.0" });

  server.tool("wio_ask", "Answer from the authenticated tenant's existing indexed sources; this does not start a crawl.", {
    question: z.string().min(3).max(4096),
    max_pages: z.number().int().min(1).max(500).optional(),
    max_seconds: z.number().int().min(1).max(3600).optional(),
  }, async ({ question, max_pages, max_seconds }) => {
    try { return result(await client.ask(question, { maxPages: max_pages, maxSeconds: max_seconds })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_research_create", "Start an asynchronous, source-cited research job. Reports contain extractive citations, not independent fact-checking.", {
    topic: z.string().min(3).max(512),
    sources: z.number().int().min(1).max(20).optional(),
    max_pages_per_source: z.number().int().min(1).max(10).optional(),
  }, async ({ topic, sources, max_pages_per_source }) => {
    try { return result(await client.createResearch({ topic, sources,
      maxPagesPerSource: max_pages_per_source })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_research_status", "Get a research job's stage, report, and citations.", {
    research_id: z.string().regex(/^research_[a-f0-9]{20}$/),
  }, async ({ research_id }) => {
    try { return result(await client.getResearch(research_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_crawl_create", "Queue a crawl job. Robots.txt is obeyed by default.", {
    url: z.string().url(), max_pages: z.number().int().min(1).max(1000).optional(),
  }, async ({ url, max_pages }) => {
    try { return result(await client.createCrawl(url, { maxPages: max_pages, obeyRobots: true })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_watch_create", "Create a recurring single-page change watch (minimum 15 minutes).", {
    url: z.string().url(), interval_minutes: z.number().int().min(15).max(10080).optional(),
  }, async ({ url, interval_minutes }) => {
    try { return result(await client.createWatch({ url, intervalMinutes: interval_minutes })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_watch_changes", "Read detected text changes for a monitored page.", {
    watch_id: z.string().regex(/^watch_[a-f0-9]{20}$/),
  }, async ({ watch_id }) => {
    try { return result(await client.getWatchChanges(watch_id)); }
    catch (error) { return failure(error); }
  });

  return server;
}

async function main() {
  const apiKey = process.env.WCP_API_KEY;
  if (!apiKey) throw new Error("Set WCP_API_KEY in the MCP server environment.");
  const client = new WebIntelligenceClient({ apiKey,
    baseUrl: process.env.WCP_BASE_URL ?? "http://localhost:8000" });
  const server = buildServer(client);
  await server.connect(new StdioServerTransport());
}

if (require.main === module) {
  main().catch((error) => {
    // MCP stdio reserves stdout for protocol frames.
    console.error(error instanceof Error ? error.message : "MCP server startup failed");
    process.exitCode = 1;
  });
}
