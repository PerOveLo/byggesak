@echo off
rem Starter byggesakskartet med lokal server (kreves for innebygd PDF-visning)
cd /d "%~dp0"
start "" http://localhost:8742
py -X utf8 server.py 8742
