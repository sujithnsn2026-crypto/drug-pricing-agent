"""Phases 2-5, built on the Ask agent's tool layer (dpa.agent):

  search.py       Phase 2: FDA label search (Azure AI Search, hybrid + semantic) as one more tool
  labels.py       Phase 2: build the label index
  mcp_server.py   Phase 3: dpa-mcp, the same read-only tools over the Model Context Protocol
  mcp_agent.py    an MCP client agent loop (used by Price-Watch)
  pricewatch.py   Phase 5: weekly multi-agent workflow (detect -> investigate x N -> verify -> human review)
  web.py          Ask chat + Price-Watch review screen
"""
