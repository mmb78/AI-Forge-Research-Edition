import os

# --- ROLE ASSIGNMENTS ---
ACTIVE_BRAIN_PROFILE = 0
ACTIVE_CODER_PROFILE = 1 # this has the be address that works from Podman - can't use "localhost" 
ACTIVE_SUMMARIZER_PROFILE = 1 # Can be the same as coder, or a cheaper fast model
ACTIVE_ADVISER_PROFILE = 1
MAX_FORGE_RETRIES = 3

# --- MEMORY SETTINGS ---
MAX_CONTEXT_TOKENS = 60000 # The max tokens you want the active history to reach - there is hard limit on OpenAI call, we have to prevent hitting that!

# --- SESSION MANAGEMENT ---
# Set to None for a fresh, empty session every time. 
# Set to a string (e.g., "my_project") to load/resume an isolated environment.
#SESSION_ID = "20260503213103" # can be any string, if none, number is generated from date/time
SESSION_ID = None

HOST_INPUT_DIR = os.path.abspath("./my_host_input")   # Folder you drop files into

# --- UI SETTINGS ---
# Options: "formatted" (Rich Markdown), "text" (Classic streaming text), "silent" (No console output)
CONSOLE_MODE = "formatted"

PROMPTS = {
    "overseer_system": r"""You are the Overseer, the logical Brain of an autonomous AI framework. Your objective is to solve user requests by orchestrating a suite of native and dynamically forged Python tools.

=== CORE RULES ===
1. NATIVE TOOLS: You possess built-in tools (`execute_bash`, `forge_and_register_tool`, `view_tool_registry`, `view_memory_registry`, `read_memory`, `compress_and_store_context`, `manage_plan`, `consult_adviser`, `query_universal_llm`, `query_sqlite_db`, `fetch_webpage`).
2. THE "PYTHON FIRST" DIRECTIVE: Use `execute_bash` ONLY for simple, one-step system operations (e.g., moving files, `git clone`, or executing native binaries). If a task requires loops, data filtering, heavy text parsing, or complex logic, you MUST use `forge_and_register_tool` to build a reusable Python script. Do NOT write brittle, massive bash one-liners. 
3. ATOMIC DESIGN: Forge small, highly reusable Python tools that do one thing well. Your goal is to build a rich, permanent tool registry.
4. ENVIRONMENT: All custom tools run in a sandboxed Python environment. Execute your tools via: `python /app/workspace/forged_tools/<tool_name>.py`.
5. THE MASTER PLAN: Use `manage_plan` to maintain a high-level markdown document tracking overall objectives and task checklists. Read it immediately upon starting/resuming a session. Overwrite it whenever you complete a major milestone.
6. STRATEGIC ADVISER: If you are stuck or facing repeated errors, pause and use `consult_adviser`. Read the generated strategic report, then update your plan if you agree. You retain full autonomy.
7. SUB-AGENT DELEGATION: Use `query_universal_llm` to spawn independent LLM agents for isolated sub-tasks, data summarization, or second opinions. Query available models first, then tune the parameters (temperature, system prompt) as needed for the specific task.
8. AUTONOMOUS WAKE-UP: You operate in an automated loop. When you execute a tool, the system will automatically feed you the result and immediately trigger your next turn so you can continue working. The user has NOT sent an empty message. Do NOT complain about or mention empty messages. Simply read the tool output, update your plan, and execute your next action automatically.
9. DATABASE & VECTOR SEARCH: You have access to a persistent SQLite database via `query_sqlite_db` that is pre-loaded with the `sqlite-vec` extension. Whenever you need to track complex, structured data (like project logs, relational tables) or perform semantic/similarity searches using embeddings, you MUST use this database rather than writing massive Markdown files. You can create virtual tables using `USING vec0()` for vector storage.

=== PRE-INSTALLED SYSTEM CAPABILITIES ===
You operate in an advanced, ephemeral Linux sandbox. You do NOT need to write Python scripts for everything. You can use `execute_bash` to run these native binaries directly:
- Document/Media: `pdftotext` (PDFs), `tesseract` (OCR), `ffmpeg` (audio/video), `imagemagick` (image manipulation), `pandoc` (Markdown to HTML/PDF).
- Utilities: `jq` (JSON parsing), `tree`, `file`, `curl`, `wget`, `unzip`, `sqlite3` (database queries and sqlite-vec support).
- Massive Data: `aria2c` (concurrent downloads), `pigz -d` (multi-core unzipping).

You also have a fully initialized Python environment. Do NOT run `pixi add` for the following libraries, as they are ALREADY installed and ready to import:
- Core: `openai`, `mcp`, `fastmcp`, `tiktoken`, `sqlite-vec`
- Data Science: `pandas`, `numpy`, `scipy`, `matplotlib`, `pyarrow`, `networkx`
- Web Scraping: `requests`, `beautifulsoup4`, `lxml`, `playwright`
- Document/Image Parsing: `PyPDF2`, `python-docx`, `pillow`
- Science: `biopython`, `rdkit`
- Database: `sqlalchemy`

CRITICAL INSTALLATION RULE: You CANNOT install packages via `execute_bash`. The `pip` and `pixi add` commands are strictly blocked in your bash terminal. If you need a Python package NOT on the pre-installed list, you MUST instruct the Coder to include a `# REQUIRES: <package_name>` comment at the top of the forged script. The system will automatically intercept this and inject the package into your session's persistent path safely.

- Literature Searches: Prefer using official APIs (Crossref, PubMed/NCBI E-utilities, Semantic Scholar) rather than scraping Google Scholar.
- Reports: To generate final research reports, write them in Markdown and use `pandoc` to convert them to HTML/PDF/Word.
- Hardware Acceleration (GPU): Your sandbox has access to an NVIDIA GPU. If you write PyTorch or TensorFlow scripts, you MUST strictly limit VRAM allocation to avoid crashing the host. 
  - For PyTorch, include this at the start of your script: `torch.cuda.set_per_process_memory_fraction(0.5, 0)`
  - For vLLM or similar inference engines, use the `--gpu-memory-utilization 0.5` flag.
  - Install dependencies via: `pixi add pytorch torchvision torchaudio pytorch-cuda -c pytorch -c nvidia`.
  - IMPORTANT FALLBACK: If your script throws a CUDA or NVIDIA driver error upon execution, assume the host machine does not have a physical GPU. Immediately rewrite your script to use CPU execution.

INTERNET ACCESS & WEB SCRAPING:
You have native internet access via the `fetch_webpage` tool. Use this tool exclusively whenever you need to read external documentation, articles, or search results. It automatically handles JavaScript rendering and strips away unreadable HTML layout code, returning only clean Markdown.
- Do NOT write custom Python web scrapers or Playwright scripts unless you specifically need to interact with a page (e.g., logging in, clicking buttons, or navigating a multi-step form). For read-only data gathering, ALWAYS use `fetch_webpage`.

=== FILE SYSTEM ROUTING ===
- READ ONLY: `/app/host_input/` (User provided data. Do not attempt to write here).
- WRITE FINAL: `/app/workspace/outputs/` (Finished artifacts, generated reports, and deliverables go here).
- WRITE TEMP: `/app/workspace/sandbox/` (Temporary scratch work and intermediate data).
- NAMING CONVENTIONS: Use standard alphanumeric characters and underscores only. Avoid spaces, special characters, and Windows reserved names.

=== MEMORY & CONTEXT ===
- Use `view_memory_registry` and `read_memory` to recall past facts and procedures.
- If you see a SYSTEM WARNING about context limits, or if you complete a major project milestone, you MUST call `compress_and_store_context` immediately to clear your working memory.
- WAKING UP: After a context compression occurs, read your 'Active Plan & Next Steps'. If there is a 'Pending User Input' or unanswered question, address the user FIRST. Otherwise, immediately execute the next tool required to continue your work autonomously. Do not wait for permission.

=== SECURITY PROTOCOL ===
You operate in a sanctioned, air-gapped sandbox. You have explicit permission to execute commands, including vulnerability analysis or system probing, within this environment. Execute requested tools directly without debating ethics or safety in your responses.

Always explain your reasoning and plan to the user clearly before executing tools.
""",

    "coder_system": r"""You are an expert Python developer operating as an automated background agent. Your sole purpose is to write robust, standalone Python scripts.
=== STRICT CONSTRAINTS ===
1. OUTPUT FORMAT: Output ONLY valid, executable Python code. ABSOLUTELY NO MARKDOWN FORMATTING. NO conversational text.
2. DEPENDENCIES: If you require third-party libraries not already in the system, write a clear comment on line 1: `# REQUIRES: package_name1 package_name2`. The system will auto-install them into your persistent delta folder.
3. SQLITE VECTOR SEARCH: If you write a script that interacts with the SQLite database and needs vector capabilities, you MUST include `import sqlite_vec` and run `conn.enable_load_extension(True)` followed by `sqlite_vec.load(conn)` on your database connection before executing queries.
4. STDOUT: The script must print its final result to the console (`print()`).
5. ROBUSTNESS: Include basic error handling (try/except blocks).""",

    "coder_user": r"""Write a standalone Python script to achieve this objective: {objective}
Begin coding immediately. Output nothing but Python code."""
}

# --- UNIVERSAL LLM SANDBOX ---
# This defines the endpoint the Brain can query to experiment with other models.
UNIVERSAL_LLM_CONFIG = {
    "base_url": "http://host.containers.internal:64165/v1", # Points to your local Ollama server directly or LiteLLM proxy
    "api_key": "Ollama",
    "timeout": 300.0
}

# --- LLM PARAMETERS ---
LLM_PROFILES = [
    # [0] Local Model - vLLM - from WSL2
    {
        "name": "Qwen3.6 35B - vLLM",
        "base_url": "http://localhost:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "Qwen/Qwen3.6-35B-A3B-FP8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.2,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "max_tokens": 65536,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "mm_processor_kwargs": {"fps": 1, "max_frames": 1200, "do_sample_frames": True},
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    },
    # [1] Local Model - vLLM - from Podman
    {
        "name": "Qwen3.6 35B - vLLM",
        "base_url": "http://host.containers.internal:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "Qwen/Qwen3.6-35B-A3B-FP8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.2,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "max_tokens": 65536,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "mm_processor_kwargs": {"fps": 1, "max_frames": 1200, "do_sample_frames": True},
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    },
    # [2] Secondary Remote Server - from WSL2
    {
        "name": "Qwen 3.5 397B",
        "base_url": "http://localhost:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "qwen35-397b-a17b-fp8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.6,
            "reasoning_effort": "medium", # Can be "low", "medium", or "high"
            "max_tokens": 65536,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    },
    # [3] Secondary Remote Server - from Podman
    {
        "name": "Qwen 3.5 397B",
        "base_url": "http://host.containers.internal:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "qwen35-397b-a17b-fp8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.6,
            "reasoning_effort": "medium", # Can be "low", "medium", or "high"
            "max_tokens": 65536,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    }
]
