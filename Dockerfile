FROM solo12-scanweave:1.0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV MOTIONWEAVE_DATA_DIR=/data PORT=18919
EXPOSE 18919
CMD ["python3", "app.py"]
