import os
import time
import threading
import random
import logging
from typing import Optional, List, Dict, Any

# Blockchain components
from blockchain.wallet import Wallet
from blockchain.consensus import Consensus
from blockchain.chain import Chain
from blockchain.utxo import UTXOSet
from blockchain.mempool import Mempool
from blockchain.block import Block
from blockchain.transaction import Transaction
from blockchain import miner
from blockchain import utils

# Networking components
from network.p2p import P2PNode
from network.message import MessageType, create_message, parse_message

# Constants
CHAIN_FILE_PREFIX = "chain_data_node_"

class Node:
    # ... (__init__ needs chain_file_base, _create_or_load_node_wallet, wallet management methods remain similar) ...
    def __init__(self, host: str, port: int, node_id: str, consensus: Consensus, bootstrap_peers: List[tuple[str, int]] = [], chain_file_base: str = CHAIN_FILE_PREFIX):
        self.id = node_id
        self.consensus = consensus
        self.mempool = Mempool()
        self.chain_file = f"{chain_file_base}{self.id}.json"

        loaded_chain = Chain.load_from_file(self.chain_file, self.consensus) if os.path.exists(self.chain_file) else None
        self.chain = loaded_chain if loaded_chain else Chain(self.consensus)
        self.chain_lock = threading.Lock()

        self.utxo_set = UTXOSet()
        with self.chain_lock: self.utxo_set.rebuild(self.chain)

        self.managed_wallets: Dict[str, Wallet] = {}
        self.node_wallet = self._create_or_load_node_wallet()

        self.p2p_node = P2PNode(host, port, self.id, self._handle_network_message)
        self.bootstrap_peers = bootstrap_peers

        self.is_mining = False; self.mining_thread = None; self.stop_mining_flag = threading.Event()
        logging.info(f"Node {self.id} initialized. Node Wallet: {self.node_wallet.get_address()[:10]} Chain: {len(self.chain.blocks)} UTXOs: {len(self.utxo_set)}")

    def _create_or_load_node_wallet(self) -> Wallet:
        # TODO: Load/Save primary key
        logging.info(f"Node {self.id}: Generating primary node wallet...")
        wallet = Wallet(); self.managed_wallets[wallet.get_address()] = wallet
        logging.info(f"Node {self.id}: Primary Wallet Address: {wallet.get_address()}")
        return wallet

    def create_managed_wallet(self) -> Wallet:
        wallet = Wallet()
        with self.chain_lock: self.managed_wallets[wallet.get_address()] = wallet
        logging.info(f"Node {self.id}: Created managed wallet: {wallet.get_address()}")
        return wallet

    def get_managed_wallet(self, address: str) -> Optional[Wallet]:
        with self.chain_lock: return self.managed_wallets.get(address)

    def get_all_managed_wallet_addresses(self) -> List[str]:
        with self.chain_lock: return list(self.managed_wallets.keys())

    # --- Mining (remains mostly the same, uses self.node_wallet.get_address()) ---
    def _mining_loop(self):
        # ... (same as before) ...
        logging.info(f"Node {self.id}: Mining thread started (Reward Addr: {self.node_wallet.get_address()[:10]}...).")
        while not self.stop_mining_flag.is_set():
            # ... (acquire locks, get data, call miner.mine_new_block) ...
            with self.chain_lock:
                 current_utxo_copy = self.utxo_set.get_copy(); last_block_copy = self.chain.get_last_block()
            if not last_block_copy: time.sleep(5); continue
            new_block = miner.mine_new_block(self.mempool, current_utxo_copy, self.chain, self.node_wallet.get_address(), self.consensus)
            if self.stop_mining_flag.is_set(): break
            if new_block:
                block_added = False
                with self.chain_lock: block_added = self.chain.add_block(new_block, self.utxo_set)
                if block_added:
                    logging.info(f"Node {self.id}: Added mined block {new_block.index} (Tx:{len(new_block.transactions)}, Hash:{new_block.hash[:10]})")
                    # ... (broadcast, remove from mempool) ...
                    block_msg = create_message(MessageType.NEW_BLOCK, payload=new_block.to_dict()); self.p2p_node.broadcast(block_msg)
                    mined_tx_ids = [tx.transaction_id for tx in new_block.transactions if not tx.is_coinbase()]; self.mempool.remove_transactions(mined_tx_ids)
                # else: logging.warning(...)
            else: # Wait if no block mined
                if not self.stop_mining_flag.wait(random.uniform(2.0, 4.0)): continue
                else: break
        logging.info(f"Node {self.id}: Mining thread finished.")

    # ... (start_mining, stop_mining remain the same) ...
    def start_mining(self): # Keep as before
        if self.is_mining: return
        self.is_mining = True; self.stop_mining_flag.clear()
        self.mining_thread = threading.Thread(target=self._mining_loop, daemon=True); self.mining_thread.start()
        logging.info(f"Node {self.id}: Started mining.")

    def stop_mining(self): # Keep as before
        if not self.is_mining or not self.mining_thread: return
        logging.info(f"Node {self.id}: Signaling mining thread to stop..."); self.stop_mining_flag.set()
        self.mining_thread.join(timeout=2.0)
        if self.mining_thread.is_alive(): logging.warning(f"Node {self.id}: Mining thread join timed out.")
        self.is_mining = False; self.mining_thread = None; logging.info(f"Node {self.id}: Mining stopped.")


    # --- Network Handling (remains the same) ---
    def _handle_network_message(self, peer_addr_tuple: tuple[str, int], message: Dict[str, Any]):
        # ... (same logic as before for handling NEW_TX, NEW_BLOCK, PEERS, PING/PONG, UTXO/BALANCE queries) ...
        peer_id_str = f"{peer_addr_tuple[0]}:{peer_addr_tuple[1]}"
        try:
            msg_type_val = message.get("type"); payload = message.get("payload")
            msg_type = MessageType(msg_type_val) if msg_type_val is not None else None

            if msg_type == MessageType.NEW_TRANSACTION and payload:
                tx = Transaction.from_dict(payload)
                if self.mempool.add_transaction(tx):
                     tx_msg = create_message(MessageType.NEW_TRANSACTION, payload=payload)
                     self.p2p_node.broadcast(tx_msg, exclude_peer=peer_addr_tuple)
            elif msg_type == MessageType.NEW_BLOCK and payload:
                block = Block.from_dict(payload)
                block_accepted = False
                with self.chain_lock: block_accepted = self.chain.add_block(block, self.utxo_set)
                if block_accepted:
                     logging.info(f"Node {self.id}: Accepted block {block.index} from {peer_id_str}.")
                     tx_ids = [tx.transaction_id for tx in block.transactions if not tx.is_coinbase()]; self.mempool.remove_transactions(tx_ids)
                     block_msg = create_message(MessageType.NEW_BLOCK, payload=payload); self.p2p_node.broadcast(block_msg, exclude_peer=peer_addr_tuple)
            elif msg_type == MessageType.GET_PEERS:
                 peer_list = self.p2p_node.get_peer_list(); peer_list_str = [f"{h}:{p}" for h, p in peer_list]
                 resp = create_message(MessageType.SEND_PEERS, payload={"peers": peer_list_str}); self.p2p_node.send_message(peer_addr_tuple, resp)
            elif msg_type == MessageType.SEND_PEERS and payload and "peers" in payload:
                 for peer_str in payload["peers"]:
                      try: h, p_str = peer_str.split(':'); p = int(p_str); self.p2p_node.connect_to_peer(h, p)
                      except: pass
            elif msg_type == MessageType.PING: self.p2p_node.send_message(peer_addr_tuple, create_message(MessageType.PONG))
            elif msg_type == MessageType.PONG: pass
            elif msg_type == MessageType.GET_UTXOS and payload and "address" in payload:
                 addr = payload["address"]; utxos_payload = {}
                 with self.chain_lock: utxos = self.utxo_set.find_utxos_for_address(addr)
                 for (tid, idx), out in utxos.items(): utxos_payload[f"{tid}:{idx}"] = out.to_dict()
                 resp = create_message(MessageType.SEND_UTXOS, payload={"address": addr, "utxos": utxos_payload}); self.p2p_node.send_message(peer_addr_tuple, resp)
            elif msg_type == MessageType.GET_ALL_BALANCES:
                  with self.chain_lock: balances = self.get_all_balances()
                  resp = create_message(MessageType.SEND_ALL_BALANCES, payload={"balances": balances}); self.p2p_node.send_message(peer_addr_tuple, resp)

        except Exception as e: logging.error(f"Node {self.id}: Error handling msg from {peer_id_str}: {e}", exc_info=True)


    # --- Transaction Creation (Updated for Anon ID concept) ---
    def create_transaction_from_managed_wallet(
        self, sender_address: str, recipient_address_or_anon_id: str, amount: float, fee: float, use_anonymity: bool = True
    ) -> Optional[Transaction]:
        """
        Creates a transaction using a managed wallet.
        If use_anonymity is True, uses anonymous ID for change/recipient and adds revocation token.
        """
        sender_wallet = self.get_managed_wallet(sender_address)
        if not sender_wallet:
            logging.error(f"Node {self.id}: Sender wallet {sender_address} not managed.")
            return None

        logging.info(f"Node {self.id}: Creating tx {amount} from managed {sender_address[:10]} -> {recipient_address_or_anon_id[:10]} (Anon: {use_anonymity})...")
        tx = None
        with self.chain_lock:
             # Wallet's create_transaction needs updating to handle anonymity flag
             # For now, pass use_anonymity flag conceptually to Wallet method
             tx = sender_wallet.create_transaction(
                  recipient_anon_id=recipient_address_or_anon_id, # Pass recipient ID (real or anon)
                  amount=amount,
                  fee=fee,
                  utxo_set=self.utxo_set,
                  use_anonymous_sender=use_anonymity # Tell wallet to use anon mode
             )
        return tx

    # --- Other methods (submit_and_broadcast, get_balance, get_all_balances, get_status, start, stop, save_chain) ---
    # Remain largely the same as previous version, ensure they use locks correctly
    def submit_and_broadcast_transaction(self, transaction: Transaction) -> bool:
         if self.mempool.add_transaction(transaction):
              tx_msg = create_message(MessageType.NEW_TRANSACTION, payload=transaction.to_dict())
              self.p2p_node.broadcast(tx_msg)
              logging.debug(f"Node {self.id}: Tx {transaction.transaction_id[:10]} added to mempool & broadcast.")
              return True
         else: logging.warning(f"Node {self.id}: Tx {transaction.transaction_id[:10]} rejected by mempool."); return False

    def get_balance(self, address_or_anon_id: Optional[str] = None) -> float:
        target_id = address_or_anon_id if address_or_anon_id else self.node_wallet.get_address()
        with self.chain_lock: balance = self.utxo_set.get_balance(target_id)
        return balance

    def get_all_balances(self) -> Dict[str, float]:
        balances = {}
        with self.chain_lock:
            known_scripts = set(out.lock_script for out in self.utxo_set.utxos.values()) # Get all unique lock scripts
            for script in known_scripts:
                balances[script] = self.utxo_set.get_balance(script) # Get balance for each script (address or anon_id)
        return balances

    def get_status(self) -> Dict[str, Any]:
         with self.chain_lock: chain_len = len(self.chain.blocks); utxo_count = len(self.utxo_set)
         mempool_size = len(self.mempool); peer_count = len(self.p2p_node.peers)
         node_balance = self.get_balance(self.node_wallet.get_address())
         return {"node_id": self.id, "node_address": self.node_wallet.get_address(), "is_mining": self.is_mining,
                 "chain_length": chain_len, "mempool_size": mempool_size, "utxo_count": utxo_count,
                 "peer_count": peer_count, "node_balance": node_balance, "managed_wallet_count": len(self.managed_wallets)}

    def start(self):
        logging.info(f"Node {self.id}: Starting P2P network...")
        self.p2p_node.start(); time.sleep(1)
        logging.info(f"Node {self.id}: Connecting to bootstrap peers: {self.bootstrap_peers}")
        for host, port in self.bootstrap_peers: self.p2p_node.connect_to_peer(host, port)

    def stop(self):
        logging.info(f"Node {self.id}: Stopping..."); self.stop_mining(); self.p2p_node.stop()
        self.save_chain(); logging.info(f"Node {self.id}: Stopped.")

    def save_chain(self):
         logging.info(f"Node {self.id}: Saving chain to {self.chain_file}...")
         with self.chain_lock: self.chain.save_to_file(self.chain_file)
         logging.info(f"Node {self.id}: Chain saved.")

    # --- Revocation Method (Conceptual) ---
    def decrypt_revocation_token(self, token: str) -> Optional[str]:
        """Decrypts a revocation token using the GOVT key (defined in utils)."""
        # In a real system, the node wouldn't necessarily hold the govt private key.
        # This would be done by a separate 'government' entity/tool.
        # Assumes GOVT_SYMMETRIC_KEY is accessible (imported from utils)
        logging.info(f"Node {self.id}: Attempting to decrypt revocation token...")
        return utils.decrypt_data(utils.GOVT_SYMMETRIC_KEY, token)
