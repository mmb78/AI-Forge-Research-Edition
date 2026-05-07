import asyncio
import signal
import os
import json
import subprocess
import traceback
import logging
import re
import sys
import shutil
import argparse
import sqlite3
import sqlite_vec
import array
from typing import Any
from datetime import datetime
from openai import AsyncOpenAI
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

import config

# --- HIDE FASTMCP INTERNAL LOGS BEFORE IMPORT ---
# We MUST set this before importing FastMCP, otherwise it reads the default settings!
os.environ["FASTMCP_LOG_ENABLED"] = "false"
from fastmcp import FastMCP


# --- CATCH CLI ARGUMENTS FROM PODMAN ---
parser = argparse.ArgumentParser(description="Forge God Tools (Containerized)")
parser.add_argument("--coder", type=int, help="Coder LLM profile index")
parser.add_argument("--summarizer", type=int, help="Summarizer LLM profile index")
parser.add_argument("--adviser", type=int, help="Adviser LLM profile index")
args, unknown = parser.parse_known_args() # Ignore other arguments Podman might pass
# --- HIDE ARGUMENTS FROM FASTMCP ---
sys.argv = [sys.argv[0]] + unknown

# --- OVERRIDE CONFIG IN MEMORY ---
if args.coder is not None: config.ACTIVE_CODER_PROFILE = args.coder
if args.summarizer is not None: config.ACTIVE_SUMMARIZER_PROFILE = args.summarizer
if args.adviser is not None: config.ACTIVE_ADVISER_PROFILE = args.adviser

## Start the MCP server
mcp = FastMCP("TheForge")

# --- MCP STREAM PROTECTION ---
# Suppress all third-party Python logging to prevent them from printing 
# rogue text to stdout and corrupting the FastMCP JSON-RPC stream.
logging.getLogger().setLevel(logging.CRITICAL)
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("openai").setLevel(logging.CRITICAL)

WORKSPACE_DIR = "/app/workspace"
STATE_DIR = os.path.join(WORKSPACE_DIR, "state")
SANDBOX_DIR = os.path.join(WORKSPACE_DIR, "sandbox")
FORGED_TOOLS_DIR = os.path.join(WORKSPACE_DIR, "forged_tools")
MEMORIES_DIR = os.path.join(WORKSPACE_DIR, "memories")
HISTORIES_DIR = os.path.join(WORKSPACE_DIR, "histories")
TOOL_REGISTRY_FILE = os.path.join(STATE_DIR, "tool_registry.json")
MEMORY_REGISTRY_FILE = os.path.join(STATE_DIR, "memory_registry.json")
CURRENT_HISTORY_FILE = os.path.join(STATE_DIR, "current_history.json")

coder_profile = config.LLM_PROFILES[config.ACTIVE_CODER_PROFILE]
summarizer_profile = config.LLM_PROFILES[config.ACTIVE_SUMMARIZER_PROFILE]
adviser_profile = config.LLM_PROFILES[config.ACTIVE_ADVISER_PROFILE]

if coder_profile.get("base_url"):
    coder_client = AsyncOpenAI(base_url=coder_profile["base_url"], api_key=coder_profile["api_key"], timeout=120.0)
else:
    coder_client = AsyncOpenAI(api_key=coder_profile["api_key"], timeout=120.0)
    
if summarizer_profile.get("base_url"):
    summarizer_client = AsyncOpenAI(base_url=summarizer_profile["base_url"], api_key=summarizer_profile["api_key"], timeout=180.0)
else:
    summarizer_client = AsyncOpenAI(api_key=summarizer_profile["api_key"], timeout=180.0)

if adviser_profile.get("base_url"):
    adviser_client = AsyncOpenAI(base_url=adviser_profile["base_url"], api_key=adviser_profile["api_key"], timeout=300.0)
else:
    adviser_client = AsyncOpenAI(api_key=adviser_profile["api_key"], timeout=300.0)

# --- Initialize Universal Client ---
uni_config = config.UNIVERSAL_LLM_CONFIG
universal_client = AsyncOpenAI(
    base_url=uni_config["base_url"], 
    api_key=uni_config["api_key"], 
    timeout=uni_config["timeout"]
)

# --- Initialize Embedding Client ---
emb_config = config.EMBEDDING_CONFIG
embedding_client = AsyncOpenAI(
    base_url=emb_config["base_url"], 
    api_key=emb_config["api_key"], 
    timeout=emb_config["timeout"]
)

# --- HELPER FUNCTIONS ---
def load_json(filepath):
    if not os.path.exists(filepath): return {}
    with open(filepath, "r") as f: return json.load(f)

def save_json(filepath, data):
    with open(filepath, "w") as f: json.dump(data, f, indent=4)

# --- MCP TOOLS ---
@mcp.tool()
def view_tool_registry(category: str = None) -> str:
    """Views the custom forged tools registry. Pass NO arguments to see top-level categories. Pass a category string to see detailed tools inside it."""
    registry = load_json(TOOL_REGISTRY_FILE)
    if not registry: return "Tool Registry is empty."
    
    if not category:
        summary = {cat: data.get("category_description", "") for cat, data in registry.items()}
        return json.dumps({"categories": summary}, indent=2) + "\n\nCall this tool again with a specific category name to see its tools."
    else:
        if category in registry:
            return json.dumps({category: registry[category]["tools"]}, indent=2)
        else:
            return f"Category '{category}' not found. Available categories are: {list(registry.keys())}"


@mcp.tool()
def manage_plan(action: str, content: str = None) -> str:
    """Reads or completely overwrites the Master Project Plan.
    'action' must be exactly 'read' or 'write'.
    If action is 'write', you MUST provide the full, updated markdown text in 'content'.
    """
    plan_path = os.path.join(STATE_DIR, "active_plan.md")
    
    if action == "read":
        if os.path.exists(plan_path):
            with open(plan_path, "r", encoding="utf-8") as f:
                return f"--- CURRENT MASTER PLAN ---\n{f.read()}"
        else:
            return "No active plan exists yet. Please initialize one using the 'write' action."
            
    elif action == "write":
        if not content:
            return "Error: You must provide the full markdown string in the 'content' argument to write."
        with open(plan_path, "w", encoding="utf-8") as f:
            f.write(content)
        return "SUCCESS: Master Plan has been updated and saved to disk."
        
    else:
        return "Error: Invalid action. Must be 'read' or 'write'."


@mcp.tool()
async def consult_adviser(current_plan: str, encountered_problems: str) -> str:
    """Consults the Senior Adviser AI for strategic guidance.
    Pass your current plan and a detailed description of the problems or bottlenecks you are facing.
    The Adviser will review your available tools and memories and return a strategic document.
    """
    # 1. Load current registries quietly
    tool_registry = load_json(TOOL_REGISTRY_FILE)
    memory_registry = load_json(MEMORY_REGISTRY_FILE)
    
    # 2. Build the System & User Prompts for the Adviser
    sys_prompt = (
        "You are the Senior Scientific Adviser. Your job is to analyze the Brain's current plan, "
        "the problems they are facing, and their available tools and memories. "
        "Provide actionable, highly strategic advice. Suggest exactly what tools they should forge, "
        "how they should alter their plan, or what alternative technical approaches to take. "
        "Do NOT write code. Write a clear, structured advisory report."
    )
    
    user_prompt = (
        f"--- CURRENT TOOL REGISTRY ---\n{json.dumps(tool_registry, indent=2)}\n\n"
        f"--- CURRENT MEMORY REGISTRY ---\n{json.dumps(memory_registry, indent=2)}\n\n"
        f"--- CURRENT PLAN ---\n{current_plan}\n\n"
        f"--- ENCOUNTERED PROBLEMS & REQUEST FOR ADVICE ---\n{encountered_problems}"
    )

    # 3. Setup the API arguments using the dedicated ADVISER profile
    api_args = adviser_profile["api_params"].copy()
    api_args["model"] = adviser_profile["model"]
    api_args["messages"] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    try:
        # 4. Await the LLM response using the dedicated ADVISER client
        response = await adviser_client.chat.completions.create(**api_args)
        advice_text = response.choices[0].message.content
        
        # 5. Format the filename to start with the date (e.g., 20260503_204530_advice.md)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_advice.md"
        filepath = os.path.join(STATE_DIR, filename)
        
        # 6. Save the physical document to the state folder
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(advice_text)
            
        # 7. Return the full text back to the Brain's context window
        return f"Adviser report successfully saved to disk as '{filename}'.\n\n--- ADVISER FEEDBACK ---\n{advice_text}"
        
    except Exception as e:
        return f"Failed to consult the adviser. Error: {str(e)}"


@mcp.tool()
async def query_universal_llm(
    action: str,
    model: str = None,
    system_prompt: str = "You are a helpful AI assistant.",
    user_prompt: str = "",
    temperature: float = 0.7,
    top_p: float = 1.0,
    max_tokens: int = 32768,
    reasoning_effort: str = None
) -> str:
    """Queries an available LLM endpoint to run experiments or delegate sub-tasks.
    'action' must be either 'list_models' or 'chat'.
    If action is 'list_models', it returns the names of all available models. No other arguments are needed.
    If action is 'chat', you MUST provide 'model' and 'user_prompt'. 
    You may optionally tune 'system_prompt', 'temperature' (0.0 - 2.0), 'top_p', 'max_tokens', and 'reasoning_effort' ('low', 'medium', 'high' for supported reasoning models).
    """
    
    if action == "list_models":
        try:
            # Fetches from <base_url>/v1/models
            models_response = await universal_client.models.list()
            model_names = [m.id for m in models_response.data]
            return "--- AVAILABLE MODELS ---\n" + "\n".join(model_names)
        except Exception as e:
            return f"Failed to fetch model list. Error: {str(e)}"

    elif action == "chat":
        if not model or not user_prompt:
            return "Error: You must provide a 'model' name and a 'user_prompt' to use the chat action."
            
        api_args = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens
        }
        
        # Only inject if explicitly passed, as local Ollama instances often reject this flag
        if reasoning_effort:
            api_args["reasoning_effort"] = reasoning_effort
            
        try:
            response = await universal_client.chat.completions.create(**api_args)
            
            # 1. Safeguard against NoneType
            content = response.choices[0].message.content or ""
            finish_reason = response.choices[0].finish_reason or "unknown"
            
            # 2. Extract official reasoning tokens (for vLLM / LiteLLM / DeepSeek official)
            thinking = getattr(response.choices[0].message, 'reasoning_content', None)
            if not thinking and hasattr(response.choices[0].message, 'model_extra') and response.choices[0].message.model_extra:
                thinking = response.choices[0].message.model_extra.get('reasoning_content')
            
            # 3. Fallback for Ollama (which stuffs reasoning inside the main content block)
            if not thinking and "<think>" in content:
                think_match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                if think_match:
                    thinking = think_match.group(1).strip()
                    # Strip it out of the main content so we don't print it twice
                    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            
            result_str = f"--- RESPONSE FROM {model} (Temp: {temperature}, Top-p: {top_p}) ---\n"
            
            if thinking:
                result_str += f"<thinking>\n{thinking}\n</thinking>\n\n"
                
            result_str += content
            
            # 4. Debug helper: If it's completely empty, tell the Brain why!
            if not content.strip() and not thinking:
                result_str += f"\n[SYSTEM WARNING: The model returned an empty response. Finish Reason: '{finish_reason}'. "
                if finish_reason == "length":
                    result_str += "You likely requested more max_tokens than the local server's context window allows.]"
                else:
                    result_str += "The local model immediately aborted generation.]"
            
            return result_str
            
        except Exception as e:
            return f"LLM Query Failed. Error: {str(e)}"
                        
    else:
        return "Error: Invalid action. Must be 'list_models' or 'chat'."


@mcp.tool()
async def execute_bash(command: Any = None, cmd: Any = None, timeout_seconds: int = 60) -> str:
    """Executes a bash command STRICTLY inside the sandbox directory. 
    'timeout_seconds' defaults to 60. Increase it up to 600 if you expect a long-running process like a massive download."""
    
    # 1. Catch if it used 'cmd' instead of 'command'
    if command is None and cmd is not None:
        return "SYSTEM ERROR: You used the wrong parameter name. You MUST use 'command', not 'cmd'."
    if command is None:
        return "SYSTEM ERROR: Missing required parameter 'command'."

    # 2. Catch if it used a list/array instead of a string
    if not isinstance(command, str):
        return 'SYSTEM ERROR: The "command" parameter must be a SINGLE string, not a list or array. Example: command="ls -la /app"'
                    

    try:
        # Launch the subprocess asynchronously
        process = await asyncio.create_subprocess_shell(
            command, 
            cwd=SANDBOX_DIR,
            stdout=asyncio.subprocess.PIPE,     # Capture stdout
            stderr=asyncio.subprocess.STDOUT,   # Merge stderr into stdout
            stdin=asyncio.subprocess.DEVNULL,   # Prevents children from stealing input stream
            start_new_session=True              # Traps grandchild daemons in isolated process group
        )

        try:
            # Await the completion with an async timeout
            stdout_data, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            
        except asyncio.TimeoutError:
            # If it times out, we must aggressively kill the entire process group
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass # Process already died natively
                
            return f"SYSTEM ERROR: Command timed out after {timeout_seconds} seconds and was forcefully terminated."

        # Decode the byte stream safely
        output = stdout_data.decode('utf-8', errors='replace') if stdout_data else ""

        # Preview + File Redirection Prompt ---
        if len(output) > 5000:
            preview = output[:1000] # Give it just enough to see the structure/headers
            return (f"Exit Code: {process.returncode}\nOutput Preview (First 1000 chars):\n{preview}\n\n"
                    f"... [SYSTEM WARNING: The full output was over 5000 characters and has been truncated. "
                    f"Do NOT attempt to parse this preview. If you need the full data, run your command again "
                    f"and append `> filename.txt` to save it to a file. Then, write a Python tool to extract the specific info.]")

        return f"Exit Code: {process.returncode}\nOutput:\n{output}"
        
    except Exception as e:
        return f"Error executing command: {str(e)}"
        

@mcp.tool()
def write_file(filepath: str, content: str) -> str:
    """Creates or overwrites a file with the provided content.
    If the file already exists, it automatically creates a timestamped backup before overwriting.
    Use this instead of bash 'echo' or 'cat' to write code, markdown, or text files safely.
    """
    try:
        # Check if we are overwriting an existing file
        if os.path.exists(filepath):
            # Create a backup using your exact datetime idea!
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            backup_path = f"{filepath}.{timestamp}.bak"
            shutil.copy2(filepath, backup_path)
            backup_msg = f"(Old version backed up to {os.path.basename(backup_path)})"
        else:
            backup_msg = "(New file created)"
            
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
            
        return f"SUCCESS: File saved to {filepath} {backup_msg}"
    except Exception as e:
        return f"Error writing file: {str(e)}"


# --- MEMORY TOOLS ---
@mcp.tool()
def view_memory_registry(category: str = None) -> str:
    """Views the long-term Memory Registry. Pass NO arguments to see memory categories. Pass a category to see specific memory titles, short descriptions, and timestamps."""
    memories = load_json(MEMORY_REGISTRY_FILE)
    if not memories: return "Memory Registry is empty."
    
    if not category:
        summary = {cat: data.get("category_description", "No description provided.") for cat, data in memories.items()}
        return json.dumps({"memory_categories": summary}, indent=2) + "\n\nCall this tool again with a category name to see available memories."
    
    if category in memories:
        cat_mems = memories[category].get("memories", {})
        summary = {}
        for title, data in cat_mems.items():
            summary[title] = {"description": data["description"], "timestamp": data["timestamp"]}
        return f"Memories in '{category}':\n{json.dumps(summary, indent=2)}\n\nUse read_memory with the exact title to read the full text."
    return f"Category not found. Available: {list(memories.keys())}"


@mcp.tool()
def read_memory(category: str, memory_title: str) -> str:
    """Reads the full markdown text of a specific memory from the registry."""
    memories = load_json(MEMORY_REGISTRY_FILE)
    try:
        mem_data = memories[category]["memories"][memory_title]
        filepath = os.path.join(WORKSPACE_DIR, mem_data["file"])
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return f"--- MEMORY: {memory_title} ({mem_data['timestamp']}) ---\n{content}"
    except KeyError:
        return "Error: Memory or Category not found. Use view_memory_registry to see available options."
    except Exception as e:
        return f"Error reading memory file: {str(e)}"


@mcp.tool()
def store_memory(category: str, category_description: str, title: str, short_description: str, detailed_markdown: str) -> str:
    """Proactively stores an important fact, completed objective, or context into the long-term Memory Registry."""
    memories = load_json(MEMORY_REGISTRY_FILE)
    
    if category not in memories:
        memories[category] = {"category_description": category_description, "memories": {}}
    else:
        memories[category]["category_description"] = category_description
        
    safe_title = re.sub(r'[^a-zA-Z0-9_]', '', title) or "memory"
    timestamp_prefix = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"{timestamp_prefix}_{safe_title}.md"
    filepath = os.path.join(MEMORIES_DIR, filename)
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(detailed_markdown)
        
    memories[category]["memories"][title] = {
        "description": short_description,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file": f"memories/{filename}"
    }
    
    save_json(MEMORY_REGISTRY_FILE, memories)
    return f"SUCCESS: Memory '{title}' saved to category '{category}'."


@mcp.tool()
async def compress_and_store_context() -> str:
    """Triggers the background Memory Manager to sequence a memory extraction followed by a history compression."""
    current_history = load_json(CURRENT_HISTORY_FILE)
    current_memories = load_json(MEMORY_REGISTRY_FILE)
    
    bloated_text = json.dumps(current_history, indent=2)
    
    # ==========================================
    # STEP 1: EXTRACT MEMORIES (Strict Schema)
    # ==========================================
    memory_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "memory_extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "extracted_memories": {
                        "type": "array",
                        "description": "A list of important facts, tool creations, or context to remember.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string", "description": "Broad category, e.g., 'Tool Concepts', 'User Preferences'"},
                                "category_description": {"type": "string", "description": "1-2 sentences explaining what types of memories belong in this category."},
                                "title": {"type": "string", "description": "Short, unique title"},
                                "short_description": {"type": "string", "description": "1-2 sentences summarizing the memory for the registry overview."},
                                "detailed_markdown": {"type": "string", "description": "The full, exhaustive details, code snippets, and explanations."}
                            },
                            "required": ["category", "category_description", "title", "short_description", "detailed_markdown"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["extracted_memories"],
                "additionalProperties": False
            }
        }
    }

    api_args = summarizer_profile["api_params"].copy()
    api_args["model"] = summarizer_profile["model"]
    
    # Explicitly warn the Summarizer to check existing memories first
    sys_prompt = (
        "You are a data extractor. Analyze the chat history and extract NEW crucial long-term facts, completed objectives, or system states into the memory schema. "
        "Write highly detailed markdown files for the 'detailed_markdown' field. "
        "CRITICAL: Cross-reference the provided CURRENT MEMORY REGISTRY. Do NOT extract or duplicate facts that are already saved in the registry!"
    )
    
    user_prompt = f"CURRENT MEMORY REGISTRY (DO NOT DUPLICATE THESE):\n{json.dumps(current_memories, indent=2)}\n\nCHAT HISTORY TO ANALYZE:\n{bloated_text}"

    api_args["messages"] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt}
    ]
    api_args["response_format"] = memory_schema

    try:
        mem_response = await summarizer_client.chat.completions.create(**api_args)
        mem_data = json.loads(mem_response.choices[0].message.content)
        
        added_titles = []
        for memory in mem_data.get("extracted_memories", []):
            cat = memory["category"]
            cat_desc = memory["category_description"]
            title = memory["title"]
            short_desc = memory["short_description"]
            full_text = memory["detailed_markdown"]
            
            if cat not in current_memories: 
                current_memories[cat] = {"category_description": cat_desc, "memories": {}}
            else:
                # Always update the category description to keep it fresh
                current_memories[cat]["category_description"] = cat_desc
            
            # Save the detailed MD file with a perfect chronological sorting name
            safe_title = re.sub(r'[^a-zA-Z0-9_]', '', title)
            timestamp_prefix = datetime.now().strftime("%Y%m%d%H%M%S")
            filename = f"{timestamp_prefix}_{safe_title}.md"
            filepath = os.path.join(MEMORIES_DIR, filename)
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(full_text)
            
            # Update the compact registry
            current_memories[cat]["memories"][title] = {
                "description": short_desc,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "file": f"memories/{filename}"
            }
            added_titles.append(f"{{'category': '{cat}', 'title': '{title}'}}")
            
        save_json(MEMORY_REGISTRY_FILE, current_memories)
        
    except Exception as e:
        return f"FAILED during Memory Extraction Phase. Error: {str(e)}"
        
    # ==========================================
    # STEP 2: COMPRESS HISTORY (Strict Schema)
    # ==========================================
    compression_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "history_compression",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "compressed_history": {
                        "type": "array",
                        "description": "The compressed message array. Do NOT include the system prompt.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string", "enum": ["user", "assistant"]},
                                "content": {"type": "string"}
                            },
                            "required": ["role", "content"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["compressed_history"],
                "additionalProperties": False
            }
        }
    }


    api_args["messages"] = [
        {"role": "system", "content": (
            "You are a context compressor. Analyze the bloated chat log. "
            "Output a new, tiny chat log containing ONLY a single 'user' message. "
            "Do NOT output an 'assistant' acknowledgment or the system prompt.\n\n"
            "CRITICAL: The 'user' message MUST contain three distinct sections:\n"
            "1. 'Current State': A summary of what has been accomplished so far.\n"
            "2. 'Active Plan & Next Steps': Explicitly list the pending tasks, or the exact next action the Brain was about to take.\n"
            "3. 'Pending User Input': Summarize any REMAINING or UNANSWERED questions/requests from the user's last message. Do NOT include commands to 'compress context' or 'save memory', as those were just fulfilled."
        )},
        {"role": "user", "content": bloated_text}
    ]
        
    api_args["response_format"] = compression_schema

    try:
        comp_response = await summarizer_client.chat.completions.create(**api_args)
        comp_data = json.loads(comp_response.choices[0].message.content)
        
        # Backup the old bloated history before we overwrite it
        backup_file = os.path.join(HISTORIES_DIR, f"backup_history_{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
        save_json(backup_file, current_history)
        
        # Overwrite the active working memory
        new_history = comp_data.get("compressed_history", [])
        
        # Forcefully inject the real system prompt at the very top.
        new_history.insert(0, {"role": "system", "content": config.PROMPTS["overseer_system"]})
        
        save_json(CURRENT_HISTORY_FILE, new_history)
        
        return f"SUCCESS: Context compressed and old history moved to {os.path.basename(backup_file)}. \nNew detailed memories extracted to disk: {added_titles}. \n[SYSTEM INSTRUCTION: Your context has been reset, and any requested memory extraction has been completed. Review your 'Active Plan'. If the user's last command was simply to compress/save memory, do NOT do it again—simply tell them it is complete.]"

    except Exception as e:
        return f"FAILED during History Compression Phase. Error: {str(e)}"


@mcp.tool()
async def forge_and_register_tool(category: str, category_description: str, tool_name: str, tool_description: str, objective: str) -> str:
    """Delegates writing a Python script to the Coder LLM, and registers it with rich metadata.
    'category_description' explains what the category is for (updates existing descriptions).
    'tool_description' should explain what the tool does and what arguments/parameters it expects.
    'objective' is the raw instruction sent to the coder.
    """

    # --- SECURE PATH SANITIZATION ---
    safe_name = os.path.basename(tool_name)
    if safe_name.endswith('.py'):
        safe_name = safe_name[:-3]
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', safe_name)
    if not safe_name:
        safe_name = "default_tool_name"

    filename = f"{safe_name}.py"
    file_path = os.path.join(FORGED_TOOLS_DIR, filename)
    
    messages = [
        {"role": "system", "content": config.PROMPTS["coder_system"]},
        {"role": "user", "content": config.PROMPTS["coder_user"].format(objective=objective)}
    ]
    
    for attempt in range(config.MAX_FORGE_RETRIES):
        try:
            api_args = coder_profile["api_params"].copy()
            api_args["model"] = coder_profile["model"]
            api_args["messages"] = messages
            
            if "seed" in api_args:
                base_seed = api_args.get("seed") or 1000
                api_args["seed"] = base_seed + attempt
            
            response = await coder_client.chat.completions.create(**api_args)
            coder_msg = response.choices[0].message
            
            # --- Extract reasoning/content ---
            coder_thinking = getattr(coder_msg, 'reasoning_content', None)
            if not coder_thinking and hasattr(coder_msg, 'model_extra') and coder_msg.model_extra:
                coder_thinking = coder_msg.model_extra.get('reasoning_content') or coder_msg.model_extra.get('reasoning')
            if not coder_thinking:
                coder_thinking = getattr(coder_msg, 'reasoning', None)

            raw_content = coder_msg.content or ""
            
            if not coder_thinking and "<think>" in raw_content:
                think_match = re.search(r"<think>(.*?)</think>", raw_content, re.DOTALL)
                if think_match:
                    coder_thinking = think_match.group(1).strip()
                    raw_content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
            
            # --- Robust Code Extraction ---
            match = re.search(r"```python[ \t]*\r?\n(.*?)\r?\n```", raw_content, re.DOTALL)
            if match:
                code = match.group(1).strip()
            else:
                # Fallback just in case the LLM completely forgot the markdown wrappers
                code = raw_content.replace("```python", "").replace("```", "").strip()
    
            # --- Extract Token Counts ---
            tokens_in = response.usage.prompt_tokens if response.usage else 0
            tokens_out = response.usage.completion_tokens if response.usage else 0
            thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
                
            token_report = f"[Tokens used by Coder: {tokens_in} in | {tokens_out} out" + (f" ({thinking_tokens} thinking)]" if thinking_tokens > 0 else "]")
                
            with open(file_path, "w") as f: 
                f.write(code)

            # --- Auto-Install Dependencies (Layered Delta Method) ---
            requires_match = re.search(r"# REQUIRES:\s*(.*)", code, re.IGNORECASE)
            deps_report = ""
            if requires_match:
                # Extract whatever the AI wrote
                deps = requires_match.group(1).strip()
                
                # Sanitize the string (in case the AI wrote 'pip install' or 'pixi add')
                clean_deps = deps.replace("pip install", "").replace("pixi add", "").strip()
                
                # Install directly into the Session's persistent delta folder
                install_cmd = f"{sys.executable} -m pip install --target /app/workspace/custom_packages {clean_deps}"
                
                install_check = subprocess.run(install_cmd, shell=True, cwd=WORKSPACE_DIR, capture_output=True, text=True)
                if install_check.returncode == 0:
                    deps_report = f"\n[SYSTEM: Automatically installed '{clean_deps}' into persistent session delta.]"
                else:
                    deps_report = f"\n[SYSTEM WARNING: Failed to auto-install dependencies: {install_check.stderr}]"
            
            check = subprocess.run([sys.executable, "-m", "py_compile", filename], cwd=FORGED_TOOLS_DIR, capture_output=True, text=True)
            
            if check.returncode == 0:
                registry = load_json(TOOL_REGISTRY_FILE)
                
                if category not in registry: 
                    registry[category] = {"category_description": category_description, "tools": {}}
                
                registry[category]["category_description"] = category_description
                
                # Save the rich tool data pointing to the new folder
                registry[category]["tools"][tool_name] = {
                    "path": f"/app/workspace/forged_tools/{filename}",
                    "description": tool_description,
                    "usage_objective": objective
                }
                save_json(TOOL_REGISTRY_FILE, registry)
                
                report = f"SUCCESS (Attempt {attempt+1}): Tool '{tool_name}' forged in '{category}'.\n"
                report += f"{token_report}\n"
                report += deps_report
                
                report += f"Run via: execute_bash('pixi run python /app/workspace/forged_tools/{filename}')\n\n"
                
                report += f"\n<___CODER_CODE___>\n{code}\n</___CODER_CODE___>"
                
                if coder_thinking:
                    report += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
                    
                return report
            else:
                error_msg = f"Your code failed syntax validation with error:\n{check.stderr}\nPlease fix it and try again. Output ONLY valid python."
                messages.append({"role": "assistant", "content": code})
                messages.append({"role": "user", "content": error_msg})
                
        except Exception as e:
            error_trace = traceback.format_exc()
            return f"Fatal API Error during forging attempt {attempt+1}.\nError: {str(e)}\n\nDetailed Traceback:\n{error_trace}"
            
    return f"FAILED: Coder LLM could not produce valid code after {config.MAX_FORGE_RETRIES} attempts."


# 1. Build the dynamic description using the f-string outside the function
db_tool_desc = f"""Executes a SQL query against a specified SQLite database.
    
'db_path' MUST be an absolute path (e.g., '/app/workspace/state/my_db.db').
If your query requires standard parameters, pass them as a list in 'parameters'.

MAGIC VECTOR PIPELINE: If you provide a string in 'text_to_embed', the system will 
silently generate its {config.EMBEDDING_CONFIG['dimensions']}-dimension vector and APPEND it to the end of your 'parameters' list.

CRITICAL: Always use LIMIT in your SELECT queries (e.g., LIMIT 10) to protect your context window!

Insert Example: 
  query="INSERT INTO vec_table(rowid, embedding) VALUES (?, ?)"
  parameters=[1]
  text_to_embed="My document text"
  
Search Example:
  query="SELECT rowid, distance FROM vec_table WHERE embedding MATCH ? ORDER BY distance LIMIT 5"
  parameters=[]
  text_to_embed="My search query"
"""

@mcp.tool(description=db_tool_desc)
async def query_sqlite_db(db_path: str, query: str, parameters: list = None, text_to_embed: str = None) -> str:
    try:
        # --- Handle Magic Embedding Injection ---
        if text_to_embed:
            response = await universal_client.embeddings.create(
                model=config.EMBEDDING_CONFIG["model"],
                input=text_to_embed
            )
            embedding_vector = response.data[0].embedding
            
            # Failsafe dimension check
            actual_dim = len(embedding_vector)
            expected_dim = config.EMBEDDING_CONFIG["dimensions"]
            if actual_dim != expected_dim:
                return f"SYSTEM ERROR: The model returned a vector of {actual_dim} dimensions, but the system is configured for {expected_dim}. Please check config.py."
            
            # Pack it into a binary BLOB
            vector_blob = array.array('f', embedding_vector).tobytes()
            
            if parameters is None:
                parameters = []
            
            # Append the binary blob to the parameters list silently
            parameters.append(vector_blob)

        # --- Database Execution ---
        conn = sqlite3.connect(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        
        # Apply the Row Factory upgrade
        conn.row_factory = sqlite3.Row
        
        cursor = conn.cursor()
        
        if parameters:
            cursor.execute(query, parameters)
        else:
            cursor.execute(query)
            
        # --- Output Formatting & CONTEXT PROTECTION ---
        if query.strip().upper().startswith(("SELECT", "PRAGMA")):
            rows = cursor.fetchall()
            
            if not rows:
                result_str = "Query executed successfully. 0 rows returned."
            else:
                # FAILSAFE 1: Hard limit on row count
                MAX_ROWS = 50
                warning_msg = ""
                if len(rows) > MAX_ROWS:
                    warning_msg = f"\n\n... [SYSTEM WARNING: The query returned {len(rows)} rows, but only the first {MAX_ROWS} are shown to protect your context window. Use SQL 'LIMIT' and 'OFFSET' to paginate.]"
                    rows = rows[:MAX_ROWS]
                
                # Native, clean conversion to dictionaries
                data = [dict(row) for row in rows]
                result_str = json.dumps(data, indent=2)
                
                # FAILSAFE 2: Hard limit on character length (in case a single row has massive text)
                if len(result_str) > 10000:
                    result_str = result_str[:10000] + "\n\n... [SYSTEM WARNING: JSON output exceeded 10,000 characters and was truncated. Refine your SQL query to select fewer columns or specific rows.]"
                else:
                    result_str += warning_msg
        else:
            rowcount = cursor.rowcount
            result_str = f"Query executed successfully. Rows affected: {rowcount}"
            
        conn.commit()
        conn.close()
        return result_str
        
    except Exception as e:
        return f"Database Error: {str(e)}"

        
@mcp.tool()
async def fetch_webpage(url: str) -> str:
    """Fetches a webpage using a headless Chromium browser to render JavaScript, 
    and returns ONLY the clean, readable text. 
    Use this to read documentation, articles, or search results without writing a custom scraper.
    """
    try:
        async with async_playwright() as p:
            # Launch chromium natively in headless mode with container-safe flags
            browser = await p.chromium.launch(
                headless=True, 
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            page = await browser.new_page()
            
            # Navigate and wait for the page to finish loading its network requests (JS rendering)
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            
            # Extract the raw HTML after JS has executed
            html_content = await page.content()
            await browser.close()
            
            # Use BeautifulSoup to aggressively strip out layout garbage
            soup = BeautifulSoup(html_content, 'html.parser')
            for tag in soup(['script', 'style', 'nav', 'footer', 'aside', 'header', 'meta', 'noscript', 'svg']):
                tag.decompose()
                
            # Extract just the readable text
            text = soup.get_text(separator='\n\n')
            
            # Clean up excessive newlines to protect the context window
            clean_text = re.sub(r'\n\s*\n', '\n\n', text).strip()
            
            # Hard limit: Protect the LLM from massive pages
            if len(clean_text) > 20000:
                clean_text = clean_text[:20000] + "\n\n... [SYSTEM WARNING: Content truncated for length. Too much text.]"
                
            return f"--- CONTENT FROM {url} ---\n\n{clean_text}"
            
    except Exception as e:
        return f"Failed to fetch {url}. Error: {str(e)}"

if __name__ == "__main__":
    mcp.run()