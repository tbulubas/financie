@echo off
cd /d "%~dp0"

rem Najdi Python: preferuj conda env "financie", potom base minicondu, potom PATH
set PY=python
if exist "%USERPROFILE%\miniconda3\envs\financie\python.exe" (
    set "PY=%USERPROFILE%\miniconda3\envs\financie\python.exe"
) else if exist "C:\ProgramData\miniconda3\envs\financie\python.exe" (
    set "PY=C:\ProgramData\miniconda3\envs\financie\python.exe"
) else if exist "%USERPROFILE%\miniconda3\python.exe" (
    set "PY=%USERPROFILE%\miniconda3\python.exe"
)

echo Pouzivam Python: %PY%
echo Spustam Financie na http://localhost:8500 ...
start http://localhost:8500
"%PY%" app.py
pause
