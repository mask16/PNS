@echo off
chcp 65001 >nul
echo ========================================
echo Fix Remote and Push to GitHub
echo ========================================
echo.

cd /d "%~dp0"

REM Find Git executable
set GIT_CMD=
if exist "C:\Program Files\Git\cmd\git.exe" (
    set "GIT_CMD=C:\Program Files\Git\cmd\git.exe"
) else if exist "C:\Program Files (x86)\Git\cmd\git.exe" (
    set "GIT_CMD=C:\Program Files (x86)\Git\cmd\git.exe"
) else if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" (
    set "GIT_CMD=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"
) else if exist "%ProgramFiles%\Git\cmd\git.exe" (
    set "GIT_CMD=%ProgramFiles%\Git\cmd\git.exe"
) else (
    where git >nul 2>&1
    if %errorlevel% equ 0 (
        set "GIT_CMD=git"
    )
)

if "%GIT_CMD%"=="" (
    echo [ERROR] Git is not found.
    pause
    exit /b 1
)

echo [1/5] Removing old remote...
"%GIT_CMD%" remote remove origin 2>nul

echo [2/5] Adding correct remote...
"%GIT_CMD%" remote add origin https://github.com/mask16/pns.git

echo [3/5] Verifying remote...
"%GIT_CMD%" remote -v

echo [4/5] Adding all files...
"%GIT_CMD%" add .

echo [5/5] Pushing to GitHub...
set GIT_ASKPASS=echo
set GIT_TERMINAL_PROMPT=0
"%GIT_CMD%" push -u origin main

if errorlevel 1 (
    echo.
    echo ========================================
    echo Push Failed
    echo ========================================
    echo.
    echo Trying to force push (if repository is empty)...
    "%GIT_CMD%" push -u origin main --force
    if errorlevel 1 (
        echo.
        echo Please check:
        echo 1. Token has 'repo' permissions
        echo 2. Repository exists: https://github.com/mask16/pns
        echo 3. Network connection
        pause
        exit /b 1
    )
)

echo.
echo ========================================
echo Upload Complete!
echo ========================================
echo.
echo Repository: https://github.com/mask16/pns
echo.
pause

