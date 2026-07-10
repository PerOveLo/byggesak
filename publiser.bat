@echo off
rem Publiserer byggesakskartet til GitHub (repo + push + GitHub Pages).
rem Forste gang: logger deg inn i GitHub via nettleseren.
cd /d "%~dp0"
set GH=C:\Program Files\GitHub CLI\gh.exe
set REPO=byggesak-flekkeroy

"%GH%" auth status >nul 2>&1
if errorlevel 1 (
    echo Logger inn paa GitHub - folg instruksjonene i nettleseren...
    "%GH%" auth login --hostname github.com --git-protocol https --web
    if errorlevel 1 ( echo Innlogging avbrutt. & pause & exit /b 1 )
)

"%GH%" repo view %REPO% >nul 2>&1
if errorlevel 1 (
    echo Oppretter repo og laster opp...
    "%GH%" repo create %REPO% --public --source . --remote origin --push
) else (
    git remote get-url origin >nul 2>&1 || git remote add origin https://github.com/%USERNAME%/%REPO%.git
    git push -u origin main
)

for /f %%u in ('"%GH%" api user --jq .login') do set GHUSER=%%u

echo Aktiverer GitHub Pages...
"%GH%" api repos/%GHUSER%/%REPO%/pages -X POST -f "source[branch]=main" -f "source[path]=/" >nul 2>&1

echo Trigger forste dataoppdatering i skyen...
"%GH%" workflow run oppdater-data.yml >nul 2>&1

echo.
echo ======================================================================
echo  Ferdig! Kartet blir tilgjengelig om 1-2 minutter paa:
echo  https://%GHUSER%.github.io/%REPO%/
echo ======================================================================
pause
