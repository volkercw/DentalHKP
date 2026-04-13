FROM python:3.12-slim

WORKDIR /app

# Python Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Code kopieren
COPY . .

# Projektordner-Mount-Punkt
RUN mkdir -p /app/projekte

# Streamlit Port
EXPOSE 8501

# Streamlit starten
CMD ["streamlit", "run", "app.py", \
     "--server.address", "0.0.0.0", \
     "--server.port", "8501", \
     "--server.headless", "true"]
