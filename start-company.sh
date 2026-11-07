#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -f .env ]; then cp .env.company.example .env; fi
mkdir -p documents
command -v uv >/dev/null 2>&1 || { echo "Install uv first: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
uv sync --locked --no-dev
echo "Document RAG workspace: http://127.0.0.1:8000"
echo "Sign in or create a company account, then upload files in Documents."
echo "Each company has its own folder, shown inside its workspace."
exec uv run --no-sync rag-aaup serve --host 127.0.0.1 --port 8000
