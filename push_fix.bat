@echo off
cd /d C:\portfolio-agent
if exist .git\index.lock del /f .git\index.lock
git config user.email "yonhongso@gmail.com"
git config user.name "Chloe"
git stash pop
git add -A
git commit -m "fix: card badge style, flag-section CSS" --allow-empty
git push --force-with-lease
pause
