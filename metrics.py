"""Prometheus instrumentation.

Counters/histograms are updated at call sites; scrape-time gauges (upcoming
events, conflicts) are set in the /chronicle/metrics handler. This module
imports nothing from the rest of chronicle so every module can import it.
"""
from prometheus_client import Counter, Gauge, Histogram

SYNC_EVENTS = Counter(
    "chronicle_sync_events_processed_total",
    "Events processed by calendar sync", ["source"],
)
SYNC_ERRORS = Counter(
    "chronicle_sync_errors_total",
    "Calendar sync failures (includes not-authenticated)", ["source"],
)
SYNC_LAST_SUCCESS = Gauge(
    "chronicle_sync_last_success_timestamp_seconds",
    "Unix time of the last successful sync", ["source"],
)

LLM_REQUESTS = Counter(
    "chronicle_llm_requests_total",
    "LLM calls", ["backend", "outcome"],
)
LLM_LATENCY = Histogram(
    "chronicle_llm_latency_seconds",
    "LLM call latency", ["backend"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)

WEBHOOKS = Counter(
    "chronicle_webhook_notifications_total",
    "Calendar webhook deliveries", ["source", "outcome"],
)
AGENT_RUNS = Counter(
    "chronicle_agent_runs_total",
    "Tool-agent turns", ["outcome"],
)

UPCOMING_EVENTS = Gauge(
    "chronicle_upcoming_events",
    "Non-cancelled events in the next 7 days", ["source"],
)
CONFLICTS_24H = Gauge(
    "chronicle_conflicts_next_24h",
    "Deterministically detected conflicts in the next 24 hours",
)
