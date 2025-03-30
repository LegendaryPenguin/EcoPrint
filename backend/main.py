# main.py
import time
import os
import argparse
import sys
import threading
import logging
from typing import Optional

from flask import Flask, request, jsonify
from flask_cors import CORS # Import CORS

# Node and Blockchain components
# Ensure Block is implicitly imported via Node or add explicitly if needed elsewhere
from node import Node, SUPPORTED_MARKETS # Import node and supported markets
from blockchain.consensus import ProofOfWork
from blockchain.order import Order # Import Order
from blockchain.block import Block # Explicitly import Block if needed by endpoints
from blockchain import utils

# --- Configuration Defaults & Globals ---
DEFAULT_DIFFICULTY = 4
DEFAULT_BASE_P2P_PORT = 5000
DEFAULT_BASE_API_PORT = 5050
CHAIN_FILE_PREFIX = "chain_data_node_"
# Match initial balances from node.py
INITIAL_USD_BALANCE = 1000.0
INITIAL_CARBON_BALANCE = 500.0

# --- Setup logging FIRST ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
# Optionally silence excessive Flask request logs
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# --- Initialize Flask App and CORS ---
flask_app = Flask(__name__)
# Apply CORS to the Flask app, specifically allowing your frontend origin
CORS(flask_app, resources={r"/*": {"origins": ["http://127.0.0.1:5500", "http://localhost:5500", "null"]}})
# Added localhost and "null" for flexibility during development

current_node: Optional[Node] = None

# --- API Endpoints ---

# --- Status & General Info ---
@flask_app.route('/status', methods=['GET'])
def get_status():
    """Returns the overall status of the node."""
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        status_data = current_node.get_status()
        status_data["supported_markets"] = SUPPORTED_MARKETS # Add supported markets
        status_data["pinata_enabled"] = current_node.pinata is not None # Indicate if Pinata is active
        return jsonify(status_data), 200
    except Exception as e:
        logging.error(f"API /status Error: {e}", exc_info=True)
        return jsonify({"error": "Server error"}), 500

@flask_app.route('/latest-block-info', methods=['GET'])
def get_latest_block_info():
    """Returns info about the latest block, including Merkle root and Pinata CID."""
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        with current_node.chain_lock:
            last_block = current_node.chain.get_last_block()
        if last_block:
            info = last_block.to_dict()
            # Remove transactions list as it can be large
            info.pop('transactions', None)
            # Ensure pinata_cid is included (it's added in node.py's Block.to_dict)
            # If pinata_cid might be missing in older blocks, handle gracefully:
            info['pinata_cid'] = getattr(last_block, 'pinata_cid', None)
            return jsonify(info), 200
        else:
            return jsonify({"error": "Chain is empty or only contains genesis block"}), 404
    except Exception as e:
        logging.error(f"API /latest-block-info Error: {e}", exc_info=True)
        return jsonify({"error": "Server error"}), 500

# NEW Endpoint to get specific block details by index or hash
@flask_app.route('/block/<identifier>', methods=['GET'])
def get_block_info(identifier: str):
    """Returns full info for a specific block by index or hash."""
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        block_to_return = None
        with current_node.chain_lock:
            try:
                 # Try interpreting as index first
                 block_index = int(identifier)
                 if 0 <= block_index < len(current_node.chain.blocks):
                     block_to_return = current_node.chain.blocks[block_index]
            except ValueError:
                 # If not an int, try searching by hash
                 for block in reversed(current_node.chain.blocks): # Search recent first
                     if block.hash == identifier:
                         block_to_return = block
                         break

        if block_to_return:
            info = block_to_return.to_dict()
            # Optionally remove full transactions list for summary endpoints
            # info.pop('transactions', None)
            info['pinata_cid'] = getattr(block_to_return, 'pinata_cid', None)
            return jsonify(info), 200
        else:
            return jsonify({"error": f"Block with identifier '{identifier}' not found"}), 404
    except Exception as e:
        logging.error(f"API /block/{identifier} Error: {e}", exc_info=True)
        return jsonify({"error": "Server error"}), 500


# --- Wallet Management ---
@flask_app.route('/create-wallet', methods=['POST'])
def api_create_wallet():
    """Generates a new wallet managed by the node."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        new_wallet = current_node.create_managed_wallet()
        address = new_wallet.get_address()
        usd_balance = current_node.get_usd_balance(address)
        carbon_balance = current_node.get_carbon_balance(address)
        anon_id = new_wallet.get_anonymous_id() if hasattr(new_wallet, 'get_anonymous_id') else None
        logging.info(f"API: Created managed wallet: {address[:10]}")
        response = {
            "message": "Wallet created successfully", "address": address,
            "initial_usd_balance": usd_balance, "initial_carbon_balance": carbon_balance,
        }
        if anon_id: response["current_anonymous_id"] = anon_id
        return jsonify(response), 201
    except Exception as e:
        logging.error(f"API /create-wallet Error: {e}", exc_info=True)
        return jsonify({"error": "Server error"}), 500

@flask_app.route('/wallets', methods=['GET'])
def api_list_managed_wallets():
    """Lists REAL addresses of wallets managed by this node."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        addresses = current_node.get_all_managed_wallet_addresses()
        return jsonify({"managed_wallets": addresses}), 200
    except Exception as e:
        logging.error(f"API /wallets Error: {e}", exc_info=True)
        return jsonify({"error": "Server error"}), 500

# --- Balance Queries ---
@flask_app.route('/native-balance/<address_or_id>', methods=['GET'])
def get_address_native_balance(address_or_id: str):
    """Gets native coin balance for an address or ID."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        if not address_or_id or not isinstance(address_or_id, str) or not (len(address_or_id) == 64 or len(address_or_id) == 32):
             return jsonify({"error": "Invalid address or ID format"}), 400
        balance = current_node.get_native_balance(address_or_id)
        return jsonify({"identifier": address_or_id, "native_balance": balance}), 200
    except Exception as e:
        logging.error(f"API /native-balance Error: {e}", exc_info=True)
        return jsonify({"error": "Server error"}), 500

@flask_app.route('/all-native-balances', methods=['GET'])
def get_all_node_native_balances():
    """Gets all known native coin balances."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        balances = current_node.get_all_native_balances()
        return jsonify(balances), 200
    except Exception as e:
        logging.error(f"API /all-native-balances Error: {e}", exc_info=True)
        return jsonify({"error": "Server error"}), 500

@flask_app.route('/usd-balance/<address>', methods=['GET'])
def get_address_usd_balance(address: str):
    """Gets off-chain USD balance."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        if not address or not isinstance(address, str) or len(address) != 64:
            return jsonify({"error": "Invalid address format"}), 400
        balance = current_node.get_usd_balance(address)
        return jsonify({"address": address, "usd_balance": balance}), 200
    except Exception as e:
        logging.error(f"API /usd-balance Error: {e}", exc_info=True)
        return jsonify({"error": "Server error"}), 500

@flask_app.route('/carbon-balance/<address>', methods=['GET'])
def get_address_carbon_balance(address: str):
    """Gets off-chain Carbon balance."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        if not address or not isinstance(address, str) or len(address) != 64:
            return jsonify({"error": "Invalid address format"}), 400
        balance = current_node.get_carbon_balance(address)
        return jsonify({"address": address, "carbon_balance": balance}), 200
    except Exception as e:
        logging.error(f"API /carbon-balance Error: {e}", exc_info=True)
        return jsonify({"error": "Server error"}), 500

@flask_app.route('/all-usd-balances', methods=['GET'])
def get_all_node_usd_balances():
    """Gets all off-chain USD balances."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        balances = current_node.get_all_usd_balances()
        return jsonify(balances), 200
    except Exception as e:
        logging.error(f"API /all-usd-balances Error: {e}", exc_info=True)
        return jsonify({"error": "Server error"}), 500

@flask_app.route('/all-carbon-balances', methods=['GET'])
def get_all_node_carbon_balances():
    """Gets all off-chain Carbon balances."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    try:
        balances = current_node.get_all_carbon_balances()
        return jsonify(balances), 200
    except Exception as e:
        logging.error(f"API /all-carbon-balances Error: {e}", exc_info=True)
        return jsonify({"error": "Server error"}), 500

# --- Native Coin Transaction ---
@flask_app.route('/create-transaction', methods=['POST'])
def api_create_native_transaction():
    """Creates and submits a NATIVE COIN transaction."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    data = request.get_json(silent=True)
    if not data: return jsonify({"error": "Invalid JSON"}), 400
    sender_addr=data.get("sender"); recipient_id=data.get("recipient"); amount_str=data.get("amount"); fee_str=data.get("fee"); use_anon=data.get("use_anonymity", False)
    if not sender_addr or len(sender_addr)!= 64: return jsonify({"error": "Invalid 'sender'"}), 400
    if not recipient_id or not (len(recipient_id) == 64 or len(recipient_id) == 32): return jsonify({"error": "Invalid 'recipient'"}), 400
    try: amount = float(amount_str); assert amount > 0
    except: return jsonify({"error": "Invalid 'amount'"}), 400
    try: fee = float(fee_str); assert fee >= 0
    except: return jsonify({"error": "Invalid 'fee'"}), 400
    if not isinstance(use_anon, bool): return jsonify({"error": "Invalid 'use_anonymity'"}), 400
    logging.info(f"API: Native Tx request: {amount:.8f} from {sender_addr[:10]} -> {recipient_id[:10]} (Anon: {use_anon}, Fee: {fee:.8f})")
    try:
        tx = current_node.create_transaction_from_managed_wallet(sender_addr, recipient_id, amount, fee, use_anonymity=use_anon)
        if not tx:
            if not current_node.get_managed_wallet(sender_addr): return jsonify({"error": f"Sender '{sender_addr[:10]}' not managed."}), 400
            else: return jsonify({"error": "Native Tx creation failed (funds?)."}), 400
        submitted = current_node.submit_and_broadcast_transaction(tx)
        if submitted: return jsonify({"message": "Native tx created & broadcast", "transaction_id": tx.transaction_id}), 202
        else: return jsonify({"error": "Native tx rejected by mempool"}), 400
    except Exception as e: logging.error(f"API /create-transaction Error: {e}", exc_info=True); return jsonify({"error": "Server error"}), 500

# --- Off-Chain USD Transfer ---
@flask_app.route('/transfer-usd', methods=['POST'])
def api_transfer_usd():
    """Transfers OFF-CHAIN USD between managed wallets."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    data = request.get_json(silent=True)
    if not data: return jsonify({"error": "Invalid JSON"}), 400
    sender_addr=data.get("sender"); recipient_addr=data.get("recipient"); amount_str = data.get("amount")
    if not sender_addr or len(sender_addr)!= 64: return jsonify({"error": "Invalid 'sender'"}), 400
    if not recipient_addr or len(recipient_addr)!= 64: return jsonify({"error": "Invalid 'recipient'"}), 400
    if sender_addr == recipient_addr: return jsonify({"error": "Sender and recipient same"}), 400
    try: amount = float(amount_str); assert amount > 0
    except: return jsonify({"error": "Invalid 'amount'"}), 400
    logging.info(f"API: USD Transfer req: {amount:.8f} from {sender_addr[:10]} -> {recipient_addr[:10]}")
    try:
        if not current_node.get_managed_wallet(sender_addr): return jsonify({"error": f"Sender '{sender_addr[:10]}' not managed."}), 400
        if not current_node.get_managed_wallet(recipient_addr): return jsonify({"error": f"Recipient '{recipient_addr[:10]}' not managed."}), 400
        success = current_node.transfer_usd_off_chain(sender_addr, recipient_addr, amount)
        if success: return jsonify({"message": "USD transfer successful (off-chain)"}), 200
        else: return jsonify({"error": "USD transfer failed (funds?)."}), 400
    except Exception as e: logging.error(f"API /transfer-usd Error: {e}", exc_info=True); return jsonify({"error": "Server error"}), 500

# --- Order Book API ---
@flask_app.route('/orderbook/<market_pair>', methods=['GET'])
def api_get_orderbook(market_pair: str):
    """Gets the order book snapshot for a specific market pair."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    market_pair_upper = market_pair.upper()
    if market_pair_upper not in SUPPORTED_MARKETS: return jsonify({"error": f"Unsupported market: {market_pair}"}), 404
    try:
        snapshot = current_node.get_order_book_snapshot(market_pair_upper)
        if snapshot is None: return jsonify({"error": "Market data error"}), 500
        return jsonify(snapshot), 200
    except Exception as e: logging.error(f"API /orderbook Error: {e}", exc_info=True); return jsonify({"error": "Server error"}), 500

@flask_app.route('/place-order', methods=['POST'])
def api_place_order():
    """Places a new buy or sell order."""
    # (No changes needed from previous version)
    if not current_node: return jsonify({"error": "Node offline"}), 503
    data = request.get_json(silent=True);
    if not data: return jsonify({"error": "Invalid JSON"}), 400
    wallet_addr = data.get("wallet_address"); market_pair_in = data.get("market_pair", ""); order_type = data.get("type"); amount_str = data.get("amount"); price_str = data.get("price")
    if not wallet_addr or len(wallet_addr)!= 64: return jsonify({"error": "Invalid 'wallet_address'"}), 400
    market_pair = market_pair_in.upper()
    if market_pair not in SUPPORTED_MARKETS: return jsonify({"error": f"Invalid 'market_pair'. Supported: {SUPPORTED_MARKETS}"}), 400
    if order_type not in ['buy', 'sell']: return jsonify({"error": "Invalid 'type'"}), 400
    try: amount = float(amount_str); assert amount > 0
    except: return jsonify({"error": "Invalid 'amount'"}), 400
    try: price = float(price_str); assert price > 0
    except: return jsonify({"error": "Invalid 'price'"}), 400
    try:
        if not current_node.get_managed_wallet(wallet_addr): return jsonify({"error": f"Wallet '{wallet_addr[:10]}' not managed."}), 400
        order_data = {"wallet_address": wallet_addr, "market_pair": market_pair, "order_type": order_type, "amount": amount, "price": price}
        order = Order.from_dict(order_data)
        success = current_node.add_order(order)
        if success: return jsonify({"message": "Order placed", "order": order.to_dict()}), 201
        else: return jsonify({"error": "Failed to place order (rejected by node?)"}), 400
    except ValueError as e: return jsonify({"error": f"Invalid order data: {e}"}), 400
    except Exception as e: logging.error(f"API /place-order Error: {e}", exc_info=True); return jsonify({"error": "Server error"}), 500

# --- Revocation Endpoint ---
@flask_app.route('/reveal-sender', methods=['POST'])
def api_reveal_sender():
     """Attempts to decrypt a revocation token."""
     # (No changes needed from previous version)
     if not current_node: return jsonify({"error": "Node offline"}), 503
     data = request.get_json(silent=True)
     if not data or "revocation_token" not in data or not isinstance(data["revocation_token"], str): return jsonify({"error": "Missing or invalid 'revocation_token'"}), 400
     token = data["revocation_token"]
     if not token: return jsonify({"error": "Empty 'revocation_token'"}), 400
     logging.info(f"API: Attempting revocation for token: {token[:15]}...")
     try:
          revealed_address = current_node.decrypt_revocation_token(token)
          if revealed_address: return jsonify({"revealed_sender_address": revealed_address}), 200
          else: return jsonify({"error": "Decryption failed (invalid token or key)"}), 400
     except Exception as e: logging.error(f"API /reveal-sender Error: {e}", exc_info=True); return jsonify({"error": "Server error"}), 500


# --- Flask Runner & Main Node Start ---
def run_flask_app(host: str, port: int):
    """Runs the Flask API server in a separate thread."""
    # (No changes needed from previous version)
    try:
        logging.info(f"Starting Flask API server on http://{host}:{port} with CORS enabled")
        flask_app.run(host=host, port=port, debug=False, use_reloader=False)
    except OSError as e:
         logging.error(f"!!! Flask API Error: Could not bind to {host}:{port}. Port likely in use. ({e})")
    except Exception as e:
        logging.error(f"!!! Flask API Unhandled Error on port {port}: {e}", exc_info=True)

def start_node_process(my_index: int, all_ips: list[str], base_p2p_port: int, base_api_port: int, difficulty: int):
    """Initializes and starts the blockchain node, P2P, API, and mining."""
    # (No changes needed from previous version)
    global current_node
    if my_index < 0 or my_index >= len(all_ips): logging.error(f"Index {my_index} out of bounds."); sys.exit(1)
    my_ip = all_ips[my_index]; listen_host = "0.0.0.0"
    p2p_port = base_p2p_port + my_index; api_port = base_api_port + my_index
    node_id = f"Node-{my_index+1}_{my_ip}_P2P:{p2p_port}_API:{api_port}"
    chain_file_base = CHAIN_FILE_PREFIX
    bootstrap_peers = [(ip, base_p2p_port + i) for i, ip in enumerate(all_ips) if i != my_index]
    logging.info(f"--- Initializing {node_id} ---"); logging.info(f"  Index: {my_index}"); logging.info(f"  P2P: {listen_host}:{p2p_port}"); logging.info(f"  API: {listen_host}:{api_port}"); logging.info(f"  Difficulty: {difficulty}"); logging.info(f"  Bootstrap: {bootstrap_peers}")
    consensus_mechanism = ProofOfWork(difficulty=difficulty)
    node_instance = Node(host=listen_host, port=p2p_port, node_id=node_id, consensus=consensus_mechanism, bootstrap_peers=bootstrap_peers, chain_file_base=chain_file_base)
    current_node = node_instance
    api_thread = threading.Thread(target=run_flask_app, args=(listen_host, api_port), name=f"FlaskAPI-{node_id}", daemon=True); api_thread.start()
    try:
        current_node.start(); logging.info(f"Node P2P and Order Matching started.")
        time.sleep(5)
        current_node.start_mining(); logging.info(f"Node Mining started.")
        logging.info(f"\n--- {current_node.id} is RUNNING (Ctrl+C to stop) ---")
        while True: time.sleep(60)
    except KeyboardInterrupt: logging.info(f"\n--- Ctrl+C detected. Stopping {current_node.id} ---")
    except Exception as e: logging.error(f"--- UNHANDLED EXCEPTION in main loop for {node_id}: {e} ---", exc_info=True)
    finally:
        logging.info(f"Initiating node shutdown...")
        if current_node: current_node.stop()
        logging.info(f"--- {node_id} Stopped ---")

# --- Argparse and Main Execution ---
if __name__ == "__main__":
    # (No changes needed from previous version)
    parser = argparse.ArgumentParser(description="Run a P2P Blockchain Node with API and Order Book.")
    parser.add_argument("--index", type=int, required=True, help="Node index (0, 1, 2...).")
    parser.add_argument("--ips", nargs='+', required=True, help="List of ALL node IPs/hostnames.")
    parser.add_argument("-d", "--difficulty", type=int, default=DEFAULT_DIFFICULTY, help=f"PoW difficulty (default: {DEFAULT_DIFFICULTY})")
    parser.add_argument("--p2p-port", type=int, default=DEFAULT_BASE_P2P_PORT, help=f"Base P2P port (default: {DEFAULT_BASE_P2P_PORT})")
    parser.add_argument("--api-port", type=int, default=DEFAULT_BASE_API_PORT, help=f"Base API port (default: {DEFAULT_BASE_API_PORT})")
    args = parser.parse_args()
    cleaned_ips = [ip.split(':')[0] for ip in args.ips]
    if args.index < 0 or args.index >= len(cleaned_ips): print(f"Error: Index {args.index} out of bounds."); sys.exit(1)
    if args.difficulty <= 0: print(f"Error: Difficulty must be positive."); sys.exit(1)
    if args.p2p_port <= 1024 or args.api_port <= 1024: print(f"Warning: Using privileged ports (<=1024).");
    if args.p2p_port > 65535 or args.api_port > 65535: print(f"Error: Ports must be <= 65535."); sys.exit(1)
    start_node_process(my_index=args.index, all_ips=cleaned_ips, base_p2p_port=args.p2p_port, base_api_port=args.api_port, difficulty=args.difficulty)