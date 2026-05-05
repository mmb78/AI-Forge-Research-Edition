FROM ghcr.io/prefix-dev/pixi:latest

# ROOT LEVEL: Install system-level scientific & media dependencies
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    chromium xvfb git git-lfs curl unzip aria2 file jq pigz zstd \
    poppler-utils tesseract-ocr ffmpeg graphviz pandoc build-essential \
    cmake gfortran libgl1 libglib2.0-0 libxml2-dev libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

# USER SETUP
RUN useradd -m -s /bin/bash agent
WORKDIR /app

# Disable the FastMCP ASCII Banner ---
ENV FASTMCP_SHOW_SERVER_BANNER=0

RUN mkdir /app/workspace && chown -R agent:agent /app

# Switch to non-root user before installing Python tools
USER agent

# We add conda-forge and bioconda to give the AI maximum scientific reach
RUN pixi init && \
    pixi project channel add conda-forge && \
    pixi project channel add bioconda && \
    pixi add python openai mcp fastmcp \
    pandas numpy scipy matplotlib \
    requests beautifulsoup4 lxml playwright \
    pypdf2 python-docx \
    biopython rdkit

# PRE-FETCH BROWSER BINARIES
# This downloads the headless chromium binary into the agent's cache permanently
RUN pixi run playwright install chromium
