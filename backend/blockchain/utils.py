import hashlib
import json
import time
import os # For random bytes
from ecdsa import SigningKey, VerifyingKey, SECP256k1, BadSignatureError
from typing import List, Any, Tuple, Dict, Optional
# Import encryption library
from cryptography.fernet import Fernet
import base64 # To handle encrypted bytes

# --- Hashing (Unchanged) ---
def calculate_block_hash(index: int, timestamp: float, previous_hash: str, merkle_root: str, nonce: int) -> str:
    header_data = f"{index}{timestamp}{previous_hash}{merkle_root}{nonce}"
    return hashlib.sha256(header_data.encode('utf-8')).hexdigest()

def calculate_tx_hash(tx_inputs_refs: List[Dict], tx_outputs_data: List[Dict]) -> str:
     tx_data = {"inputs": tx_inputs_refs, "outputs": tx_outputs_data}
     tx_string = json.dumps(tx_data, sort_keys=True).encode('utf-8')
     return hashlib.sha256(tx_string).hexdigest()

# --- ECDSA (Unchanged) ---
def generate_key_pair() -> Tuple[str, str]:
    sk = SigningKey.generate(curve=SECP256k1)
    vk = sk.verifying_key
    return sk.to_string().hex(), vk.to_string().hex()

def sign(private_key_hex: str, message: str) -> str:
    sk = SigningKey.from_string(bytes.fromhex(private_key_hex), curve=SECP256k1)
    message_hash = hashlib.sha256(message.encode('utf-8')).digest()
    signature = sk.sign_digest(message_hash)
    return signature.hex()

def verify(public_key_hex: str, message: str, signature_hex: str) -> bool:
    try:
        vk = VerifyingKey.from_string(bytes.fromhex(public_key_hex), curve=SECP256k1)
        message_hash = hashlib.sha256(message.encode('utf-8')).digest()
        return vk.verify_digest(bytes.fromhex(signature_hex), message_hash)
    except (BadSignatureError, ValueError, Exception): # Catch more errors
        return False

def get_public_key_from_private(private_key_hex: str) -> str:
     """Derives the public key hex from a private key hex."""
     sk = SigningKey.from_string(bytes.fromhex(private_key_hex), curve=SECP256k1)
     vk = sk.verifying_key
     return vk.to_string().hex()

# --- Address (Unchanged conceptually, but might represent anonymous ID later) ---
def public_key_to_address(public_key_hex: str) -> str:
    # TODO: Implement proper address encoding (e.g., Base58Check)
    public_key_bytes = bytes.fromhex(public_key_hex)
    return hashlib.sha256(public_key_bytes).hexdigest()

# --- Merkle Tree (Unchanged) ---
def calculate_merkle_root(transaction_ids: List[str]) -> str:
    if not transaction_ids: return hashlib.sha256(b"").hexdigest()
    current_level = list(transaction_ids)
    while len(current_level) > 1:
         if len(current_level) % 2 != 0: current_level.append(current_level[-1])
         next_level = []
         for i in range(0, len(current_level), 2):
              combined_data = (current_level[i] + current_level[i+1]).encode('utf-8')
              combined_hash = hashlib.sha256(combined_data).hexdigest()
              next_level.append(combined_hash)
         current_level = next_level
    return current_level[0]

# --- Placeholder Symmetric Encryption for Revocation Token ---
# WARNING: Using symmetric encryption here requires the 'govt_key' to be shared
# with wallets to *create* the token. A real group signature uses asymmetric crypto
# where wallets only need the govt *public* key. This is just a conceptual placeholder.

def generate_symmetric_key() -> bytes:
    """Generates a key suitable for Fernet symmetric encryption."""
    return Fernet.generate_key()

def encrypt_data(key: bytes, data: str) -> str:
    """Encrypts string data using a symmetric key, returns base64 encoded string."""
    f = Fernet(key)
    encrypted_bytes = f.encrypt(data.encode('utf-8'))
    return base64.urlsafe_b64encode(encrypted_bytes).decode('utf-8')

def decrypt_data(key: bytes, encrypted_data_b64: str) -> Optional[str]:
    """Decrypts base64 encoded symmetric encrypted data."""
    try:
        f = Fernet(key)
        encrypted_bytes = base64.urlsafe_b64decode(encrypted_data_b64.encode('utf-8'))
        decrypted_bytes = f.decrypt(encrypted_bytes)
        return decrypted_bytes.decode('utf-8')
    except Exception as e:
        # print(f"Decryption failed: {e}") # Debug only
        return None

# --- Placeholder Government Key ---
# Generate a key once and store it, or load from config/env variable
# IMPORTANT: In this SIMPLIFIED symmetric model, this key must ALSO be known by
# the wallet creating the transaction, which breaks real-world security models.
# For demonstration, we generate it here.
GOVT_SYMMETRIC_KEY = generate_symmetric_key()
print(f"!!! WARNING: Using DEMO symmetric key for revocation: {GOVT_SYMMETRIC_KEY.decode()} - DO NOT USE IN PRODUCTION !!!")

# --- Placeholder Anonymous ID Generation ---
def generate_anonymous_id(real_address: str) -> str:
     """Generates a simple pseudo-anonymous ID. NOT SECURE ANONYMITY."""
     # Simplistic example: Hash address with some randomness (timestamp isn't good)
     # Using a fixed salt + hash provides linkability, just obscures address slightly.
     # Real anonymity requires different cryptographic techniques.
     nonce = os.urandom(8).hex() # Add some randomness
     return hashlib.sha256((real_address + nonce + "ANON_SALT").encode()).hexdigest()[:32] # Truncate for brevity
