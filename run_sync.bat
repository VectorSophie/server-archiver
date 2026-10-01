@echo off
REM Pushes changed archive data to the deployed remote mirror.
REM If python.exe is not on PATH, replace "python" below with its full path.
cd /d "%~dp0"
python -m archiver.cli sync
