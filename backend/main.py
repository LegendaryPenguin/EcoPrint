import time
import os
import argparse
import sys
import threading
import logging

from typing import Optional

from flask import Flask, request, jsonify

from node import Node
from blockchain.consensus import ProofOfWork
# Import utils to potentially access GOVT key for decryption endpoint
from blockchain import utils

# --- Configuration Defaults & Globals ---
DEFAULT_DIFFICULTY = 4
DEFAULT_BASE_P2P_PORT = 5000
DEFAULT_BASE_API_PORT = 5050
CHAIN_FILE_PREFIX = "chain_data_node_"
current_node: Optional[Node] = None
flask_app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
# logging.getLogger('werkzeug').setLevel(logging.ERROR) # Silence Flask logs if needed

# --- API Endpoints ---

@flask_app.route('/status', methods=['GET'])
def get_status():
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try: return jsonify(current_node.get_status()), 200
    except Exception as e: logging.error(f"API /status Error: {e}", exc_info=True); return jsonify({"error": "Server error"}), 500

@flask_app.route('/balance/<address_or_id>', methods=['GET'])
def get_address_balance(address_or_id: str):
    """Gets balance for a real address or an anonymous ID."""
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        # Basic length check, might need adjustment if anon IDs differ
        if not address_or_id or (len(address_or_id) != 64 and len(address_or_id) != 32): # Allow real addr or anon ID length
             return jsonify({"error": "Invalid address or ID format"}), 400
        balance = current_node.get_balance(address_or_id)
        return jsonify({"identifier": address_or_id, "balance": balance}), 200
    except Exception as e: logging.error(f"API /balance Error: {e}", exc_info=True); return jsonify({"error": "Server error"}), 500

@flask_app.route('/all-balances', methods=['GET'])
def get_all_node_balances():
    """Returns balances for all identifiers (addresses/anon IDs) found in the UTXO set."""
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try: return jsonify(current_node.get_all_balances()), 200
    except Exception as e: logging.error(f"API /all-balances Error: {e}", exc_info=True); return jsonify({"error": "Server error"}), 500

@flask_app.route('/create-wallet', methods=['POST'])
def api_create_wallet():
    """Generates a new wallet managed by the node."""
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        new_wallet = current_node.create_managed_wallet()
        logging.info(f"API: Created managed wallet: {new_wallet.get_address()}")
        return jsonify({
            "message": "Wallet created successfully (Managed by Node)",
            "address": new_wallet.get_address(), # Return REAL address
            "current_anonymous_id": new_wallet.get_anonymous_id() # Also return current anon ID
        }), 201
    except Exception as e: logging.error(f"API /create-wallet Error: {e}", exc_info=True); return jsonify({"error": "Server error"}), 500

@flask_app.route('/wallets', methods=['GET'])
def api_list_managed_wallets():
    """Lists REAL addresses of wallets managed by this node."""
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try: return jsonify({"managed_wallets": current_node.get_all_managed_wallet_addresses()}), 200
    except Exception as e: logging.error(f"API /wallets Error: {e}", exc_info=True); return jsonify({"error": "Server error"}), 500

@flask_app.route('/create-transaction', methods=['POST'])
def api_create_transaction():
    """
    Creates and submits a transaction using a wallet MANAGED BY THIS NODE.
    Can optionally use anonymous sending (default).
    Expects JSON: {
        "sender": "<sender_REAL_address>",
        "recipient": "<recipient_REAL_address_OR_ANON_ID>",
        "amount": <float>,
        "fee": <float>,
        "use_anonymity": <bool> (optional, default: true)
    }
    """
    if not current_node: return jsonify({"error": "Node offline"}), 503
    data = request.get_json(silent=True);
    if not data: return jsonify({"error": "Invalid JSON"}), 400
    logging.debug(f"API /create-transaction received: {data}")

    # --- Input Validation ---
    sender_addr = data.get("sender"); recipient_id = data.get("recipient")
    amount_str = data.get("amount"); fee_str = data.get("fee")
    use_anon = data.get("use_anonymity", True) # Default to using anonymity

    if not sender_addr or not isinstance(sender_addr, str) or len(sender_addr) != 64: return jsonify({"error": "Invalid 'sender' (must be real address)"}), 400
    # Recipient can be real address (64) or anon ID (e.g., 32)
    if not recipient_id or not isinstance(recipient_id, str) or not (len(recipient_id) == 64 or len(recipient_id) == 32): return jsonify({"error": "Invalid 'recipient' (address or anon_id)"}), 400
    try: amount = float(amount_str); assert amount > 0
    except: return jsonify({"error": "Invalid 'amount'"}), 400
    try: fee = float(fee_str); assert fee >= 0
    except: return jsonify({"error": "Invalid 'fee'"}), 400
    if not isinstance(use_anon, bool): return jsonify({"error": "Invalid 'use_anonymity' (must be true/false)"}), 400
    # --- End Validation ---

    logging.info(f"API: Tx request: {amount} from managed {sender_addr[:10]} -> {recipient_id[:10]} (Anon: {use_anon}, Fee: {fee})")
    try:
        # Use node method which finds wallet by REAL address and creates tx
        tx = current_node.create_transaction_from_managed_wallet(
            sender_addr, recipient_id, amount, fee, use_anonymity=use_anon
        )
        if not tx:
             if not current_node.get_managed_wallet(sender_addr): return jsonify({"error": f"Sender address '{sender_addr}' not managed."}), 400
             else: return jsonify({"error": "Tx creation failed (likely insufficient funds)."}), 400

        submitted = current_node.submit_and_broadcast_transaction(tx)
        if submitted: return jsonify({"message": "Transaction created and broadcast", "transaction_id": tx.transaction_id}), 202
        else: return jsonify({"error": "Transaction rejected by mempool"}), 400

    except Exception as e: logging.error(f"API /create-transaction Error: {e}", exc_info=True); return jsonify({"error": "Server error"}), 500

# --- Revocation Endpoint (Conceptual) ---
@flask_app.route('/reveal-sender', methods=['POST'])
def api_reveal_sender():
     """
     Attempts to decrypt a revocation token from a transaction input.
     Expects JSON: {"revocation_token": "<base64_string>"}
     NOTE: In reality, the node itself wouldn't hold the govt key.
     """
     if not current_node: return jsonify({"error": "Node offline"}), 503
     data = request.get_json(silent=True)
     if not data or "revocation_token" not in data: return jsonify({"error": "Missing 'revocation_token'"}), 400

     token = data["revocation_token"]
     try:
          revealed_address = current_node.decrypt_revocation_token(token)
          if revealed_address:
               logging.info(f"API: Revocation successful for token, revealed: {revealed_address}")
               return jsonify({"revealed_sender_address": revealed_address}), 200
          else:
               logging.warning(f"API: Revocation failed for token.")
               return jsonify({"error": "Decryption failed (invalid token or key)"}), 400
     except Exception as e:
          logging.error(f"API /reveal-sender Error: {e}", exc_info=True)
          return jsonify({"error": "Server error during decryption"}), 500


# --- Flask Runner & Main Node Start --- (Mostly unchanged) ---
def run_flask_app(host: str, port: int):
    try: logging.info(f"Starting Flask API on http://{host}:{port}"); flask_app.run(host=host, port=port, debug=False, use_reloader=False)
    except Exception as e: logging.error(f"!!! Flask API Error on port {port}: {e}", exc_info=True)

def start_node_process(my_index: int, all_ips: list[str], base_p2p_port: int, base_api_port: int, difficulty: int):
    global current_node
    # ... (Determine ports, node_id, chain_file, bootstrap_peers as before) ...
    my_ip = all_ips[my_index]; listen_host = "0.0.0.0"
    p2p_port = base_p2p_port + my_index; api_port = base_api_port + my_index
    node_id = f"Node-{my_index+1}_{my_ip}_P2P:{p2p_port}_API:{api_port}"
    chain_file_base = CHAIN_FILE_PREFIX
    bootstrap_peers = [(ip, base_p2p_port + i) for i, ip in enumerate(all_ips) if i != my_index]

    logging.info(f"--- Initializing {node_id} ---")
    logging.info(f"  P2P Listen: {listen_host}:{p2p_port}, API Listen: {listen_host}:{api_port}")
    # ... (rest of logging, file removal) ...

    consensus = ProofOfWork(difficulty=difficulty)
    node = Node(host=listen_host, port=p2p_port, node_id=node_id, consensus=consensus, bootstrap_peers=bootstrap_peers, chain_file_base=chain_file_base)
    current_node = node

    api_thread = threading.Thread(target=run_flask_app, args=(listen_host, api_port), daemon=True); api_thread.start()

    try:
        node.start(); time.sleep(3); node.start_mining()
        logging.info(f"\n--- {node.id} Running (Ctrl+C to stop) ---")
        while True: time.sleep(60)
    except KeyboardInterrupt: logging.info(f"\n--- Stopping {node.id} ---")
    finally:
        if current_node: current_node.stop()
        logging.info(f"--- {node.id} Stopped ---")

# --- Argparse and Main Execution --- (Unchanged) ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a P2P Blockchain Node with API.")
    parser.add_argument("--index", type=int, required=True, help="Index (0, 1, 2...)")
    parser.add_argument("--ips", nargs='+', required=True, help="List of ALL node IPs/hostnames")
    parser.add_argument("-d", "--difficulty", type=int, default=DEFAULT_DIFFICULTY, help=f"PoW difficulty (default: {DEFAULT_DIFFICULTY})")
    parser.add_argument("--p2p-port", type=int, default=DEFAULT_BASE_P2P_PORT, help=f"Base P2P port (default: {DEFAULT_BASE_P2P_PORT})")
    parser.add_argument("--api-port", type=int, default=DEFAULT_BASE_API_PORT, help=f"Base API port (default: {DEFAULT_BASE_API_PORT})")
    args = parser.parse_args()
    cleaned_ips = [ip.split(':')[0] for ip in args.ips];
    if args.index >= len(cleaned_ips): print(f"Error: Index {args.index} out of bounds"); sys.exit(1)
    start_node_process(my_index=args.index, all_ips=cleaned_ips, base_p2p_port=args.p2p_port, base_api_port=args.api_port, difficulty=args.difficulty)
