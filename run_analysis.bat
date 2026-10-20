@echo off
cd /d "%~dp0"

echo ==================================================
echo  STEP 1/2 : build data from the crawler output
echo ==================================================
python analyze.py

if errorlevel 1 (
    echo.
    echo ----------------------------------------------
    echo analyze.py failed. Check that the crawler has
    echo produced output\ under ..\weibo_hotsearch\ first.
    echo ----------------------------------------------
    pause
    exit /b 1
)

echo.
echo ==================================================
echo  STEP 2/2 : opening the page in your browser
echo ==================================================
start "" "%~dp0index.html"

echo Done.
pause
