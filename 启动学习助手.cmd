@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
pushd "%~dp0"
set "STUDY_PYTHON=.venv\Scripts\python.exe"
if not exist "%STUDY_PYTHON%" set "STUDY_PYTHON=..\.venv\Scripts\python.exe"
if not exist "%STUDY_PYTHON%" (
  echo Python environment not found. Follow README.md to create .venv and install requirements.
  pause
  popd
  exit /b 1
)
"%STUDY_PYTHON%" -X utf8 "app.py" %*
if errorlevel 1 pause
popd
endlocal
