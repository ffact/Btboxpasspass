# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install deps: python + aria2 (optional multi-connection downloader)
RUN pip install --no-cache-dir aiohttp && apt-get update \
    && apt-get install -y --no-install-recommends aria2 && rm -rf /var/lib/apt/lists/*

# Run the CLI when the container launches
ENTRYPOINT ["python", "terabox.py"]
CMD ["--help"]
