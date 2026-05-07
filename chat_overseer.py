import os
import asyncio
import json
import time
import re
import copy
import subprocess
import traceback
import logging
import argparse
import sys
import atexit
from datetime import datetime
from openai import AsyncOpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from contextlib import nullcontext

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel

import config

# Can be run using arguments like this:
# pixi run python chat_overseer.py -p "tell me what tools do you have" -f formatted -s "Test_2" --brain 2 --coder 3 --summarizer 3 --adviser 3 -x

# --- CLI ARGUMENT PARSER ---
parser = argparse.ArgumentParser(description="AI-Forge Overseer")
parser.add_argument("-p", "--prompt", type=str, help="Initial user prompt")
parser.add_argument("-f", "--format", choices=["formatted", "text", "silent"], help="Console mode override")
parser.add_argument("-s", "--session", type=str, help="Session ID to load or create")
parser.add_argument("-x", "--exit", action="store_true", help="Auto-exit after finishing the CLI prompt")
# Add the LLM profile overrides back:
parser.add_argument("--brain", type=int, help="Brain LLM profile index")
parser.add_argument("--coder", type=int, help="Coder LLM profile index")
parser.add_argument("--summarizer", type=int, help="Summarizer LLM profile index")
parser.add_argument("--adviser", type=int, help="Adviser LLM profile index")

cli_args = parser.parse_args()

# Apply Overrides BEFORE any LLM clients initialize
if cli_args.format: config.CONSOLE_MODE = cli_args.format
if cli_args.session: config.SESSION_ID = cli_args.session
if cli_args.brain is not None: config.ACTIVE_BRAIN_PROFILE = cli_args.brain
if cli_args.coder is not None: config.ACTIVE_CODER_PROFILE = cli_args.coder
if cli_args.summarizer is not None: config.ACTIVE_SUMMARIZER_PROFILE = cli_args.summarizer
if cli_args.adviser is not None: config.ACTIVE_ADVISER_PROFILE = cli_args.adviser

# Capture Prompt (from -p flag OR piped STDIN)
cli_prompt = cli_args.prompt
if not cli_prompt and not sys.stdin.isatty():
    # This grabs piped text like: cat input.txt | python chat_overseer.py
    cli_prompt = sys.stdin.read().strip()
   
# Mute the MCP client's internal info logs
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("fastmcp").setLevel(logging.WARNING)

# Initialize the rich console
console = Console()

# --- CLI Colors ---
COLOR_RED = '\033[91m'
COLOR_BLUE = '\033[94m'
COLOR_YELLOW = '\033[93m'
COLOR_BRIGHT_GREEN = '\033[92m'
COLOR_DARK_GREEN = '\033[32m'
COLOR_ORANGE = '\033[38;5;208m' # ANSI 256-color orange
COLOR_DIM = '\033[2m'   # Dim text for "thinking"
COLOR_RESET = '\033[0m'

brain_profile = config.LLM_PROFILES[config.ACTIVE_BRAIN_PROFILE]
if brain_profile.get("base_url"):
    brain_client = AsyncOpenAI(base_url=brain_profile["base_url"], api_key=brain_profile["api_key"], timeout=180.0)
else:
    brain_client = AsyncOpenAI(api_key=brain_profile["api_key"], timeout=180.0)

# --- SESSION & DIRECTORY SETUP ---
timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

if config.SESSION_ID:
    active_session = f"Session_ID_{config.SESSION_ID}"
else:
    active_session = f"Session_ID_{timestamp}"

SESSION_DIR = os.path.abspath(f"./sessions/{active_session}")
is_resuming = os.path.exists(SESSION_DIR)

# Build the isolated folder structure
os.makedirs(f"{SESSION_DIR}/logs", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/forged_tools", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/histories", exist_ok=True) 
os.makedirs(f"{SESSION_DIR}/memories", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/state", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/sandbox", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/outputs", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/archive", exist_ok=True)

# Build the isolated folder structure for input/output folders
os.makedirs(config.HOST_INPUT_DIR, exist_ok=True)

# --- Pixi environment already baked into the podman container! ---

# Point the log file to this specific session
LOG_FILE = f"{SESSION_DIR}/logs/chat_log_{timestamp}.txt"
CURRENT_HISTORY_FILE = f"{SESSION_DIR}/state/current_history.json"

# --- STATE MANAGEMENT HELPERS ---
def load_history():
    """Loads the true state of the brain from the hard drive."""
    if not os.path.exists(CURRENT_HISTORY_FILE):
        init_state = [{"role": "system", "content": config.PROMPTS["overseer_system"]}]
        save_history(init_state)
        return init_state
    with open(CURRENT_HISTORY_FILE, "r") as f: return json.load(f)

def save_history(messages):
    """Saves the active history. Strips thinking tokens."""
    clean_messages = []
    for msg in messages:
        clean_msg = copy.deepcopy(msg)
        clean_msg.pop("reasoning_content", None)
        clean_messages.append(clean_msg)
        
    with open(CURRENT_HISTORY_FILE, "w") as f: 
        json.dump(clean_messages, f, indent=4)

def estimate_tokens(messages):
    """Rough heuristic: 4 chars = 1 token. Only counts actual content, ignoring JSON boilerplate."""
    total_text = ""
    for msg in messages:
        total_text += str(msg.get("content", ""))
        # Also count tool arguments if they exist
        if "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                total_text += str(tc.get("function", {}).get("arguments", ""))
                
    return len(total_text) // 4
    
def log_event(role, content, usage=None, thinking=None, text_color=None):
    time_str = datetime.now().strftime("%H:%M:%S")
    
    # 1. Plain Text Log File (Append to file silently)
    file_msg = f"\n[{time_str}] === {role.upper()} ===\n"
    if thinking:
        file_msg += f"<thinking>\n{thinking}\n</thinking>\n\n"
    file_msg += f"{content}\n"
    if usage and usage.prompt_tokens is not None:
        file_msg += f"[Tokens: {usage.prompt_tokens} in | {usage.completion_tokens} out]\n"
        
    with open(LOG_FILE, "a", encoding="utf-8") as f: 
        f.write(file_msg)

    # 2. Console logging
    if config.CONSOLE_MODE != "silent":
        if role.upper() not in ["BRAIN"]:
            if role.upper().startswith("TOOL"):
                console_header = f"\n{COLOR_BRIGHT_GREEN}[{time_str}] === {role.upper()} ==={COLOR_RESET}"
                actual_color = text_color if text_color else COLOR_DARK_GREEN
                console_content = f"{actual_color}{content}{COLOR_RESET}"
            else:
                console_header = f"\n{COLOR_BLUE}[{time_str}] === {role.upper()} ==={COLOR_RESET}"
                console_content = f"{COLOR_RED}{content}{COLOR_RESET}" if role.upper() in ["USER", "YOU"] else content
                
            print(f"{console_header}\n{console_content}")
            if usage and usage.prompt_tokens is not None:
                print(f"{COLOR_YELLOW}[Tokens: {usage.prompt_tokens} in | {usage.completion_tokens} out]{COLOR_RESET}")

active_container_name = None

def cleanup_container():
    """Guarantees the container is killed when the python script exits."""
    if active_container_name:
        if config.CONSOLE_MODE != "silent":
            print(f"\n\033[91m[SYSTEM] Tearing down container {active_container_name}...\033[0m")
        subprocess.run(
            f"podman rm -f {active_container_name}", 
            shell=True, 
            stderr=subprocess.DEVNULL, 
            stdout=subprocess.DEVNULL
        )

# Register the cleanup function to run when the script dies
atexit.register(cleanup_container)

async def run_chat():
    banner = (
        f"Session: [{active_session}]\n"
        f"Brain:      {config.LLM_PROFILES[config.ACTIVE_BRAIN_PROFILE]['name']}\n"
        f"Coder:      {config.LLM_PROFILES[config.ACTIVE_CODER_PROFILE]['name']}\n"
        f"Summarizer: {config.LLM_PROFILES[config.ACTIVE_SUMMARIZER_PROFILE]['name']}\n"
        f"Adviser:    {config.LLM_PROFILES[config.ACTIVE_ADVISER_PROFILE]['name']}\n"
        f"Log saved to: {LOG_FILE}"
    )
    log_event("SYSTEM", banner)
    
    prompt_session = None
    quit_app = False
    last_known_tokens = 0 # State tracker for accurate token checking
    
    if is_resuming:
        log_event("SYSTEM", f"Successfully restored '{active_session}'. Your forged tools are loaded. Type '/exit' or '/quit' to close.")
    else:
        log_event("SYSTEM", f"Started new workspace: '{active_session}'. Type '/exit' or '/quit' to close.")

    # THE SELF-HEALING CONNECTION LOOP
    while not quit_app:
        global active_container_name
        active_container_name = f"forge_sandbox_{active_session}_{os.getpid()}"
        # Clear stale Podman WSL state before every single boot/reboot ---
        if config.CONSOLE_MODE != "silent":
            print(f"{COLOR_DIM}Sweeping stale Podman state...{COLOR_RESET}")

        subprocess.run(
            f"podman rm -f -i {active_container_name}",
            #"podman system prune -f && podman rm -f $(podman ps -aq)",
            #"rm -rf ~/.podman-run/containers ~/.podman-run/libpod/tmp",
            shell=True, 
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL
        )

        try:
            # --- 1. DYNAMICALLY BUILD THE TARGET COMMAND ---
            # Start with the base command
            god_tools_cmd = "pixi run --locked --manifest-path /app/pixi.toml -q python /app/god_tools.py"
            
            # Append any CLI profile overrides that were provided
            if cli_args.coder is not None: god_tools_cmd += f" --coder {cli_args.coder}"
            if cli_args.summarizer is not None: god_tools_cmd += f" --summarizer {cli_args.summarizer}"
            if cli_args.adviser is not None: god_tools_cmd += f" --adviser {cli_args.adviser}"
                    
            server_params = StdioServerParameters(
                command="podman",
                args=[
                    "--log-level=error",
                    "run", "-i", "--rm",
                    "--init",
                    f"--name={active_container_name}", # True unique identifier
                    "--network=slirp4netns", # networking mode built specifically for rootless Podman
                    "--add-host=host.containers.internal:host-gateway",
                    "--security-opt=no-new-privileges:true", # Prevent privilege escalation
                    "--cap-drop=ALL",         # Drop all Linux capabilities
                    "--cpus=4.0",            # Limit to 4 CPU cores
                    "--memory=16g",           # Limit to 16 GB of RAM
                    "--pids-limit=1000",      # Neutralizes bash fork bombs
                    "--userns=keep-id",
                    "--device=nvidia.com/gpu=all", # GPU Passthrough!
#                    "--storage-opt", "size=10G", # Limits the container's scratch space, does not work on WSL2
                    "-v", f"{SESSION_DIR}:/app/workspace:Z",
                    "-v", f"{os.path.abspath('./config.py')}:/app/config.py:ro,Z",
                    "-v", f"{os.path.abspath('./god_tools.py')}:/app/god_tools.py:ro,Z",
                    "-v", f"{config.HOST_INPUT_DIR}:/app/host_input:ro,Z", # same for all sessions, read only
                    "ai-forge",
                    "bash", "-c", f"""
                    # 1. Ensure the persistent custom packages folder exists
                    mkdir -p /app/workspace/custom_packages
                    
                    # 2. Tell Python to load packages from both the Pixi Base AND the Session Delta
                    export PYTHONPATH=/app/workspace/custom_packages:$PYTHONPATH
                    
                    # 3. Start the MCP server using the Base Image's heavy Pixi environment
                    cd /app/workspace
                    {god_tools_cmd}
                    """
                ]
            )
            
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    
                    mcp_tools = await session.list_tools()
                    openai_tools = [
                        {
                            "type": "function", 
                            "function": {
                                "name": t.name, 
                                "description": t.description, 
                                "parameters": t.inputSchema,
                                "strict": True
                            }
                        } 
                        for t in mcp_tools.tools
                    ]
                    
                    # Initialize tracker outside the loop
                    cli_prompt_consumed = False
                    
                    while True:
                        try:
                            # 1. Check if we have a CLI or Piped prompt to use first
                            if cli_prompt and not cli_prompt_consumed:
                                user_input = cli_prompt
                                cli_prompt_consumed = True
                            else:
                                # 2. Auto-exit for silent mode after CLI prompt is done
                                if cli_prompt and (config.CONSOLE_MODE == "silent" or cli_args.exit):
                                    quit_app = True
                                    break
                                    
                                # 3. Normal interactive mode
                                # Only initialize the prompter if we actually need human input!
                                if prompt_session is None:
                                    prompt_session = PromptSession()

                                prompt_text = ANSI(f"\n{COLOR_RED}YOU: {COLOR_RESET}")
                                user_input = await prompt_session.prompt_async(prompt_text)
                                
                        # Catch Ctrl+C and Ctrl+D gracefully
                        except KeyboardInterrupt:
                            continue
                        except EOFError:
                            quit_app = True
                            break

                        user_input = user_input.strip()
                        if user_input.lower() in ['/quit', '/exit', 'quit', 'exit']: 
                            quit_app = True
                            break
                        if not user_input: continue
                            
                        log_event("USER", user_input)
                        
                        # Load and save state
                        messages = load_history()
                        messages.append({"role": "user", "content": user_input})
                        save_history(messages)

                        # --- ESCALATING LOOP DETECTION (INIT) ---
                        consecutive_tool_chains = 0
                        
                        while True:
                            try:
                                messages = load_history()
                                
                                # --- 1. TIME INJECTION (Sweep & Replace) ---
                                # Remove any previous system clocks to prevent bloat
                                messages = [msg for msg in messages if not (msg.get("role") == "user" and "[SYSTEM CLOCK:" in str(msg.get("content")))]
                                
                                # Inject the fresh clock
                                live_time = datetime.now().strftime("%A, %B %d, %Y %H:%M:%S")
                                messages.insert(1, {
                                    "role": "user", 
                                    "content": f"[SYSTEM CLOCK: It is currently {live_time}]"
                                })

                                # --- 2. TOKEN WARNING INJECTION ---
                                current_token_estimate = last_known_tokens if last_known_tokens > 0 else estimate_tokens(messages)
                                pct = current_token_estimate / config.MAX_CONTEXT_TOKENS
                                
                                if pct >= 0.75:
                                    # Prevent appending multiple warnings in a row if the AI ignores it for a few turns
                                    messages = [msg for msg in messages if not (msg.get("role") == "user" and "[SYSTEM WARNING: Your context window is at" in str(msg.get("content")))]
                                    
                                    warn_msg = f"[SYSTEM WARNING: Your context window is at ~{pct*100:.0f}%. "
                                    if pct >= 0.90: warn_msg += "CRITICAL LIMIT REACHED. You MUST use the compress_and_store_context tool immediately.]"
                                    else: warn_msg += "Consider finishing your current task and using the compress_and_store_context tool soon.]"
                                    
                                    messages.append({"role": "user", "content": warn_msg})
                                    print(f"\n{COLOR_YELLOW}{warn_msg}{COLOR_RESET}")
                                    log_event("SYSTEM", warn_msg)

                                # --- 3. SAVE THE PURIST HISTORY BEFORE API CALL ---
                                save_history(messages)

                                # Setup API args using the true, saved messages
                                api_args = brain_profile["api_params"].copy()
                                api_args["model"] = brain_profile["model"]
                                api_args["messages"] = messages
                                api_args["tools"] = openai_tools
                                api_args["stream"] = True
                                api_args["stream_options"] = {"include_usage": True}
                                
                                if "seed" in api_args:
                                    api_args["seed"] = api_args.get("seed") or 42

                                response_stream = await brain_client.chat.completions.create(**api_args)
                                
                                if config.CONSOLE_MODE != "silent":
                                    time_str = datetime.now().strftime("%H:%M:%S")
                                    print(f"\n{COLOR_BLUE}[{time_str}] === BRAIN ==={COLOR_RESET}")
                                
                                full_content = ""
                                full_thinking = ""
                                tool_calls_dict = {}
                                final_usage = None
                                
                                # --- MULTI-MODE CONTEXT ROUTING ---
                                if config.CONSOLE_MODE == "formatted":
                                    display_ctx = Live(console=console, refresh_per_second=4, transient=False)
                                else:
                                    display_ctx = nullcontext()

                                with display_ctx as live:
                                    async for chunk in response_stream:
                                        
                                        if chunk.usage:
                                            final_usage = chunk.usage

                                        if len(chunk.choices) > 0:
                                            delta = chunk.choices[0].delta
                                            
                                            # Logic for thinking/content extraction stays the same
                                            chunk_thinking = getattr(delta, 'reasoning_content', None)
                                            if not chunk_thinking and hasattr(delta, "model_extra") and delta.model_extra:
                                                chunk_thinking = delta.model_extra.get("reasoning_content") or delta.model_extra.get("reasoning")
                                            
                                            if chunk_thinking: full_thinking += chunk_thinking
                                            if delta.content: full_content += delta.content

                                            # (Keep your existing tool_calls_dict logic here)
                                            if delta.tool_calls:
                                                for tc in delta.tool_calls:
                                                    if tc.index not in tool_calls_dict:
                                                        tool_calls_dict[tc.index] = {
                                                            "id": tc.id, 
                                                            "type": "function", 
                                                            "function": {"name": tc.function.name, "arguments": ""}
                                                        }
                                                    if tc.function.arguments:
                                                        tool_calls_dict[tc.index]["function"]["arguments"] += tc.function.arguments


                                            # --- CONSOLE MODE DISPATCHER ---
                                            if config.CONSOLE_MODE == "text":
                                                # Classic raw streaming
                                                if chunk_thinking:
                                                    print(f"{COLOR_DIM}{chunk_thinking}{COLOR_RESET}", end="", flush=True)
                                                if delta.content:
                                                    print(delta.content, end="", flush=True)
                                                    
                                            elif config.CONSOLE_MODE == "formatted":
                                                # Rich UI Assembly
                                                display_elements = []
                                                if full_thinking:
                                                    display_elements.append(Panel(
                                                        Markdown(full_thinking),
                                                        title="[bold yellow]BRAIN THOUGHTS[/bold yellow]", border_style="yellow", padding=(1, 2), subtitle="[dim white]Internal Logic Loop[/dim white]"
                                                    ))
                                                if full_content:
                                                    display_elements.append(Markdown(full_content))
                                                
                                                if not full_content and tool_calls_dict:
                                                    tool_names = [tc['function']['name'] for tc in tool_calls_dict.values()]
                                                    display_elements.append(Markdown(f"*Preparing tool calls: {', '.join(f'`{n}`' for n in tool_names)}...*"))
                                                    
                                                if display_elements:
                                                    live.update(Group(*display_elements))
                                                    
                                            # If mode is "silent", we do nothing visually!

                                if config.CONSOLE_MODE != "silent":
                                    print() # Drop a single clean newline after the stream is fully finished
                                    
                                assistant_message = {"role": "assistant", "content": full_content}
                                
                                if full_thinking:
                                    assistant_message["reasoning_content"] = full_thinking

                                if tool_calls_dict:
                                    assistant_message["tool_calls"] = list(tool_calls_dict.values())
                                    
                                messages = load_history()
                                messages.append(assistant_message)
                                save_history(messages)
                                log_event("BRAIN", full_content, final_usage, full_thinking)
                                
                                # Update exact token count state for the next loop!
                                if final_usage:
                                    last_known_tokens = final_usage.prompt_tokens + final_usage.completion_tokens
                                    reasoning_tokens = 0
                                    if hasattr(final_usage, 'completion_tokens_details') and final_usage.completion_tokens_details:
                                        reasoning_tokens = getattr(final_usage.completion_tokens_details, 'reasoning_tokens', 0)
                                    
                                    if reasoning_tokens == 0 and full_thinking:
                                        reasoning_tokens = len(full_thinking) // 4
                                        
                                    if config.CONSOLE_MODE != "silent":   
                                        if reasoning_tokens > 0:
                                            print(f"{COLOR_YELLOW}[Tokens: {final_usage.prompt_tokens} in | {final_usage.completion_tokens} out (~{reasoning_tokens} thinking)]{COLOR_RESET}")
                                        else:
                                            print(f"{COLOR_YELLOW}[Tokens: {final_usage.prompt_tokens} in | {final_usage.completion_tokens} out]{COLOR_RESET}")

                                if not tool_calls_dict:
                                    break

                                # --- Initialize the RAM cache for parallel tool outputs ---
                                executed_tool_outputs = {}

                                for tc_data in assistant_message["tool_calls"]:
                                    name = tc_data["function"]["name"]
                                    args_str = tc_data["function"]["arguments"]
                                    tc_id = tc_data["id"]
                                    
                                    # --- Intercept and self-heal bad JSON ---
                                    try:
                                        args = json.loads(args_str)
                                    except json.JSONDecodeError:
                                        error_msg = "SYSTEM ERROR: You provided invalid JSON arguments for this tool call. Please check your syntax (watch out for unescaped quotes or missing brackets) and try again."
                                        print(f"{COLOR_RED}Error decoding JSON. Intercepting and asking Brain to retry...{COLOR_RESET}")
                                        log_event("TOOL CALL", f"Requesting: {name}\nArgs: [MALFORMED JSON]\n{args_str}")
                                        log_event("TOOL RESULT (0.00s)", error_msg, text_color=COLOR_RED)
                                        
                                        # Cache the error so it survives compression!
                                        executed_tool_outputs[tc_id] = error_msg 
                                        
                                        messages = load_history()
                                        messages.append({
                                            "role": "tool",
                                            "tool_call_id": tc_id,
                                            "name": name,
                                            "content": error_msg
                                        })
                                        save_history(messages)
                                        continue 

                                    log_event("TOOL CALL", f"Requesting: {name}\nArgs: {json.dumps(args, indent=2)}")

                                    if config.CONSOLE_MODE != "silent":                                    
                                        if name == "forge_and_register_tool":
                                            print(f"\n{COLOR_ORANGE}▶ Passing task to Coder... Awaiting response...{COLOR_RESET}")
                                        elif name == "compress_and_store_context":
                                            print(f"\n{COLOR_ORANGE}▶ Triggering Memory Manager Pipeline... Awaiting response...{COLOR_RESET}")
                                        elif name == "consult_adviser":
                                            print(f"\n{COLOR_ORANGE}▶ Consulting Senior Adviser... Awaiting strategic report...{COLOR_RESET}")
                                        elif name == "query_universal_llm":
                                            print(f"\n{COLOR_ORANGE}▶ Spawning Sub-Agent... Awaiting response...{COLOR_RESET}")
                                            
                                    start = time.time()
                                    result = await session.call_tool(name, args)
                                    output = result.content[0].text
                                    
                                    # --- NEW: Save the real output to our RAM dictionary immediately ---
                                    executed_tool_outputs[tc_id] = output
                                    
                                    coder_thoughts = ""
                                    coder_code = ""

                                    if "<___CODER_THOUGHTS___>" in output:
                                        match = re.search(r"<___CODER_THOUGHTS___>(.*?)</___CODER_THOUGHTS___>", output, re.DOTALL)
                                        if match: coder_thoughts = match.group(1).strip()
                                        output = re.sub(r"<___CODER_THOUGHTS___>.*?</___CODER_THOUGHTS___>", "", output, flags=re.DOTALL).strip()

                                    if "<___CODER_CODE___>" in output:
                                        match = re.search(r"<___CODER_CODE___>(.*?)</___CODER_CODE___>", output, re.DOTALL)
                                        if match: coder_code = match.group(1).strip()
                                        output = re.sub(r"<___CODER_CODE___>.*?</___CODER_CODE___>", "", output, flags=re.DOTALL).strip()

                                    if coder_thoughts or coder_code:
                                        time_str = datetime.now().strftime("%H:%M:%S")
                                        log_text = f"\n[{time_str}] === CODER (HIDDEN) ===\n"

                                        if config.CONSOLE_MODE != "silent":
                                            print(f"\n{COLOR_ORANGE}[{time_str}] === CODER (HIDDEN) ==={COLOR_RESET}")

                                        if coder_thoughts:
                                            log_text += f"--- THOUGHTS ---\n{coder_thoughts}\n\n"
                                            if config.CONSOLE_MODE != "silent":
                                                print(f"{COLOR_DIM}--- THOUGHTS ---\n{coder_thoughts}\n{COLOR_RESET}")

                                        if coder_code:
                                            log_text += f"--- GENERATED CODE ---\n{coder_code}\n\n"
                                            if config.CONSOLE_MODE != "silent":
                                                print(f"--- GENERATED CODE ---\n{coder_code}\n")

                                        with open(LOG_FILE, "a", encoding="utf-8") as f:
                                            f.write(log_text)

                                    out_color = COLOR_ORANGE if name in ["forge_and_register_tool", "compress_and_store_context"] else COLOR_DARK_GREEN
                                    log_event(f"TOOL RESULT ({time.time() - start:.2f}s)", output, text_color=out_color)

                                    if name == "compress_and_store_context":
                                        print(f"\n{COLOR_ORANGE}[SYSTEM] Reloading compressed state from disk...{COLOR_RESET}")
                                        messages = load_history()
                                        messages.append(assistant_message)
                                        
                                        # --- NEW: Rebuild sequence using the RAM cache! ---
                                        for tc in assistant_message["tool_calls"]:
                                            current_tc_id = tc["id"]
                                            current_tc_name = tc["function"]["name"]
                                            
                                            # Pull the real output if it ran, otherwise fallback
                                            final_content = executed_tool_outputs.get(current_tc_id, "[SYSTEM NOTE: Tool execution aborted due to memory compression priority.]")
                                            
                                            messages.append({
                                                "role": "tool",
                                                "tool_call_id": current_tc_id,
                                                "name": current_tc_name,
                                                "content": final_content
                                            })
                                        
                                        save_history(messages)
                                        last_known_tokens = 0 
                                        consecutive_tool_chains = 0
                                        break 
                                    
                                    else:
                                        messages = load_history()
                                        messages.append({
                                            "role": "tool",
                                            "tool_call_id": tc_id,
                                            "name": name,
                                            "content": output
                                        })
                                        save_history(messages)

                                # --- ESCALATING LOOP DETECTION (CHECK) ---
                                consecutive_tool_chains += 1

                                if consecutive_tool_chains >= 300:
                                    # Hard Stop: Protect the API limits
                                    halt_msg = f"[SYSTEM METRIC: You have executed {consecutive_tool_chains} consecutive tool chains. For safety and observability, you MUST STOP using tools now. Summarize your progress and ask the user for permission to continue.]"
                                    messages = load_history()
                                    messages.append({"role": "user", "content": halt_msg})
                                    save_history(messages)
                                    print(f"\n{COLOR_YELLOW}[SYSTEM: Hard pause triggered ({consecutive_tool_chains} chains). Forcing Brain to wait for user.]{COLOR_RESET}")
                                    log_event("SYSTEM", halt_msg)
                                    consecutive_tool_chains = -999 # Prevent re-triggering while it writes the summary
                                
                                elif consecutive_tool_chains > 0 and consecutive_tool_chains % 100 == 0:
                                    # Sweep old soft-pauses
                                    messages = [msg for msg in messages if not (msg.get("role") == "user" and "[SYSTEM METRIC: You have executed" in str(msg.get("content")))]
                                    # Soft Reflection: Ask the AI to evaluate itself
                                    eval_msg = f"[SYSTEM METRIC: You have executed {consecutive_tool_chains} consecutive tool chains. Please review your recent actions. Are you making steady progress, or are you stuck in an error loop? If you are stuck or repeatedly failing, STOP using tools and ask the user for input. If you are making legitimate progress, continue.]"
                                    messages = load_history()
                                    messages.append({"role": "user", "content": eval_msg})
                                    save_history(messages)
                                    print(f"\n{COLOR_YELLOW}[SYSTEM: Soft pause triggered ({consecutive_tool_chains} chains). Prompting Brain to self-evaluate.]{COLOR_RESET}")
                                    log_event("SYSTEM", eval_msg)
                              
                                    
                            except (KeyboardInterrupt, asyncio.CancelledError):
                                print(f"\n\n{COLOR_RED}[SYSTEM] 🛑 Process manually interrupted! Returning to prompt...{COLOR_RESET}")
                                log_event("SYSTEM", "Process manually interrupted by user.")
                                
                                messages = load_history()
                                messages.append({
                                    "role": "user", 
                                    "content": "[SYSTEM ALERT: The user pressed Ctrl+C to instantly abort the previous text generation or tool execution. Stop what you were doing, acknowledge the interruption, and await new instructions.]"
                                })
                                save_history(messages)
                                break
                                
        # 3. CATCH DEAD CONTAINERS AND RESTART
        except Exception as e:
            
            print(f"\n{COLOR_RED}[CRASH DETECTED] {type(e).__name__}: {str(e)}{COLOR_RESET}")
            
            # UNPACK THE EXCEPTION GROUP ---
            print(f"{COLOR_YELLOW}--- TRACEBACK ---{COLOR_RESET}")
            traceback.print_exc()
            print(f"{COLOR_YELLOW}-----------------{COLOR_RESET}")
            
            print(f"\n{COLOR_YELLOW}[SYSTEM] Sandbox connection dropped or API failed. Restarting loop...{COLOR_RESET}")
            
            await asyncio.sleep(1) # Give the OS a second to clean up the dead Podman process
        except BaseExceptionGroup:
            # anyio throws BaseExceptionGroup when background tasks (like reading stdio) crash
            print(f"\n{COLOR_YELLOW}[SYSTEM] Sandbox connection dropped (Likely due to interrupt). Restarting container...{COLOR_RESET}")
            await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print(f"\n{COLOR_YELLOW}[SYSTEM] Hard interrupt detected. Resetting sandbox...{COLOR_RESET}")
            await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(run_chat())
    except KeyboardInterrupt:
        print(f"\n\033[91m[SYSTEM] Forced shutdown. Goodbye!\033[0m")