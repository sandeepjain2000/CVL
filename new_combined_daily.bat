@echo off
setlocal EnableExtensions EnableDelayedExpansion
title CVL Daily Combined - Zeroclone + Campaign
cd /d "%~dp0"

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

set "CVL_DIR=%~dp0"
set "ZEROCLONE_DIR=%~dp0..\zeroclone"

:MENU
cls
echo.
echo  ========================================================================
echo   DAILY COMBINED MENU  (no scraper)
echo   CVL:    %CVL_DIR%
echo   Clone:  %ZEROCLONE_DIR%
echo  ========================================================================
echo.
echo   [1] Zeroclone run_cycle     Zeroclone_Menu [1]-^>[1]  / run_cycle.bat
echo   [2] CVL check bounces       CVL_Menu [5]  check_bounces.py
echo   [3] CVL pipeline summary    reads pre-saved DB snapshot  instant
echo   [4] CVL send campaigns      CVL_Menu [2]-^>[2]  send_linkedin -n 5
echo   [5] CVL pool sender         send_validated_pool.py  validated leftovers
echo   [0] Exit
echo.
echo   Suggested order:  [2] -^> [1] -^> [3] -^> [4] -^> [5] optional
echo.
set "CHOICE="
set /p CHOICE="  Select option: "
if "%CHOICE%"=="1" goto T1_ZEROCLONE
if "%CHOICE%"=="2" goto T2_BOUNCES
if "%CHOICE%"=="3" goto T3_SUMMARY
if "%CHOICE%"=="4" goto T4_SEND
if "%CHOICE%"=="5" goto T5_POOL_MENU
if "%CHOICE%"=="0" exit /b 0
goto MENU

:T1_ZEROCLONE
cls
echo.
echo  Running Zeroclone run_cycle (extract + validate + DB + views)...
echo.
if not exist "%ZEROCLONE_DIR%\run_cycle.bat" (
  echo  ERROR: Not found: %ZEROCLONE_DIR%\run_cycle.bat
  goto PAUSE_MENU
)
pushd "%ZEROCLONE_DIR%"
call run_cycle.bat
set "ZC_ERR=!ERRORLEVEL!"
popd
if "!ZC_ERR!"=="" set "ZC_ERR=0"
echo.
echo  ------------------------------------------------------------------------
if not "!ZC_ERR!"=="0" (
  echo  Zeroclone finished with exit code !ZC_ERR!.
) else (
  echo  Zeroclone run_cycle finished successfully.
)
echo  Press any key to return to daily menu...
pause
goto MENU

:T2_BOUNCES
cls
echo.
echo  Running check_bounces.py - IMAP scan all Gmail profiles...
echo.
call %PY% check_bounces.py
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" echo  check_bounces.py finished with exit code %ERR%.
goto PAUSE_MENU

:T3_SUMMARY
cls
echo.
echo  Pipeline summary from pre-saved DB snapshot - instant...
echo  Snapshot refreshes after bounces [2] or zeroclone [1]
echo.
call %PY% scripts\print_pipeline_summary.py
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" echo  summary finished with exit code %ERR%.
goto PAUSE_MENU

:T4_SEND
cls
echo.
echo  Running send_linkedin_campaigns_params.py - ALL countries, 5 per profile...
echo.
call %PY% send_linkedin_campaigns_params.py -n 5
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" echo  send finished with exit code %ERR%.
goto PAUSE_MENU

:T5_POOL_MENU
cls
echo.
echo  VALIDATED POOL SENDER - allowlisted addresses not yet in email_attempts
echo  Optional after main campaign [4] to drain pool_sendable leftovers.
echo.
echo   [1] Default - 5 per Gmail profile
echo   [2] Ignore progress JSON - use when progress file blocks valid leftovers
echo   [0] Back to daily menu
echo.
set "PCH="
set /p PCH="  Select option: "
if "%PCH%"=="1" goto T5_POOL_DEFAULT
if "%PCH%"=="2" goto T5_POOL_IGNORE_JSON
if "%PCH%"=="0" goto MENU
goto T5_POOL_MENU

:T5_POOL_DEFAULT
cls
echo.
echo  Running send_validated_pool.py - 5 per profile...
echo.
call %PY% send_validated_pool.py -n 5
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" echo  pool send finished with exit code %ERR%.
goto PAUSE_MENU

:T5_POOL_IGNORE_JSON
cls
echo.
echo  Running send_validated_pool.py - 5 per profile, ignore progress JSON...
echo.
call %PY% send_validated_pool.py -n 5 --ignore-progress-json
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" echo  pool send finished with exit code %ERR%.
goto PAUSE_MENU

:PAUSE_MENU
echo.
echo  ------------------------------------------------------------------------
echo  Script finished. Results retained on screen.
echo  Press any key to return to daily menu...
pause
goto MENU

