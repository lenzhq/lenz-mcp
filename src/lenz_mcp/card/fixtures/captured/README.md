# Synthetic API responses

The bodies in this directory are invented. Each file holds responses in the
exact shape the Lenz API returns (`POST /api/v1/assess`, `GET
/api/v1/verifications/{id}`), keyed by the scenario it reproduces: a two-,
four- or five-row list, a deep check with many sources, a long quote. Every
claim, summary, source and id is made up, and every link points at an
`example.org`, `example.com` or `example.net` address.

The bodies are checked against the API's response schemas, so a fixture cannot
drift from the real contract. `build_fixtures.py` turns them into the card's
fixtures by running them through the connector's own tool code.
