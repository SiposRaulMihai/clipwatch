@echo off
rem ClipWatch on Windows: run setup once (see README), then double-click this file.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (echo First follow the Windows install steps in README.md & pause & exit /b 1)
start "" http://127.0.0.1:8792
.venv\Scripts\python.exe server.py
