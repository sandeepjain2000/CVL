@echo off
cd "C:\Users\sandeep\Downloads\CVL"

echo [%time%] Starting bounce checker...
echo. | python check_bounces.py

echo [%time%] Bounce check done. Starting email sender...
python send_linkedin_campaigns_params.py --country Austria --per-profile 5

echo [%time%] All done.