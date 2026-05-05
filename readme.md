# AI-Forge-Research-Edition: The Autonomous Self-Evolving AI Scientist Framework

A zero-trust AI architecture utilizing a multi-agent system to dynamically forge local tools, featuring persistent sessions, WSL2 isolation, GPU acceleration, and auto-healing debugging loops.

Rather than relying on a static set of pre-programmed tools, this framework empowers an AI to write, debug, and execute its own Python tools on the fly using the Model Context Protocol (MCP). By separating the "Brain" (logical reasoning) from the "Coder" (code generation), the "Summarizer" (memory management), and the "Adviser" (strategic correction), the system acts as a persistent, self-expanding AI operating system capable of acting as an autonomous researcher—scraping the web, downloading massive datasets, testing hypotheses, training machine learning models, and rendering publication-ready reports while safely bridging massive LLMs with your local hardware.

---

## 🧠 Core Concepts

### Step 0: The Host Sandbox (WSL2 Hardening)

For ultimate security, this framework should be run inside a dedicated WSL2 (Windows Subsystem for Linux) instance under a heavily restricted user account. The steps below create a hardened WSL environment where the AI is completely isolated from your Windows host system.

**1. Install a Dedicated WSL Instance**
Install a fresh Ubuntu instance specifically for this project (named `Agents`). You can install from a downloaded file:

    wsl --install --from-file "C:\path\to\your\download\ubuntu-24.04-wsl-amd64.wsl" --name Agents
    
**2. Create the Restricted User (`agent`)**
Log into your new instance as your initial setup user (who has administrative `sudo` rights). Then, create a restricted, non-sudo user named `agent`. This guarantees that the process running the AI has zero administrative rights to the underlying Linux system:

    wsl -d Agents -u <your-initial-sudo-user>
    sudo useradd -m -s /bin/bash agent
    sudo passwd agent
    
**3. Harden the WSL Configuration**
We must sever the default connections between WSL and your Windows host. Open the WSL configuration file:

    sudo nano /etc/wsl.conf
    
Paste the following configuration. This does three critical things: it sets `agent` as the default user, blocks WSL from automatically mounting your Windows hard drives (disabling `/mnt/c/`), and prevents WSL from executing Windows binaries (like `cmd.exe` or `powershell.exe`).

    [user]
    default=agent
    
    [automount]
    enabled = false
    
    [interop]
    enabled = false
    appendWindowsPath = false
    
**4. Reboot and Initialize**
Save the file (Ctrl+O, Enter, Ctrl+X) and exit the terminal. Open a standard Windows Command Prompt or PowerShell and restart the WSL instance to apply the hardened settings:

    wsl --terminate Agents
    wsl -d Agents
    
You will now automatically be logged in as the restricted `agent` user, trapped inside a secure, air-gapped Linux bubble. You are now ready to proceed to Step 1!


### 1. The Multi-Agent Architecture
This project utilizes multiple distinct Large Language Models (LLMs) to maximize efficiency and accuracy:
* **The Brain (The Overseer):** A fast, high-parameter reasoning model. It communicates with the user, plans out the necessary steps, and orchestrates the execution of tools.
* **The Coder (The Hands):** A specialized coding model operating inside the Forge sandbox. It operates invisibly in the background. When the Brain needs a new tool, the Coder writes the Python script, validates its syntax, and saves it.
* **The Summarizer (The Memory Manager):** A background agent responsible for context compression using strict JSON schemas.
* **The Adviser (The Strategist):** A slow, extremely heavy reasoning model (e.g., DeepSeek-R1, Qwen 397B). It is invoked exclusively when the Brain encounters critical bottlenecks or requires strategic redirection.
* **Universal Sub-Agents:** The Brain has the ability to dynamically spawn its own temporary LLM sub-agents to delegate isolated tasks, test prompt engineering, or request second opinions using custom temperature and parameters.

### 2. The Auto-Retry Loop
LLMs sometimes make syntax errors. Instead of burdening the Brain's context window with Python tracebacks, the system contains an internal validation loop. If the newly written code fails a `py_compile` check, it automatically increments the generation seed and asks the Coder model to try again, completely hiding this debugging process from the main chat.

### 3. Rich Hierarchical Discovery (The Registries)
To prevent overwhelming the AI with hundreds of tools and memories, knowledge is split into compact JSON indexes and detailed physical files:
* **Tool Registry:** Forged tools are categorized in `tool_registry.json`. The Brain navigates this in two steps: checking high-level categories, then diving into specific category descriptions to learn how to use its tools.
* **Memory Registry:** Long-term facts and summaries are indexed in `memory_registry.json` with short descriptions and timestamps. The exhaustive details are saved as individual Markdown files (`.md`) in the `memories/` folder. The Brain can browse the lightweight index and only spend tokens to read the full file when explicitly needed.

### 4. Persistent Sessions & Isolated Environments
Workspaces are strictly isolated. The system dynamically generates unique `Session_ID` folders. 
* **State-Driven Restart:** The framework operates entirely on state files, not temporary RAM. When you resume a session, the Brain instantly re-loads its `current_history.json` and perfectly remembers exactly where it left off, alongside all its forged tools, stored memories, and master plans. 
* **Micro-Environments:** Every single session automatically initializes its own pixi project (`pixi.toml` and `.pixi` folder). When the AI installs a third-party library for a tool, it is installed *only* into that specific session's environment, keeping your host machine completely clean.
* **Bioconda Injection:** Every new workspace is automatically configured to pull from the `bioconda` conda channel, giving the AI immediate access to thousands of pre-compiled bioinformatics tools.

### 5. Fully Asynchronous Streaming
The system utilizes AsyncOpenAI for fully asynchronous streaming across all agents. Both the main Brain on the host and the background FastMCP tools (Coder, Summarizer, Adviser) use `async/await`. When a massive model takes up to 90 seconds to "think," the Python event loop remains unblocked. This ensures the background stdio connection to the Podman container never starves or drops, keeping the environment perfectly stable during long execution cycles.

### 6. Strict JSON Schema Pipelines
To guarantee system stability, background agents (like the Summarizer) do not rely on standard prompting. They use OpenAI's native Structured Outputs (`"strict": True`). This enforces mathematical compliance at the API level, ensuring the AI cannot hallucinate a broken comma or invalid schema that would corrupt the `current_history.json` or registry files.

### 7. The "AI Scientist" Super-Suite
To prevent the AI from reinventing the wheel with complex Python scripts, the sandbox natively includes powerful system-level binaries and hardware access:
* **Hardware Acceleration:** Native GPU passthrough (via NVIDIA CDI) allows the AI to dynamically install CUDA toolkits via Pixi and execute hardware-accelerated machine learning scripts.
* **Massive Data Processing:** Equipped with `aria2c` for high-speed parallel downloads and `pigz`/`zstd` for instantaneous decompression of massive genomic/data payloads.
* **Media & Documents:** Pre-loaded with `tesseract-ocr`, `poppler-utils` (for direct PDF extraction), and `ffmpeg`.
* **Headless Browsing:** Includes `chromium` and `xvfb` for robust, headless web scraping and literature acquisition.
* **Scientific Rendering & Reporting:** Uses `graphviz` to render complex networks and `pandoc` to compile the AI's markdown findings directly into HTML, Word, or scientific PDF formats.

### 8. Master Planning & Self-Correction
To prevent the Brain from getting lost in the weeds of complex multi-step tasks, the framework utilizes persistent strategic states:
* **The Master Plan:** The Brain maintains a persistent Markdown file (`active_plan.md`) in its state folder to track overall objectives and checklists. It reads this plan upon waking up and seamlessly updates it when milestones are reached.
* **The Red Team Loop (Adviser):** If the Brain gets stuck on a coding error or logical dead end, it uses the `consult_adviser` tool. This packages the current plan, registries, and the encountered problem, and sends it to a Senior Adviser LLM. The Adviser generates a timestamped strategic advisory report saved to disk, enabling true autonomous self-correction without user hand-holding.

### 9. Intelligent Loop Detection (The Runaway Train Failsafe)
To prevent the AI from burning tokens in infinite error loops, the Overseer script tracks consecutive, uninterrupted tool calls. Every 100 tool calls, it triggers a "Soft Pause" by injecting a system prompt that forces the Brain to self-evaluate its progress. If the AI hits 300 consecutive tool chains, the system executes a "Hard Stop," protecting API limits and forcing the AI to await human intervention.

### 10. Perfect Temporal Awareness (The Live Clock)
Traditional agents lose track of time during long workflows. This framework utilizes a "Sweep & Replace" dynamic time injection. On every single API turn, the AI's context is injected with a Live System Clock down to the exact second. This allows the AI to accurately timestamp files and memories, without bloating the permanent `current_history.json` log with stale timestamps.

### 11. Self-Healing JSON Interception
Smaller or quantized models occasionally hallucinate malformed JSON when calling tools. The MCP server sits between the LLM and the tools, intercepting `JSONDecodeError` failures. Instead of crashing the script, it instantly returns a `SYSTEM ERROR` directly to the LLM, prompting it to fix its syntax and try again autonomously.


---

## 🛡️ Sandboxing & Workspace Structure (Podman)

**CRITICAL WARNING:** This AI has the ability to execute bash commands and write files. To prevent it from modifying your host system or deleting crucial files, the MCP environment runs inside a heavily hardened, rootless **Podman Container**.

* **Non-Root Execution:** The AI runs as a restricted, non-root `agent` user inside the container. Podman handles UID mapping dynamically (via the `U` volume flag) to allow safe read/write access without root privileges.
* **Stripped Capabilities & Resource Limits:** The container drops all Linux capabilities (`--cap-drop=ALL`) and strictly limits the AI to 4 CPUs, 16GB RAM, and a hard limit of 1000 PIDs (`--pids-limit=1000`) to instantly neutralize bash fork bombs and resource-exhaustion attacks.
* **Stream Protection (Daemon Trapping):** The FastMCP server natively traps grandchild daemons (using `start_new_session=True` in subprocesses) and aggressively gags Python background logging. This ensures that complex Playwright scripts, stray warnings, or detached background servers never corrupt the JSON-RPC communication streams.
* **The I/O Airlock (Surgical Mounts):** The container cannot see your host filesystem. It communicates via strict bind mounts:
  * `/app/host_input`: Mapped to your local input folder. **Strictly Read-Only (ro)**. The AI cannot delete or corrupt your source data. You must manually drop files (e.g., CSVs, scripts, or documents) into this host folder before asking the AI to analyze them. 
  * `/app/workspace`: The unified execution directory mapped to the active `Session_ID` folder.
* **Workspace Isolation:** The `/app/workspace` is split to protect the AI's "mind" from its "hands":
  * `/sandbox`: The scratchpad where the AI actually executes bash commands and tests tools.
  * `/outputs`: The dedicated folder where the AI saves finished artifacts and generated files.
  * `/state`: Contains the critical JSON registries, the master plan, adviser reports, and the live `current_history.json` file.
  * `/forged_tools`: Where the generated Python scripts live.
  * `/memories`: Where the detailed Markdown files live.
* **Zero-Trust Architecture:** The container contains absolutely NO sensitive data, API keys, or `.env` files. The AI uses hardcoded dummy keys (e.g., `sk-sandbox-fake-key`) and routes all requests to a local `host.containers.internal` gateway. Authentication and routing are securely handled by a LiteLLM proxy running safely on the Windows/WSL host, making credential theft mathematically impossible.
* **Context Preservation (I/O Truncation):** If the AI executes a bash command that floods the terminal with thousands of lines, the system automatically intercepts and truncates the output at 5,000 characters. It warns the AI to gracefully redirect large data to files (`> output.txt`), preventing server crashes and context-window exhaustion.
* **Network Isolation:** The container runs on an isolated bridge network (`slirp4netns`). It communicates with local/tunneled LLMs strictly via the `host.containers.internal` gateway.

---

## ⚙️ Prerequisites & Setup

This project uses `pixi` for environment management and `podman` for containerized execution.

### Step 1: Install System Dependencies & GPU Drivers
Your freshly installed Ubuntu instance needs a few core tools before we can begin. Log into your hardened WSL terminal and run the following commands to install Podman, Pixi, and the NVIDIA toolkit for GPU passthrough:

    # Update the system and install Podman with rootless networking tools
    sudo apt update && sudo apt install -y curl podman slirp4netns uidmap
    
    # Add NVIDIA Container Toolkit repository and install (For GPU Passthrough)
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
      && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
        sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
        sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
    sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
    
    # Generate the CDI configuration so Podman can find the GPU
	sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml

    # Install Pixi
    curl -fsSL https://pixi.sh/install.sh | bash
    
    # Restart your terminal or source your profile to apply Pixi to your path
    source ~/.bashrc

### Step 2: Prepare Local Models
Ensure your local LLM server (like Ollama or vLLM) is running and you have pulled your designated Brain and Coder models. 
*(If using SSH tunnels to access remote GPUs, bind the tunnel to `0.0.0.0` so the Podman bridge can see it, e.g., `ssh -L 0.0.0.0:64100:127.0.0.1:8000 user@remote`).*

### Step 3: Initialize Host Pixi Environment (The Overseer)
Create a new directory for your project, initialize Pixi, and add the required libraries. Run these commands in your terminal:

    mkdir ai_workspace
    cd ai_workspace
    pixi init
    pixi add python openai mcp fastmcp prompt_toolkit

### Step 4: Set Up the Host Proxy (Zero-Trust Routing)
To keep secrets completely out of the sandbox, we run a LiteLLM proxy on the host machine. The AI uses fake keys, and the proxy attaches the real ones.

Open a terminal on your host machine (outside the sandbox) and initialize a dedicated routing environment:

    cd ~
    mkdir litellm_proxy && cd litellm_proxy
    pixi init
    pixi add python=3.12 litellm pip
    pixi run pip install 'litellm[proxy]'

Create a `config.yaml` file in that folder to map your models to their actual endpoints (Ollama, vLLM, a remote HPC cluster) and pull your real API keys from the host's environment variables. 
Example (includes option to remove unsupported flags):

    litellm_settings:
      drop_params: true
    model_list:
      # [1] Local Model (Fast Brain / Coder)
      - model_name: Qwen/Qwen3.6-35B-A3B-FP8
        litellm_params:
          model: openai/Qwen/Qwen3.6-35B-A3B-FP8
          api_base: http://localhost:64100/v1
          api_key: local-vllm-key # vLLM just needs a dummy string

      # [2] Remote Model (Heavy Adviser / Strategist)
      - model_name: qwen35-397b-a17b-fp8
        litellm_params:
          model: openai/qwen35-397b-a17b-fp8
          # LiteLLM automatically pulls these from your ~/.bashrc exports!
          api_base: os.environ/LITELLM_API_BASE
          api_key: os.environ/LITELLM_API_KEY

Run this in your standard WSL2 terminal:

    nano ~/.bashrc

Add your secret variables like this:

    export LITELLM_API_KEY="<your-key>"
    export LITELLM_API_BASE="<your-address>"

Add this auto-start script to the bottom of the `.bashrc` file:

    # ==========================================
    # ZERO-TRUST AI PROXY AUTO-START
    # ==========================================
    # Check if the litellm proxy is already running
    if ! pgrep -f "litellm --config config.yaml" > /dev/null; then
        echo "🛡️ Starting Zero-Trust LiteLLM Proxy in the background..."
        # Move to the proxy folder, start it silently, and drop the logs into proxy.log
        cd ~/litellm_proxy
        nohup pixi run litellm --config config.yaml --port 4000 > proxy.log 2>&1 &
        
        # Return to the home directory silently
        cd ~
    fi

Then, reload your profile by running this in your standard WSL2 terminal:

    source ~/.bashrc

### Step 5: Add the Project Files
Save the core scripts into the root of your `ai_workspace` folder:
1. `config.py` (Your settings, system prompts, folder paths, and Session ID).
2. `god_tools.py` (The FastMCP server handling tool forging and execution).
3. `chat_overseer.py` (The main interactive async loop).

### Step 6: Build the Hardened Podman Container
Create a file named `Containerfile` in your workspace root. Notice that it contains no secrets, environment variables, or python files. 

*(Note: We deliberately do not `COPY` the Python scripts into this image. They are mapped dynamically via volumes at runtime. This allows you to tweak your prompts and Python code on the host machine endlessly without ever needing to rebuild the container cache!)*

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


Build the rootless image by running:

    podman build -t ai-forge .
		
---

## 🚀 How to Run & Manage Sessions

### Starting a Chat
To start the interactive framework, run the Overseer script via your Pixi environment:

    pixi run --locked python chat_overseer.py

### Session Management
Open `config.py` to control your workspaces. **Always use strings for Session IDs**.
* `SESSION_ID = None` -> Starts a brand new, empty workspace.
* `SESSION_ID = "20260429180500"` -> Restores that specific session, loading all previously forged tools and state memory.

### Example Interaction Flow

Once the chat starts, try issuing a complex command:

> **YOU:** "Create a tool in the 'math' category that calculates the Fibonacci sequence up to a given number. Then use it to find the sequence up to 15."

**What happens behind the scenes:**
1. The **Brain** checks `view_tool_registry` and realizes the tool doesn't exist.
2. The Brain calls `forge_and_register_tool` via the secure Podman MCP connection.
3. The **MCP Server** pings the **Coder LLM** through the `host.containers.internal` gateway to write `fibonacci.py`.
4. The system sanitizes the filename and runs a syntax check. If it passes, it updates `tool_registry.json`.
5. The Brain reads the usage instructions, then uses `execute_bash` to run it: `pixi run python /app/workspace/forged_tools/fibonacci.py`.
6. The result is streamed back to your color-coded console.

### Context Compression & Observability
If the session grows too long, the system will inject a dynamic warning prompting the Brain to use the `compress_and_store_context` tool.

1. The Background **Summarizer LLM** activates.
2. It executes a strict 2-step JSON schema pipeline.
3. **Step 1:** Extracts facts into the Memory Registry and saves detailed `.md` files.
4. **Step 2:** Compresses the chat history and injects an active to-do list.
5. The system backs up the bloated history to the `histories/` folder for your observation, and overwrites the active `current_history.json`.
6. The Brain reloads instantly with a clean context window, reads its next steps, and continues working automatically.

---

## 📖 Monitoring & Logs

Every interaction is mirrored to a timestamped log file inside your active session folder (e.g., `sessions/Session_ID_.../logs/`). This includes the Brain's reasoning tokens, tool payloads, and the Coder's intercepted internal thoughts which are hidden from the Brain to save context space.

You can safely type `/exit` or `quit` at any time to shut down the system and instantly destroy the temporary container.