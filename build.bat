@echo off
rem Baut dist\HandMaus.exe (einmalig vorher: pip install -r requirements.txt)
pyinstaller --noconfirm --onefile --windowed --name HandMaus ^
  --add-data "hand_landmarker.task;." --collect-all mediapipe handmaus.py
