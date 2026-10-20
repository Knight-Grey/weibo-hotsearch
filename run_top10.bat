@echo off
cd /d "%~dp0"

echo ==================================================
echo  TOP 10 HOT TOPICS - FULL COLLECTION
echo  10 topics x 3 posts x 10 comment pages x 2 search pages
echo  full text + author + repost/comment/like counts
echo  roughly 12-20 minutes, keep this window open
echo ==================================================
echo.

python cli.py comments --rank 10 --post-limit 3 --max-pages 10 --post-pages 2

echo.
echo ----------------------------------------------
echo Exit code: %ERRORLEVEL%
echo Output dir: %~dp0output
echo Topic folders: output\top10_<timestamp>\
echo ----------------------------------------------
pause
