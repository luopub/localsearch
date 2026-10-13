@echo off
setlocal EnableExtensions

rem ============================================================================
rem  push-git.bat - stage everything, commit, and push to both remotes
rem ----------------------------------------------------------------------------
rem  Run from the project root:      .\push-git.bat
rem
rem  Remotes are created automatically if missing. If a remote already exists
rem  but points somewhere else, this script only WARNS - it never rewrites your
rem  remote URLs behind your back.
rem
rem  Pushing happens even when there is nothing to commit. That is deliberate:
rem  it is the recovery path when one remote succeeded and the other failed on
rem  the previous run. Just re-run the script.
rem
rem  ---------------------------------------------------------------------------
rem  Two encoding decisions worth knowing about.
rem
rem  1. This file is ASCII-only. cmd.exe reads a .bat in the OEM code page
rem     - 936 on a Chinese Windows - so non-ASCII text in the file itself is a
rem     dependable way to break a batch file.
rem
rem  2. There is no "chcp" here, deliberately.
rem
rem     An earlier draft called "chcp 65001" on the theory that a Chinese
rem     commit message would otherwise be captured as GBK bytes and stored by
rem     git mislabelled as UTF-8. That theory was wrong, and the test that
rem     appeared to confirm it was wrong too.
rem
rem     What actually happens: cmd holds its variables as UTF-16 and passes
rem     the command line to git as UTF-16. The console code page therefore
rem     does not decide what git receives. Typing Chinese on a cp936 console
rem     delivers GBK bytes to cmd, and the message still reaches git as
rem     correct UTF-8. Verified by piping GBK bytes into the prompt with no
rem     chcp anywhere - the message round-tripped intact.
rem
rem     Adding chcp would have cost real things: it flushes the console input
rem     buffer, so "push-git.bat < input.txt" silently falls back to the
rem     default message, and it leaves your console in UTF-8 afterwards.
rem     Paying that for a benefit that does not exist is a bad trade.
rem ============================================================================

set "URL_GITHUB=https://github.com/luopub/localsearch"
set "URL_GITEE=https://gitee.com/luopub/localsearch"
set "REMOTE_GITHUB=origin"
set "REMOTE_GITEE=gitee"

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 goto :not_a_repo

call :ensure_remote "%REMOTE_GITHUB%" "%URL_GITHUB%"
call :ensure_remote "%REMOTE_GITEE%"   "%URL_GITEE%"

echo.
echo === Current state ===
git status --short --branch
if errorlevel 1 goto :fail_status

echo.
echo === Staging ===
git add .
if errorlevel 1 goto :fail_add

rem "git diff --cached --quiet" exits 0 when nothing is staged, non-zero when
rem something is. Either way we continue - see the note about re-running above.
git diff --cached --quiet
if errorlevel 1 goto :do_commit

echo Nothing staged - skipping the commit.
goto :push

:do_commit
echo.
echo === Commit ===
echo Press Enter to use the default message.
set "MSG="
set /p "MSG=Commit message: "

if not defined MSG (
    for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm'"`) do set "MSG=chore: auto-commit %%t"
)
if not defined MSG set "MSG=chore: auto-commit"

rem Double quotes in the message would end the -m argument early. Strip them
rem rather than let git see a mangled message. Everything else - ampersands,
rem pipes, angle brackets - is safe because the argument is quoted.
set "MSG=%MSG:"=%"

echo Committing as: "%MSG%"
git commit -m "%MSG%"
if errorlevel 1 goto :fail_commit

:push
echo.
echo === Push ===
set "PUSH_RC=0"

rem No commit at all yet - an unborn branch. "git push HEAD" would fail with
rem "src refspec HEAD does not match any", which reads like a remote problem
rem but is not. Say what is actually wrong instead.
git rev-parse --verify HEAD >nul 2>&1
if errorlevel 1 goto :no_commits

echo.
echo --- pushing to %REMOTE_GITHUB%
echo     %URL_GITHUB%
git push -u %REMOTE_GITHUB% HEAD
if errorlevel 1 set "PUSH_RC=1"

echo.
echo --- pushing to %REMOTE_GITEE%
echo     %URL_GITEE%
git push %REMOTE_GITEE% HEAD
if errorlevel 1 set "PUSH_RC=1"

echo.
if "%PUSH_RC%"=="0" echo OK - both remotes are up to date.
if not "%PUSH_RC%"=="0" echo FAILED - at least one remote did not accept the push. Fix it and re-run; this script is safe to re-run.

goto :done


rem ---------------------------------------------------------------------------
rem  Subroutines
rem ---------------------------------------------------------------------------

:ensure_remote
rem %1 = remote name, %2 = expected url
git remote get-url %1 >nul 2>&1
if errorlevel 1 (
    echo Adding missing remote %~1
    git remote add %1 %2
    exit /b 0
)
set "CUR_URL="
for /f "delims=" %%u in ('git remote get-url %1') do set "CUR_URL=%%u"
rem %~2, not %2: the caller passes the url quoted, and %2 would keep those
rem quotes, so the comparison below would never match and every run would
rem print a bogus warning.
if /i "%CUR_URL%"=="%~2" exit /b 0
echo.
echo WARNING: remote %~1 points at
echo            %CUR_URL%
echo          but this script expects
echo            %~2
echo          Pushing to it anyway - check which repository that is.
echo.
exit /b 0


:not_a_repo
echo ERROR: not inside a git working tree.
echo        Run this from the project root, or "git init" first.
set "PUSH_RC=1"
goto :done

:fail_status
echo ERROR: "git status" failed.
set "PUSH_RC=1"
goto :done

:fail_add
echo ERROR: "git add ." failed. Nothing was committed or pushed.
set "PUSH_RC=1"
goto :done

:fail_commit
echo ERROR: "git commit" failed. Nothing was pushed.
echo        A pre-commit hook, or a missing user.name / user.email, are the
echo        usual causes. Run "git commit" by hand to see the full message.
set "PUSH_RC=1"
goto :done

:no_commits
echo ERROR: this repository has no commits yet, so there is nothing to push.
echo        Stage some files and try again - the commit step will create the
echo        first one.
set "PUSH_RC=1"
goto :done


:done
echo.
if "%PUSH_RC%"=="0" echo Finished.
if not "%PUSH_RC%"=="0" echo Finished with errors.
echo.
rem Remove the pause line if you ever call this script from another script.
pause
endlocal & exit /b %PUSH_RC%
