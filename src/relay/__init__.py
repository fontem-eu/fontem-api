"""Wikidata EventStreams → Postgres relay.

Subscribes to Wikimedia's `recentchange` SSE stream and buffers every
wikidatawiki edit into ``wikidata.recentchange`` so we own retention
rather than depending on Kafka's 7-day rolling buffer. A downstream
worker (deployed later) reads from the buffer, fetches each affected
entity's current truthy via the Wikidata API, applies our predicate
whitelist + lang filter, and writes diff statements into Virtuoso.

Schema lives in the events Postgres (alongside ``events.entity_events``):
- ``wikidata.recentchange``  — append-only event log
- ``wikidata.relay_state``   — singleton row with the resume cursor

See ``wikidata_recentchange.py`` for the SSE consumer entry point.
"""
