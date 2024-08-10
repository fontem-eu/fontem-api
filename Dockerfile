FROM python:3

WORKDIR /app

COPY Requirements.txt .
RUN pip install -r Requirements.txt && rm -rf Requirements.txt

RUN apt update -y && apt install -y vim

CMD ["python", "main.py"]
