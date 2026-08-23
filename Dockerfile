# etfedge-mcp — owner-only read-only MCP server for etfedge.xyz
FROM python:3.13-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml ./
COPY mcp_server.py mcp_tools.py ./

# Install runtime deps directly (avoid editable install — flat layout has no package).
RUN pip install --no-cache-dir \
    "fastmcp>=2.0" "uvicorn[standard]>=0.30" "sqlalchemy>=2.0" "psycopg[binary]>=3.2"

EXPOSE 8000

CMD ["python3", "mcp_server.py"]
