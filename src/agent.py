import os
import json
import asyncio
from typing import List, Dict, Any, Callable, Optional
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

from src.tools.file_tools import (
    file_tools_schema,
    change_workspace,
    read_file,
    write_file,
    edit_file
)

from src.tools.terminal_tools import (
    TERMINAL_TOOLS,
    new_terminal,
    execute_command,
    read_logs,
    close_terminal
)

# 1. Initialize OpenAI async client
api_key = os.environ.get("OPENAI_API_KEY")
base_url = os.environ.get("OPENAI_BASE_URL")
model = os.environ.get("OPENAI_MODEL", "gpt-4o")

# Create a dictionary with only the non-None values to avoid overriding defaults with None
client_kwargs = {}
if api_key:
    client_kwargs["api_key"] = api_key
if base_url:
    client_kwargs["base_url"] = base_url

client = AsyncOpenAI(**client_kwargs)

# 2. Maintain in-memory messages list
messages: List[Dict[str, Any]] = [
    {"role": "system", "content": "You are a helpful coding assistant."}
]

# 3. Combine schemas
tools = file_tools_schema + TERMINAL_TOOLS

# 4. Implement process_user_message
async def process_user_message(user_message: str, yield_callback: Optional[Callable] = None):
    messages.append({"role": "user", "content": user_message})

    responses = []

    while True:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools
        )

        message = response.choices[0].message

        # Append assistant message
        # Convert to dict to ensure compatibility when passing back
        msg_dict = message.model_dump(exclude_none=True)
        messages.append(msg_dict)

        # If the model returns text
        if message.content:
            if yield_callback:
                if asyncio.iscoroutinefunction(yield_callback):
                    await yield_callback(message.content)
                else:
                    yield_callback(message.content)
            else:
                responses.append(message.content)

        # If the model returns tool_calls
        if message.tool_calls:
            for tool_call in message.tool_calls:
                function_name = tool_call.function.name
                try:
                    kwargs = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    kwargs = {}

                # Execute the appropriate python functions
                try:
                    if function_name == "change_workspace":
                        result = change_workspace(**kwargs)
                    elif function_name == "read_file":
                        result = read_file(**kwargs)
                    elif function_name == "write_file":
                        result = write_file(**kwargs)
                    elif function_name == "edit_file":
                        result = edit_file(**kwargs)
                    elif function_name == "new_terminal":
                        result = await new_terminal(**kwargs)
                    elif function_name == "execute_command":
                        result = await execute_command(**kwargs)
                    elif function_name == "read_logs":
                        result = await read_logs(**kwargs)
                    elif function_name == "close_terminal":
                        result = await close_terminal(**kwargs)
                    else:
                        result = f"Error: Unknown tool {function_name}"
                except Exception as e:
                    result = f"Error executing {function_name}: {str(e)}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": str(result)
                })
        else:
            # Stop when the model returns a final text message without tool calls
            break

    if not yield_callback:
        return responses

class Agent:
    async def process_user_message(self, user_message, yield_callback=None):
        return await process_user_message(user_message, yield_callback)
