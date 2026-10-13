@echo off
setlocal EnableExtensions

rem ============================================================================
rem  LocalSearch - scheduled incremental index launcher
rem ----------------------------------------------------------------------------
rem  Invoked by the Windows Task Scheduler task:
rem      \LocalSearch\DailyIncrementalIndex
rem
rem  The task passes the lsearch arguments, e.g.
rem      run-index.cmd index --prune
rem  Run with no arguments and it falls back to
rem      index --prune
rem
rem  Why a wrapper instead of putting everything in the task XML:
rem    - the log directory must exist before cmd.exe opens the redirect handle,
rem      otherwise the redirect fails and the run produces no evidence at all
rem    - the console log is rotated so it cannot grow without bound
rem    - the exit code is normalised: "finished, but some files failed" (A6) is
rem      a success, not a crash, and must not trip <RestartOnFailure>
rem    - it warns when HF_HOME is absent from the scheduler's environment
rem
rem  ASCII-only on purpose. cmd.exe reads .cmd in the OEM code page, and a UTF-8
rem  file carrying non-ASCII comments is a dependable way to break a batch file.
rem
rem  Also: no parentheses in any echo text that sits inside an if/for block.
rem  cmd.exe closes the block at the first unescaped ")" it sees, and the
rem  failure it reports points at the wrong line.
rem ============================================================================

rem --- paths: overridable, setdefault semantics ------------------------------
rem These three exist so the launcher can be relocated, or exercised on its own,
rem without editing this file. config/config.toml remains the single source of
rem truth for the application; LS_DATA_DIR governs only where this launcher
rem writes its console log. Keep the two in step if you ever move data_dir.
if not defined LS_PROJECT_DIR set "LS_PROJECT_DIR=D:\work\test\localsearch"
if not defined LS_DATA_DIR    set "LS_DATA_DIR=D:\localsearch-data"
if not defined LS_LSEARCH     set "LS_LSEARCH=%LS_PROJECT_DIR%\.venv\Scripts\lsearch.exe"

set "LOG_DIR=%LS_DATA_DIR%\logs"
set "LOG_FILE=%LOG_DIR%\scheduled-index.log"
set "LOG_MAX_BYTES=5242880"

set "ARGS=%*"
if "%ARGS%"=="" set "ARGS=index --prune"

for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"`) do set "TS=%%t"
if "%TS%"=="" set "TS=%DATE% %TIME%"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" 2>nul
if not exist "%LOG_DIR%" exit /b 2

rem --- rotate, keeping one previous generation -------------------------------
if exist "%LOG_FILE%" for %%F in ("%LOG_FILE%") do if %%~zF GTR %LOG_MAX_BYTES% move /y "%LOG_FILE%" "%LOG_FILE%.1" >nul 2>&1

if not exist "%LS_LSEARCH%" (
    >>"%LOG_FILE%" echo [%TS%] FATAL: lsearch.exe not found at "%LS_LSEARCH%"
    >>"%LOG_FILE%" echo [%TS%]        Run "uv sync" in "%LS_PROJECT_DIR%" first.
    exit /b 9009
)

if "%HF_HOME%"=="" (
    >>"%LOG_FILE%" echo [%TS%] WARNING: HF_HOME is not set in this context.
    >>"%LOG_FILE%" echo [%TS%]          The app falls back to %LS_DATA_DIR%\models\hf and may
    >>"%LOG_FILE%" echo [%TS%]          re-download the weights, 1-3 GB. Expected is the
    >>"%LOG_FILE%" echo [%TS%]          user-level HF_HOME. If it is missing, the task is
    >>"%LOG_FILE%" echo [%TS%]          probably running as SYSTEM or via S4U rather than
    >>"%LOG_FILE%" echo [%TS%]          as the logged-on user. See DEVELOPMENT_PLAN.md 4.8
)

cd /d "%LS_PROJECT_DIR%"

>>"%LOG_FILE%" echo.
>>"%LOG_FILE%" echo [%TS%] ==== start: lsearch %ARGS% ====

rem call, not a bare invocation: without it cmd hands control to another batch
rem file permanently and never comes back, so the "end" line never gets written.
call "%LS_LSEARCH%" %ARGS% >>"%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

>>"%LOG_FILE%" echo [%TS%] ==== end:   exit %RC% ====

rem --- exit code policy ------------------------------------------------------
rem  0    clean run
rem  1    run completed, but some files could not be indexed (see files.error_msg
rem       and A6). Deliberately reported as success so that <RestartOnFailure>
rem       does not fire on a routine per-file failure.
rem  2+   real failure (config, DB, model load) -> let <RestartOnFailure> retry.
if "%RC%"=="1" set "RC=0"

exit /b %RC%
