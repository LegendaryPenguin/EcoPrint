# message

import json
from enum import Enum
from typing import Dict, Any, Optional

class MessageType(Enum):
    NEW_TRANSACTION = 1
    NEW_BLOCK = 2
    GET_BLOCKS = 3 # Request blocks from a peer
    SEND_BLOCKS = 4 # Send blocks to a peer
    GET_PEERS = 5   # Request peer list
    SEND_PEERS = 6   # Send peer list
    ERROR = 7       # Send error message
    PING = 8
    PONG = 9

# Simple Message Protocol using JSON
# {"type": MessageType.value, "payload": {...}}

def create_message(msg_type: MessageType, payload: Optional[Dict[str, Any]] = None) -> str:
    """Creates a JSON message string."""
    message = {"type": msg_type.value}
    if payload is not None:
        message["payload"] = payload
    try:
        return json.dumps(message) + "\n" # Add newline as delimiter
    except TypeError as e:
         print(f"Error serializing message payload for type {msg_type}: {e}")
         # Send error message instead?
         return json.dumps({"type": MessageType.ERROR.value, "payload": {"error": "Serialization failed"}}) + "\n"


def parse_message(message_str: str) -> Optional[Dict[str, Any]]:
    """Parses a JSON message string."""
    try:
        # Handle potential multiple messages if buffer contained more than one
        message_str = message_str.strip()
        if not message_str:
             return None
        # Assume one message per call for simplicity now
        return json.loads(message_str)
    except json.JSONDecodeError:
        # print(f"Error decoding JSON message: {message_str}")
        return None
    except Exception as e:
        # print(f"Unexpected error parsing message: {e}")
        return None