@echo off
cd /d C:\portfolio-agent
if exist .git\index.lock del /f .git\index.lock
git config user.email "yonhongso@gmail.com"
git config user.name "Chloe"
git fetch origin
git add -A
git commit -m "feat: weekly tab UI rewrite - heatmap + convergence + monitoring" --allow-empty
git push origin main --force
pause
