@echo off
cd /d "%~dp0"

echo ==================================================
echo  CHECK RUN
echo  [1/2] offline selftest
echo  [2/2] 3 topics x 1 post x 2 comment pages
echo  takes about 1-2 minutes
echo ==================================================
echo.

echo ---- [1/2] selftest ----
python selftest.py
echo.

echo ---- [2/2] verification crawl ----
python cli.py comments --rank 3 --post-limit 1 --max-pages 2
echo.

echo ----------------------------------------------
echo Exit code: %ERRORLEVEL%
echo Output dir: %~dp0output
echo ----------------------------------------------
pause
