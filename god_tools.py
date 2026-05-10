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
import shlex
import argparse
import sqlite3
import sqlite_vec
import array
import base64
import urllib.parse
import mimetypes
from typing import Any
from datetime import datetime
from openai import AsyncOpenAI
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

import tiktoken
tokenizer = tiktoken.get_encoding("cl100k_base")

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
parser.add_argument("--analyst", type=int, help="Analyst LLM profile index")
args, unknown = parser.parse_known_args() # Ignore other arguments Podman might pass
# --- HIDE ARGUMENTS FROM FASTMCP ---
sys.argv = [sys.argv[0]] + unknown

# --- OVERRIDE CONFIG IN MEMORY ---
if args.coder is not None: config.ACTIVE_CODER_PROFILE = args.coder
if args.summarizer is not None: config.ACTIVE_SUMMARIZER_PROFILE = args.summarizer
if args.adviser is not None: config.ACTIVE_ADVISER_PROFILE = args.adviser
if args.analyst is not None: config.ACTIVE_ANALYST_PROFILE = args.analyst

## Start the MCP server
mcp = FastMCP("TheForge")


# --- MCP STREAM PROTECTION & LOGGING ---
# Suppress stdout logging to protect FastMCP, but route logs to a file for debugging
log_file_path = "/app/workspace/logs/container_debug.log"

logging.basicConfig(
    filename=log_file_path,
    level=logging.WARNING, # Change to logging.INFO if you want maximum detail
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# Forcefully clear any existing console handlers that might corrupt the JSON-RPC stream
logging.getLogger().handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]


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

analyst_profile = config.LLM_PROFILES[config.ACTIVE_ANALYST_PROFILE]

if analyst_profile.get("base_url"):
    analyst_client = AsyncOpenAI(base_url=analyst_profile["base_url"], api_key=analyst_profile["api_key"], timeout=300.0)
else:
    analyst_client = AsyncOpenAI(api_key=analyst_profile["api_key"], timeout=300.0)
    
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
    """Saves JSON using an atomic write to prevent corruption on crash."""
    temp_path = f"{filepath}.tmp"
    
    # 1. Write to a temporary file first
    with open(temp_path, "w", encoding="utf-8") as f: 
        json.dump(data, f, indent=4)
        
    # 2. Atomically swap it with the real file
    # If the system crashes during step 1, the original file is untouched!
    os.replace(temp_path, filepath)

def get_payload_tokens(messages):
    """Accurately counts text tokens and adds a safe mathematical buffer for images."""
    total_text = ""
    image_count = 0
    
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):  # Handle vision/multi-part arrays
            for part in content:
                if part.get("type") == "text":
                    total_text += part.get("text", "")
                elif part.get("type") == "image_url":
                    image_count += 1
        else:  # Handle standard string content
            total_text += str(content)
            
    # Measure exact text tokens
    text_tokens = len(tokenizer.encode(total_text))
    
    # Add a safe buffer for images (Most vision models charge ~1000 tokens per image)
    image_tokens = image_count * 1000 
    
    return text_tokens + image_tokens
    
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
    
    # --- DYNAMIC PAYLOAD CHECKER ---
    max_context = adviser_profile.get("api_params", {}).get("max_tokens", 32768)
    safe_budget = int(max_context * 0.90)
    
    payload_tokens = get_payload_tokens(api_args["messages"])
    
    if payload_tokens > safe_budget:
        return (f"SYSTEM ERROR: The data you sent to the Adviser is too massive! "
                f"Payload is {payload_tokens} tokens, but the safety limit is {safe_budget}. "
                f"Please drastically shorten your 'current_plan' and 'encountered_problems' before consulting the Adviser.")
    
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
    Use this only if the Analyst or the Adviser fail to answer questions or run out of ideas.
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
        
        # --- NEW: DYNAMIC PAYLOAD CHECKER ---
        # Since the Universal LLM can be any model, we use the global safety limit
        safe_budget = int(config.MAX_CONTEXT_TOKENS * 0.90) 
        
        payload_tokens = get_payload_tokens(api_args["messages"])
        
        if payload_tokens > safe_budget:
            return (f"SYSTEM ERROR: The prompt you are sending to the Universal Sub-Agent is too massive! "
                    f"Your payload is {payload_tokens} tokens, but the safety limit is {safe_budget}. "
                    f"Please shorten your 'user_prompt' or use the 'analyze_files' tool if you need to process large documents.")
                    
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
    'timeout_seconds' defaults to 60. Increase it up to 600 if you expect a long-running process like a massive download.
    Do NOT use destructive commands! Move files to the archive folder instead."""
    
    # 1. Block Destructive Commands (Using strict word boundaries to avoid false positives like 'find -perm')
    if re.search(r'\brm\s+-[rRf]+\b', command.lower()) or re.search(r'\brm\s+', command.lower()):
        return "SYSTEM ERROR: Destructive commands (rm) are blocked. Use the archive folder instead."

    # 2. Catch if it used 'cmd' instead of 'command'
    if command is None and cmd is not None:
        return "SYSTEM ERROR: You used the wrong parameter name. You MUST use 'command', not 'cmd'."
    if command is None:
        return "SYSTEM ERROR: Missing required parameter 'command'."

    # 3. Catch if it used a list/array instead of a string
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

        # --- THE POINTER APPROACH (Context Protection) ---
        if len(output) > 10000:
            # Generate a clean timestamped filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_file_name = f"cmd_output_{timestamp}.txt"
            temp_file_path = os.path.join(SANDBOX_DIR, temp_file_name)
            
            # Save the full massive output safely to the sandbox
            with open(temp_file_path, "w", encoding="utf-8") as f:
                f.write(output)
                
            preview = output[:3000] # Give it just enough to see the structure/headers
            
            return (f"Exit Code: {process.returncode}\n"
                    f"Output Preview (First 3000 chars):\n{preview}\n\n"
                    f"... [SYSTEM: The full output ({len(output)} chars) was too large for your context window and was saved to '/app/workspace/sandbox/{temp_file_name}'. "
                    f"Do NOT attempt to parse this preview. If you need the full data, use the 'analyze_files' tool or bash 'grep'.]")

        return f"Exit Code: {process.returncode}\nOutput:\n{output}"
        
    except Exception as e:
        return f"Error executing command: {str(e)}"
        

@mcp.tool()
def write_file(filepath: str, content: str) -> str:
    """Creates or overwrites a file with the provided content.
    If the file already exists, it automatically creates a timestamped backup before overwriting.
    Use this instead of bash 'echo' or 'cat' to write markdown, or text files safely.
    You can only write to 'outputs' or 'sandbox' directories.
    IMPORTANT: For Python code, use forge_and_register_tool!
    """

    if filepath.strip().endswith(".py"):
        return "SYSTEM ERROR: You are strictly FORBIDDEN from using write_file to create Python (.py) scripts. You MUST use 'forge_and_register_tool' so the Coder LLM can write it properly."

    # 1. Resolve the absolute path
    resolved_path = os.path.realpath(filepath)
    
    # 2. Define safe zones
    allowed_dirs = [
        os.path.realpath("/app/workspace/outputs"),
        os.path.realpath("/app/workspace/sandbox")
    ]
    
    # 3. Check if the resolved path starts with any allowed directory
    is_safe = any(os.path.commonpath([resolved_path, safe_dir]) == safe_dir for safe_dir in allowed_dirs)
    
    if not is_safe:
        return f"SYSTEM ERROR: Path Traversal Blocked. You are only allowed to write files to {allowed_dirs}."

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
    
    # Look up max tokens for Summarizer
    max_context = summarizer_profile.get("api_params", {}).get("max_tokens", 32768)
    safe_budget = int(max_context * 0.85)
    
    # --- Safe string accumulator to prevent MCP stdio corruption ---
    rolling_log = "" 
    
    # --- SMART TOKEN-TARGETED ROLLING SUMMARIZATION ---
    current_tokens = len(tokenizer.encode(json.dumps(current_history)))
    
    # 1. Pre-compute token counts for each message ONCE
    msg_tokens = [len(tokenizer.encode(json.dumps(msg))) for msg in current_history]
    current_tokens = sum(msg_tokens)
    
    
    if current_tokens > safe_budget:
        rolling_log += f"\n\n[SYSTEM METRIC: Pre-compression history exceeded limits ({current_tokens} > {safe_budget}). Executed Smart Rolling Summarization:]\n"
        
        while current_tokens > safe_budget and len(current_history) > 3:
            excess_tokens = current_tokens - safe_budget
            
            max_chunk_size = safe_budget - 2000 
            target_chunk_size = min(excess_tokens + 500, max_chunk_size)
            
            chunk_to_compress = []
            chunk_tokens = 0
            slice_end_index = 1
            
            for i in range(1, len(current_history)):
                # Use our pre-computed array instead of recalculating!
                if chunk_tokens + msg_tokens[i] > max_chunk_size and chunk_tokens > 0:
                    break
                    
                chunk_to_compress.append(current_history[i])
                chunk_tokens += msg_tokens[i]
                slice_end_index = i + 1
                
                if chunk_tokens >= target_chunk_size:
                    break
                                
            # --- Chronological Bulleted List ---
            chunk_prompt = [
                {"role": "system", "content": "You are a context compressor. Condense the following chat history into a highly dense, chronological bulleted list of key events, tool executions, and findings. Retain exact file paths, critical data points, and decisions. Be extremely concise. Do not use conversational filler."},
                {"role": "user", "content": json.dumps(chunk_to_compress)}
            ]
            
            chunk_args = summarizer_profile["api_params"].copy()
            chunk_args["model"] = summarizer_profile["model"]
            chunk_args["messages"] = chunk_prompt
            
            try:
                chunk_resp = await summarizer_client.chat.completions.create(**chunk_args)
                dense_summary = chunk_resp.choices[0].message.content
                
                # Formatted clearly so the Brain can read it easily
                summary_msg = {"role": "system", "content": f"[ARCHIVED HISTORY (Chronological Summary)]\n{dense_summary}"}
                
                # Calculate the token size of the new summary message
                summary_tokens = len(tokenizer.encode(json.dumps(summary_msg)))
                
                # Splice the history array
                current_history = [current_history[0]] + [summary_msg] + current_history[slice_end_index:]
                
                # Splicing the token array mathematically (O(1) speed!)
                msg_tokens = [msg_tokens[0]] + [summary_tokens] + msg_tokens[slice_end_index:]
                current_tokens = sum(msg_tokens)
                
                rolling_log += f"- Compressed a {chunk_tokens}-token chunk. New total: {current_tokens} tokens.\n"
                            
            except Exception as e:
                rolling_log += f"- Error during chunk compression: {e}. Falling back to single-message truncation to survive.\n"
                current_history.pop(1)
                current_tokens = len(tokenizer.encode(json.dumps(current_history)))

    # Serialize compactly to save token overhead before sending to the main summarizer
    bloated_text = json.dumps(current_history)
        
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
        new_history.insert(0, {"role": "system", "content": config.PROMPTS["overseer_system"]})
        save_json(CURRENT_HISTORY_FILE, new_history)
        
        return f"SUCCESS: Context compressed and old history moved to {os.path.basename(backup_file)}.{rolling_log}\nNew detailed memories extracted to disk: {added_titles}. \n[SYSTEM INSTRUCTION: Your context has been reset, and any requested memory extraction has been completed. Review your 'Active Plan'. If the user's last command was simply to compress/save memory, do NOT do it again—simply tell them it is complete.]"
        
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
            
            # --- AUTO-BACKUP SYSTEM ---
            if os.path.exists(file_path):
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                backup_dir = os.path.join(WORKSPACE_DIR, "archive")
                backup_path = os.path.join(backup_dir, f"{safe_name}_{timestamp}.bak.py")
                shutil.copy2(file_path, backup_path)
            
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

                # Safely split the string into a list of arguments, neutralizing semicolons/pipes
                safe_deps_list = shlex.split(clean_deps) 

                install_cmd = [sys.executable, "-m", "pip", "install", "--target", "/app/workspace/custom_packages"] + safe_deps_list

                # Use asyncio.to_thread so we don't freeze the MCP server during downloads!
                install_check = await asyncio.to_thread(
                    subprocess.run, install_cmd, cwd=WORKSPACE_DIR, capture_output=True, text=True
                )

                if install_check.returncode == 0:
                    deps_report = f"\n[SYSTEM: Automatically installed '{clean_deps}' into persistent session delta.]"
                else:
                    deps_report = f"\n[SYSTEM WARNING: Failed to auto-install dependencies: {install_check.stderr}]"
            
            # Non-blocking compile check
            check = await asyncio.to_thread(
                subprocess.run, [sys.executable, "-m", "py_compile", filename], cwd=FORGED_TOOLS_DIR, capture_output=True, text=True
            )
            
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
                # Clean up the broken script so it doesn't clutter the directory
                if os.path.exists(file_path):
                    os.remove(file_path)
                error_msg = f"Your code failed syntax validation with error:\n{check.stderr}\nPlease fix it and try again. Output ONLY valid python."
                messages.append({"role": "assistant", "content": code})
                messages.append({"role": "user", "content": error_msg})
                
        except Exception as e:
            error_trace = traceback.format_exc()
            return f"Fatal API Error during forging attempt {attempt+1}.\nError: {str(e)}\n\nDetailed Traceback:\n{error_trace}"
            
    return f"FAILED: Coder LLM could not produce valid code after {config.MAX_FORGE_RETRIES} attempts."


db_tool_desc = f"""Executes a SQL query against a specified SQLite database.
'db_path' MUST be an absolute path (e.g., '/app/workspace/state/my_db.db').

CRITICAL RULES:
- CONTEXT: Always use LIMIT in your SELECT queries (e.g., LIMIT 10) to protect your context window!
- MULTIPLE STATEMENTS: To execute multiple schema creation commands at once, you may chain them with semicolons.
- BULK INSERTS: To insert multiple rows efficiently, write a single parameterized INSERT statement and pass a LIST OF LISTS to 'parameters'. The system will automatically use batch execution (executemany).

SEMANTIC SEARCH: If you provide a string in 'search_text_to_embed', the system will automatically generate its vector and append it to the end of your 'parameters' list. 
Search Syntax Example:
  query="SELECT m.*, v.distance FROM (SELECT rowid, distance FROM docs_vec WHERE embedding MATCH ? ORDER BY distance LIMIT 5) v JOIN docs m ON v.rowid = m.id"
  parameters=[]
  search_text_to_embed="My search query"
"""

@mcp.tool(description=db_tool_desc)
async def query_sqlite_db(db_path: str, query: str, parameters: list = None, search_text_to_embed: str = None) -> str:
    try:
        # --- Handle Search Embedding Injection ---
        if search_text_to_embed:
            response = await universal_client.embeddings.create(
                model=config.EMBEDDING_CONFIG["model"],
                input=search_text_to_embed
            )
            embedding_vector = response.data[0].embedding
            vector_blob = array.array('f', embedding_vector).tobytes()
            
            if parameters is None:
                parameters = []
            if len(parameters) > 0 and isinstance(parameters[0], list):
                return "SYSTEM ERROR: You cannot use 'search_text_to_embed' simultaneously with bulk (list of lists) parameters."
            parameters.append(vector_blob)
        
        upper_query = query.strip().upper()
        if upper_query.startswith("DROP TABLE") or upper_query.startswith("DELETE FROM"):
            return "SYSTEM ERROR: 'DROP TABLE' and 'DELETE FROM' are disabled for safety. To 'delete' data, rename the table using ALTER."
        
        # --- Database Execution ---
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        is_bulk_operation = False
        
        if parameters:
            # Detect Bulk Insert (List of Lists)
            if len(parameters) > 0 and isinstance(parameters[0], list):
                cursor.executemany(query, parameters)
                is_bulk_operation = True
            else:
                cursor.execute(query, parameters)
        else:
            try:
                cursor.execute(query)
            except Exception as e:
                # Catch multi-statement attempts and cleanly route them to executescript
                if "one statement at a time" in str(e).lower() or "can only execute one statement" in str(e).lower():
                    cursor.executescript(query)
                    is_bulk_operation = True
                else:
                    raise e
            
        # --- Output Formatting & CONTEXT PROTECTION ---
        if is_bulk_operation or cursor.description is None:
            # executescript and executemany don't return fetchable rows
            rowcount = cursor.rowcount
            result_str = f"Operation executed successfully. Rows affected/processed: {rowcount}"
        else:
            rows = cursor.fetchall()
            if not rows:
                result_str = "Query executed successfully. 0 rows returned."
            else:
                MAX_ROWS = 50
                warning_msg = ""
                if len(rows) > MAX_ROWS:
                    warning_msg = f"\n\n... [SYSTEM WARNING: The query returned {len(rows)} rows, but only the first {MAX_ROWS} are shown to protect context. Use 'LIMIT' to paginate.]"
                    rows = rows[:MAX_ROWS]
                
                data = [dict(row) for row in rows]
                result_str = json.dumps(data, indent=2, default=str)
                
                if len(result_str) > 20000:
                    result_str = result_str[:20000] + "\n\n... [SYSTEM WARNING: JSON output exceeded 20,000 characters and was truncated. Refine your SQL query.]"
                else:
                    result_str += warning_msg
                    
        conn.commit()
        conn.close()
        return result_str
        
    except Exception as e:
        return f"SYSTEM ERROR: Database Exception: {str(e)}"


@mcp.tool()
async def batch_generate_embeddings(db_path: str, vec_table: str, rowids: list[int], texts_to_embed: list[str]) -> str:
    """
    Generates vector embeddings in bulk and inserts them into a vec0 virtual table.
    Use this AFTER you have already inserted your data into a standard SQLite metadata table.
    You must provide a list of 'rowids' (from your metadata table) and a perfectly matching list of 'texts_to_embed'.
    """
    if not rowids or not texts_to_embed:
        return "SYSTEM ERROR: The lists cannot be empty."
        
    if len(rowids) != len(texts_to_embed):
        return f"SYSTEM ERROR: Mismatch! You provided {len(rowids)} rowids but {len(texts_to_embed)} texts."
        
    try:
        # 1. Generate ALL embeddings in a single lightning-fast API call
        response = await universal_client.embeddings.create(
            model=config.EMBEDDING_CONFIG["model"],
            input=texts_to_embed
        )
        
        # 2. Connect to the SQLite database
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        cursor = conn.cursor()
        
        inserted_count = 0
        
        # 3. Insert ONLY into the vec_table
        for i, rowid in enumerate(rowids):
            embedding_vector = response.data[i].embedding
            vector_blob = array.array('f', embedding_vector).tobytes()
            
            # Insert the vector using the provided rowid to link them
            cursor.execute(f"INSERT INTO {vec_table}(rowid, embedding) VALUES (?, ?)", (rowid, vector_blob))
            inserted_count += 1
            
        conn.commit()
        conn.close()
        
        return f"SUCCESS: Batch processed {inserted_count} embeddings and inserted them into {vec_table}."
        
    except Exception as e:
        return f"SYSTEM ERROR: Batch Embedding Failed. {str(e)}"
        

@mcp.tool()
async def search_web(query: str, max_results: int = 5) -> str:
    """Searches the web using a stealth browser to find relevant URLs and snippets.
    ALWAYS use this tool first to find factual URLs before using fetch_webpage.
    Do NOT guess or fabricate URLs.
    """
    # We use DuckDuckGo's legacy HTML page because it lacks advanced JS bot-detection
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    
    try:
        # Spin up the exact same stealth browser you use for fetch_webpage
        async with Stealth().use_async(async_playwright()) as p:
            browser = await p.chromium.launch(
                headless=True, 
                args=[
                    '--no-sandbox', 
                    '--disable-setuid-sandbox', 
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled' 
                ]
            )
            
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-US"
            )
            
            page = await context.new_page()
            
            # Navigate to the search page
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            html_content = await page.content()
            await browser.close()
            
        # Parse the raw HTML using BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
        results = soup.find_all('div', class_='result')
        
        if not results:
            return f"SYSTEM ERROR: No results found for '{query}'. The stealth browser may have been served a Captcha."
            
        formatted_results = f"--- SEARCH RESULTS FOR '{query}' ---\n\n"
        count = 0
        
        for row in results:
            if count >= max_results:
                break
                
            title_tag = row.find('a', class_='result__a')
            snippet_tag = row.find('a', class_='result__snippet')
            url_tag = row.find('a', class_='result__url')
            
            if title_tag and snippet_tag and url_tag:
                title = title_tag.text.strip()
                snippet = snippet_tag.text.strip()
                
                # The visual URL text is usually cleaner than the href redirect link
                actual_url = url_tag.text.strip()
                if not actual_url.startswith("http"):
                    actual_url = "https://" + actual_url
                    
                formatted_results += f"{count+1}. {title}\nURL: {actual_url}\nSnippet: {snippet}\n\n"
                count += 1
                
        if count == 0:
            return f"SYSTEM ERROR: Could not extract valid links from the search page."
            
        return formatted_results

    except Exception as e:
        return f"SYSTEM ERROR: Stealth search failed. {str(e)}"


@mcp.tool()
async def fetch_webpage(url: str) -> str:
    """Fetches a webpage using a headless Chromium browser to render JavaScript, 
    and returns ONLY the clean, readable text. 
    Use this to read documentation, articles, or search results without writing a custom scraper.
    """
    try:
        async with Stealth().use_async(async_playwright()) as p:
            # Launch chromium natively in headless mode with container-safe flags
            browser = await p.chromium.launch(
                headless=True, 
                args=[
                    '--no-sandbox', 
                    '--disable-setuid-sandbox', 
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled' 
                ]
            )
            
            # Spoof a realistic Windows Chrome browser
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York"
            )
            
            page = await context.new_page()
                        
            # Navigate and wait for the page to finish loading its network requests (JS rendering)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        
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
            
            # --- THE POINTER APPROACH (Context Protection) ---
            if len(clean_text) > 20000:
                # Create a safe filename based on the domain name
                parsed_url = urllib.parse.urlparse(url)
                safe_domain = re.sub(r'[^a-zA-Z0-9]', '_', parsed_url.hostname or "webpage")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                temp_file_name = f"web_{safe_domain}_{timestamp}.txt"
                temp_file_path = os.path.join(SANDBOX_DIR, temp_file_name)
                
                # Save the full scraped text
                with open(temp_file_path, "w", encoding="utf-8") as f:
                    f.write(f"--- FULL CONTENT FROM {url} ---\n\n{clean_text}")
                    
                preview = clean_text[:6000]
                
                return (f"--- PREVIEW FROM {url} ---\n\n{preview}\n\n"
                        f"... [SYSTEM: The webpage was {len(clean_text)} characters long. To protect your context window, "
                        f"the full text was saved to '/app/workspace/sandbox/{temp_file_name}'. Use the 'analyze_files' tool to read it fully if needed.]")
                
            return f"--- CONTENT FROM {url} ---\n\n{clean_text}"

    except Exception as e:
        return f"Failed to fetch {url}. Error: {str(e)}"


@mcp.tool()
async def analyze_files(filepaths: list[str], instruction: str) -> str:
    """Delegates the analysis of multiple massive text files, logs, or images to the Analyst LLM.
    Use this to prevent large files from blowing out your context window, or to compare multiple files.
    'filepaths' must be a list of absolute paths to the files.
    'instruction' must be a specific question or command (e.g., "Compare these logs", "Find the error between this code and this log").
    """
    api_args = analyst_profile["api_params"].copy()
    api_args["model"] = analyst_profile["model"]
    
    # We build a multi-part message array
    user_content = [{"type": "text", "text": f"Instruction: {instruction}\n\n"}]
    
    try:
        for filepath in filepaths:
            if not os.path.exists(filepath):
                user_content.append({"type": "text", "text": f"\n[ERROR: File '{filepath}' does not exist.]"})
                continue

            mime_type, _ = mimetypes.guess_type(filepath)
            is_image = mime_type and mime_type.startswith('image/')
            
            filename = os.path.basename(filepath)
            
            if is_image:
                # --- VISION PIPELINE ---
                with open(filepath, "rb") as image_file:
                    base64_image = base64.b64encode(image_file.read()).decode('utf-8')
                user_content.append({"type": "text", "text": f"\n--- IMAGE: {filename} ---"})
                user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}})
            else:
                # --- TEXT PIPELINE ---
                with open(filepath, "r", encoding="utf-8", errors="replace") as text_file:
                    file_content = text_file.read(50000) # Only reads the first 50k characters
                    
                # Truncate to ~50k chars per file to prevent crashing the Analyst on multi-file requests
                if len(file_content) == 50000:
                    file_content += "\n... [TRUNCATED DUE TO SIZE]"
                    
                user_content.append({"type": "text", "text": f"\n--- TEXT FILE: {filename} ---\n{file_content}\n"})

        api_args["messages"] = [
            {"role": "system", "content": config.PROMPTS["analyst_system"]},
            {"role": "user", "content": user_content}
        ]
        
        # --- DYNAMIC PAYLOAD CHECKER ---
        # 1. Look up the max context limit for the Analyst profile (defaulting to 32k if missing)
        max_context = analyst_profile.get("api_params", {}).get("max_tokens", 32768)
        safe_budget = int(max_context * 0.90) # Leave 10% for the response!
        
        # 2. Accurately measure what we are about to send
        payload_tokens = get_payload_tokens(api_args["messages"])
        
        # 3. Bounce the request back to the Brain if it's too massive
        if payload_tokens > safe_budget:
            return (f"SYSTEM ERROR: The files you asked the Analyst to read are too massive! "
                    f"Your payload is {payload_tokens} tokens, but the safety limit is {safe_budget} tokens. "
                    f"Please run 'analyze_files' on fewer files at a time, or use bash tools like 'head', 'tail', or 'grep' to narrow down the data first.")
                    
        # --- 1. FORCE THE JSON SCHEMA ---
        api_args["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "analyst_report_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "executive_summary": {
                            "type": "string", 
                            "description": "A 1-3 sentence definitive answer or core conclusion."
                        },
                        "full_report": {
                            "type": "string", 
                            "description": "The exhaustive, detailed analysis and breakdown."
                        }
                    },
                    "required": ["executive_summary", "full_report"],
                    "additionalProperties": False
                }
            }
        }

        # Call the Analyst Model
        response = await analyst_client.chat.completions.create(**api_args)
        
        # --- 2. PARSE THE JSON ---
        try:
            report_data = json.loads(response.choices[0].message.content)
            ex_summ = report_data.get("executive_summary", "")
            full_rep = report_data.get("full_report", "")
        except json.JSONDecodeError:
            # Fallback just in case the JSON breaks
            ex_summ = "Failed to parse JSON."
            full_rep = response.choices[0].message.content
            
        file_list = ", ".join([os.path.basename(f) for f in filepaths])
        combined_text = f"--- EXECUTIVE SUMMARY ---\n{ex_summ}\n\n--- DETAILED REPORT ---\n{full_rep}"
        
        # --- 3. AUTO-SAVE THE FULL COMBINED REPORT ---
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_analyst_report.md"
        filepath = os.path.join(STATE_DIR, filename)
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(combined_text)
            
        # --- 4. YOUR DYNAMIC ROUTING LOGIC ---
        if len(combined_text) > 20000:
            # Return ONLY the summary
            return (f"--- ANALYST EXECUTIVE SUMMARY FOR [{file_list}] ---\n{ex_summ}\n\n"
                    f"... [SYSTEM ALERT: The detailed report was {len(combined_text)} chars long. To protect your context window, "
                    f"the full analysis was saved to '/app/workspace/state/{filename}'.]")
        else:
            # Return BOTH
            return f"--- ANALYST REPORT FOR [{file_list}] ---\n{combined_text}\n\n[SYSTEM: A backup of this report was saved to '/app/workspace/state/{filename}']"

    except Exception as e:
        return f"Analyst failed to process files. Error: {str(e)}"
        
if __name__ == "__main__":
    mcp.run()