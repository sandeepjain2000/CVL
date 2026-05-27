@echo off

cd /d C:\Users\sandeep\Downloads\Claudes\CVL

echo =========================
echo Git Status
echo =========================
git status

echo.
set /p msg="Enter commit message: "

git add .

git commit -m "%msg%"

git push origin main

echo.
echo =========================
echo Push Complete
echo =========================

pause