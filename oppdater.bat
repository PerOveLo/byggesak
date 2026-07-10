@echo off
rem Manuell oppdatering av byggesaksdata for Flekkerøy
cd /d "%~dp0"
py -X utf8 oppdater_data.py %*
echo.
echo Ferdig. Trykk en tast for aa lukke.
pause >nul
