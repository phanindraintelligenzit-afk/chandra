"""Per-node standalone test scripts for the Chandra LangGraph pipeline.

Each module in this package exercises exactly one node from
``src.chandra/graphs/chandra_graph.py`` by constructing a small
``ChandraState`` input, invoking the node, and printing a readable
summary of the node's return value to stdout.

Run any single script directly:

    uv run python -m tests.test_nodes.onboard_account

Or run them all in order to validate the full pipeline end-to-end:

    uv run python -m tests.test_nodes.run_all

Every script is self-contained: it sets the required moto / dummy AWS
environment variables, mocks Bedrock where the composer is involved,
and never reaches the real cloud.
"""
