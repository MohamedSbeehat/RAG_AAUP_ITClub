@echo off
setlocal
cd /d "%~dp0"
if not exist ".env" copy ".env.company.example" ".env" >nul
if not exist "documents" mkdir "documents"
where uv >nul 2>nul
if errorlevel 1 (
  echo Install uv first: winget install --id astral-sh.uv
  pause
  exit /b 1
)
uv sync --locked --no-dev
if errorlevel 1 (
  pause
  exit /b 1
)
echo Document RAG workspace: http://127.0.0.1:8000
echo Sign in or create a company account, then upload files in Documents.
echo Each company has its own folder, shown inside its workspace. Keep this window open.
uv run --no-sync rag-aaup serve --host 127.0.0.1 --port 8000
if errorlevel 1 pause
