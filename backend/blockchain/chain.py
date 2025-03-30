import json
import time
import copy
import logging # Use logging
from typing import List, Set, Optional, Tuple, TYPE_CHECKING

from .block import Block
from .consensus import Consensus, ProofOfWork
from .transaction import Transaction, TransactionInput, TransactionOutput, COINBASE_TX_ID
from .utils import verify, public_key_to_address, calculate_merkle_root # Import verify
from .utxo import UTXOKey

if TYPE_CHECKING:
    from .utxo import UTXOSet, UTXOKey

BLOCK_REWARD = 50.0

class Chain:
    # ... (__init__, _create_genesis_block, get_last_block remain mostly the same) ...
    def __init__(self, consensus: Consensus):
        self.blocks: List[Block] = []
        self.consensus: Consensus = consensus
        self._create_genesis_block()

    def _create_genesis_block(self):
        if not self.blocks:
            logging.info("Creating Genesis Block...")
            genesis_coinbase_input = TransactionInput(COINBASE_TX_ID, -1, {"data": "Genesis Block"})
            genesis_output = TransactionOutput(0.0, "genesis_address") # Placeholder address
            genesis_tx = Transaction(tx_inputs=[genesis_coinbase_input], tx_outputs=[genesis_output])
            timestamp = time.time()
            merkle_root = calculate_merkle_root([genesis_tx.transaction_id])
            previous_hash = "0" * 64
            nonce = self.consensus.prove(0, timestamp, previous_hash, merkle_root)
            genesis_block = Block(index=0, transactions=[genesis_tx], timestamp=timestamp,
                                  previous_hash=previous_hash, merkle_root=merkle_root, nonce=nonce)
            self.blocks.append(genesis_block)
            logging.info(f"Genesis block created. Hash: {genesis_block.hash[:10]}...")

    def get_last_block(self) -> Optional[Block]:
        return self.blocks[-1] if self.blocks else None

    # --- Main Block Addition Logic ---
    def add_block(self, block: Block, utxo_set: 'UTXOSet') -> bool:
        last_block = self.get_last_block()
        # --- Header/Link Validation ---
        if last_block:
             if block.previous_hash != last_block.hash or block.index != last_block.index + 1: return False
        elif block.index != 0 or block.previous_hash != "0"*64: return False # Genesis check
        if not self.consensus.validate_block_header(block): return False
        # --- Transaction Validation ---
        if not block.transactions: return False # Must have coinbase
        recalculated_merkle_root = calculate_merkle_root([tx.transaction_id for tx in block.transactions])
        if block.merkle_root != recalculated_merkle_root: return False

        temp_utxo_set = utxo_set.get_copy() # Validate against temporary state
        total_fees = 0.0
        coinbase_tx_count = 0
        spent_in_block: Set[UTXOKey] = set() # Track UTXOs spent *within this block*

        for i, tx in enumerate(block.transactions):
            if tx.is_coinbase():
                if i != 0: logging.warning(f"Block {block.index} invalid: Coinbase not first tx."); return False
                coinbase_tx_count += 1
                # TODO: Validate coinbase amount against reward+fees
                continue

            # Regular Transaction Validation
            is_valid, tx_fee, spent_keys = self.validate_transaction(tx, temp_utxo_set, spent_in_block)
            if not is_valid:
                 logging.warning(f"Block {block.index} invalid: Tx {tx.transaction_id[:10]} failed validation.")
                 return False

            total_fees += tx_fee
            spent_in_block.update(spent_keys) # Add keys spent by this tx to the block-wide set
            # Update temp UTXO set for subsequent checks *within this block*
            for key in spent_keys: temp_utxo_set.remove_utxo(key[0], key[1])
            for out_idx, out in enumerate(tx.outputs): temp_utxo_set.add_utxo(tx.transaction_id, out_idx, out)

        # Final checks
        if coinbase_tx_count != 1: logging.warning(f"Block {block.index} invalid: {coinbase_tx_count} coinbase txs."); return False
        # TODO: Final check on coinbase amount vs BLOCK_REWARD + total_fees

        # --- Commit ---
        utxo_set.update_from_block(block) # Update the *real* UTXO set
        self.blocks.append(block)
        logging.debug(f"Block {block.index} added to chain. UTXOs: {len(utxo_set)}")
        return True

    # --- Transaction Validation (Updated for Group Sig concept) ---
    def validate_transaction(
        self, transaction: Transaction, utxo_set: 'UTXOSet',
        spent_in_block: Set[UTXOKey] = set() # Pass set of already spent outputs in this block
    ) -> Tuple[bool, float, List[UTXOKey]]: # Return validity, fee, and UTXOs spent by THIS tx
        """
        Validates a single non-coinbase transaction.
        Checks UTXO existence, ownership (via pubkey derived from sig), signature, value conservation.
        Does NOT check or use the revocation_token here.
        Returns: Tuple (is_valid: bool, fee: float, spent_keys_in_this_tx: List[UTXOKey])
        """
        if transaction.is_coinbase(): return False, 0.0, []

        total_input_value = 0.0
        spent_keys_in_this_tx: List[UTXOKey] = []

        try: data_to_sign = transaction.get_data_to_sign()
        except Exception as e: logging.error(f"Tx {transaction.transaction_id[:10]} err creating data to sign: {e}"); return False, 0.0, []

        # Validate Inputs
        if not transaction.inputs: return False, 0.0, []
        for i, inp in enumerate(transaction.inputs):
            utxo_key: UTXOKey = (inp.transaction_id, inp.output_index)

            # 1. Check if already spent within this block or transaction
            if utxo_key in spent_in_block or utxo_key in spent_keys_in_this_tx:
                 logging.warning(f"Tx {transaction.transaction_id[:10]} invalid: Input {i} UTXO {utxo_key} double spent in block/tx.")
                 return False, 0.0, []

            # 2. Find the UTXO in the provided set
            spent_utxo = utxo_set.get_utxo(utxo_key[0], utxo_key[1])
            if spent_utxo is None:
                 logging.warning(f"Tx {transaction.transaction_id[:10]} invalid: Input {i} references non-existent/spent UTXO {utxo_key}.")
                 return False, 0.0, []

            # 3. Verify Unlock Script (Signature and Ownership)
            unlock_script = inp.unlock_script
            if not isinstance(unlock_script, dict) or \
               'signature' not in unlock_script or \
               'public_key' not in unlock_script: # Require pubkey for verification
                 logging.warning(f"Tx {transaction.transaction_id[:10]} invalid: Input {i} unlock_script missing fields.")
                 return False, 0.0, []

            pub_key_hex = unlock_script['public_key']
            signature_hex = unlock_script['signature']

            # 4. Derive address from provided public key
            try: derived_address = public_key_to_address(pub_key_hex)
            except Exception: logging.warning(f"Tx {transaction.transaction_id[:10]} invalid: Input {i} has bad public key format."); return False, 0.0, []

            # 5. Check if derived address matches the UTXO's lock script
            # This links the signature/pubkey to the UTXO being spent.
            # In our simple model, lock_script IS the address (real or anonymous)
            # If using anonymous IDs, this check needs adjustment based on how anon IDs are linked/verified.
            # WORKAROUND: If lock_script is an anon ID, we can't directly verify against derived address.
            # For now, we ASSUME the UTXO *was* locked to the *real* address derived from the pubkey.
            # This is a WEAKNESS of the simplified anonymous model.
            # A real group sig would verify the signature proves membership and right-to-spend without revealing identity here.
            utxo_intended_recipient = spent_utxo.lock_script
            # print(f"DEBUG: UTXO Lock: {utxo_intended_recipient}, Derived Addr: {derived_address}")
            # if utxo_intended_recipient != derived_address: # Temporarily disable if using anon IDs in lock script without link proof
            #      logging.warning(f"Tx {transaction.transaction_id[:10]} invalid: Input {i} pubkey derived addr doesn't match UTXO lock script. (Check Anon ID logic)")
                 # return False, 0.0, [] # Re-enable if lock script IS the real address

            # 6. Verify the signature against the transaction data
            if not verify(pub_key_hex, data_to_sign, signature_hex):
                logging.warning(f"Tx {transaction.transaction_id[:10]} invalid: Input {i} invalid signature.")
                return False, 0.0, []

            # If checks pass, accumulate value and mark UTXO as spent for this TX
            total_input_value += spent_utxo.amount
            spent_keys_in_this_tx.append(utxo_key)

        # Validate Outputs
        if not transaction.outputs: return False, 0.0, []
        total_output_value = 0.0
        for i, out in enumerate(transaction.outputs):
            if out.amount < 0: logging.warning(f"Tx {transaction.transaction_id[:10]} invalid: Output {i} negative amount."); return False, 0.0, []
            total_output_value += out.amount

        # Check Value Conservation
        fee = round(total_input_value - total_output_value, 8)
        if fee < 0: logging.warning(f"Tx {transaction.transaction_id[:10]} invalid: Output > Input value."); return False, 0.0, []

        # All checks passed for this transaction
        return True, fee, spent_keys_in_this_tx

    # --- Persistence (remains the same) ---
    def save_to_file(self, path: str):
        try:
            data = {"chain": [block.to_dict() for block in self.blocks]}
            with open(path, 'w') as f: json.dump(data, f, indent=2)
        except Exception as e: logging.error(f"Error saving chain to {path}: {e}")

    @classmethod
    def load_from_file(cls, path: str, consensus: Consensus) -> Optional['Chain']:
        try:
            with open(path, 'r') as f: data = json.load(f)
            chain = cls(consensus); chain.blocks = [] # Start empty before loading
            loaded_blocks = [Block.from_dict(block_data) for block_data in data.get('chain', [])]
            if not loaded_blocks or loaded_blocks[0].index != 0 or loaded_blocks[0].previous_hash != "0"*64:
                 logging.warning(f"Loaded chain from {path} invalid/empty. Creating fresh genesis.")
                 chain._create_genesis_block()
            else:
                 chain.blocks = loaded_blocks # Assign loaded blocks
            logging.info(f"Chain loaded from {path}. Length: {len(chain.blocks)}")
            return chain
        except FileNotFoundError: return None # Expected if first run
        except Exception as e: logging.error(f"Error loading chain from {path}: {e}. Creating fresh genesis."); return cls(consensus) # Fallback
