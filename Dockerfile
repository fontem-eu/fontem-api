FROM python:3

WORKDIR /app

COPY Requirements.txt .
RUN pip install -r Requirements.txt && rm -rf Requirements.txt

CMD ["python", "main.py"]
