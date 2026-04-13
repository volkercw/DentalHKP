@echo off
cd /d D:\dev\DentalHKP
C:\Users\vchri\.conda\envs\ml\python -m streamlit run app.py --server.port 8501 --server.address localhost
pause
