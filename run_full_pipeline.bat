@echo off
setlocal EnableExtensions
title CVL Full Pipeline
cd /d "%~dp0"
chcp 65001 >nul 2>&1

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

REM ========== CONFIG ==========
REM Scraper production runs (1, 2, or 3):
set "SCRAPER_RUNS=2"
REM Resume mid-pipeline only if needed:
REM set "FROM_STEP=--from-step zeroclone"
REM ============================

%PY% scripts\run_full_pipeline.py --scraper-runs %SCRAPER_RUNS% %FROM_STEP%
exit /b %ERRORLEVEL%
