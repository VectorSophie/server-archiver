@echo off
REM Launches `archive live` with no console window, for Task Scheduler.
REM If pythonw.exe is not on PATH, replace "pythonw" below with its full
REM path (e.g. "C:\Python314\pythonw.exe").
cd /d "%~dp0"
pythonw.exe -m archiver.cli live
