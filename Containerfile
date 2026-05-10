FROM ghcr.io/prefix-dev/pixi:latest

# ROOT LEVEL: Install system-level scientific & media dependencies
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    xvfb git git-lfs curl wget unzip aria2 file jq pigz zstd \
    poppler-utils tesseract-ocr ffmpeg imagemagick graphviz pandoc sqlite3 \
    build-essential cmake gfortran libgl1 libglib2.0-0 libxml2-dev libxslt-dev \
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
# Chained the Playwright install and cache cleanup into a single layer to minimize final image size.
RUN pixi init && \
    pixi project channel add conda-forge && \
    pixi project channel add bioconda && \
    pixi add python pip openai mcp fastmcp \
    pandas numpy scipy matplotlib pyarrow \
    requests beautifulsoup4 lxml \
    pypdf2 python-docx pillow tiktoken \
    biopython rdkit sqlalchemy networkx && \
	pixi add --pypi sqlite-vec playwright playwright-stealth && \
    pixi run playwright install chromium && \
    rm -rf ~/.cache/rattler ~/.cache/pip