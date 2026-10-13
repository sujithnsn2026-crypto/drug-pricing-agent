"""dpa-mcp: the project's Model Context Protocol server.

Every agent (Ask agent, Price-Watch investigators, or any MCP client such as
VS Code / Copilot / Claude) gets its tools from here.

    python -m dpa.ai.mcp_server            # stdio transport
"""
from __future__ import annotations

import json
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from dpa.ai.backends import AzureSearch, LocalSearch, sql_backend
from dpa.ai.settings import AISettings
from dpa.ai.tools import Toolbox

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


def build_toolbox(s: AISettings | None = None) -> Toolbox:
    s = s or AISettings()
    search = None
    if s.backend == "local":
        path = os.path.join(s.local_dir, "labels.json")
        search = LocalSearch(json.load(open(path))) if os.path.exists(path) else None
    elif s.search_endpoint:
        from dpa.ai.llm import embedder

        search = AzureSearch(s, embedder(s))
    return Toolbox(sql_backend(s), search, s.max_rows)


def build_server(tb: Toolbox) -> MCPServer:
    server = MCPServer(
        name="dpa-mcp",
        instructions="Read-only tools over US drug pricing (CMS NADAC) and FDA shortage/label data.",
    )

    def out(d: dict) -> str:
        return json.dumps(d, default=str)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def describe_gold_tables() -> str:
        """Describe the gold tables and columns available to query_prices. Call this before writing SQL."""
        return out(tb.describe_gold_tables())

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def query_prices(sql: str) -> str:
        """Run ONE read-only SQL SELECT (Databricks SQL dialect) over gold tables: drug_price_current,
        fact_price_change, nadac_price_history, dq_results. A row limit is applied automatically."""
        return out(tb.query_prices(sql))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def price_history(ndc11: str) -> str:
        """Full NADAC price history (all SCD2 versions) for one 11-digit NDC."""
        return out(tb.price_history(ndc11))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def lookup_shortage(query: str) -> str:
        """FDA drug-shortage records for an 11-digit NDC or a drug name (partial match)."""
        return out(tb.lookup_shortage(query))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def search_labels(query: str, generic_name: str | None = None, section: str | None = None, top: int = 5) -> str:
        """Search FDA drug-label text (indications, dosage, warnings, interactions...). Returns passages
        with citations. Optional filters: generic_name, section (e.g. boxed_warning, indications_and_usage)."""
        return out(tb.search_labels(query, generic_name, section, top))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def data_freshness() -> str:
        """Latest NADAC file date loaded, active NDC count, and latest data-quality check results."""
        return out(tb.data_freshness())

    return server


def main() -> None:
    build_server(build_toolbox()).run("stdio")


if __name__ == "__main__":
    main()
