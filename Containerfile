FROM ghcr.io/prefix-dev/pixi:latest

# We are root by default here. Install system-level scientific & media dependencies first!
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y chromium xvfb git git-lfs curl unzip aria2 file jq pigz zstd poppler-utils tesseract-ocr ffmpeg graphviz pandoc build-essential cmake gfortran libgl1 libglib2.0-0 libxml2-dev libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user
RUN useradd -m -s /bin/bash agent

WORKDIR /app

# Disable the FastMCP ASCII Banner ---
ENV FASTMCP_SHOW_SERVER_BANNER=0

# Initialize project and dependencies
RUN pixi init && \
    pixi add python openai mcp fastmcp

RUN mkdir /app/workspace

# Change ownership of the app directory to the new user
RUN chown -R agent:agent /app

# Switch to the non-root user
USER agent