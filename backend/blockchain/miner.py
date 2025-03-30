# blockchain/miner.py

import time
import json
import logging # Use logging
from typing import List, Optional, TYPE_CHECKING, Set, Tuple # Import Set and Tuple

# Assuming utils, transaction, block, consensus are accessible via relative paths
from .utils import calculate_merkle_root, public_key_to_address, verify
from .transaction import Transaction, TransactionInput, TransactionOutput, COINBASE_TX_ID, COINBASE_OUTPUT_INDEX
from .block import Block
from .consensus import Consensus

# Import type alias if defined in utxo.py, otherwise define here for clarity if needed
try:
    from .utxo import UTXOKey # UTXOKey = Tuple[str, int]
except ImportError:
    logging.warning("Could not import UTXOKey from .utxo, defining locally.")
    UTXOKey = Tuple[str, int]


if TYPE_CHECKING:
    from .mempool import Mempool
    from .utxo import UTXOSet
    from .chain import Chain

BLOCK_REWARD = 50.0 # Example reward

def mine_new_block(
    mempool: 'Mempool',
    utxo_set: 'UTXOSet',
    chain: 'Chain',
    miner_address: str,
    consensus: Consensus
) -> Optional[Block]:
    """
    Mines a new block including transactions from the mempool and a coinbase reward.
    Performs validation against a temporary UTXO state during transaction selection.
    """
    last_block = chain.get_last_block()
    if not last_block:
        logging.error("Miner Error: Cannot mine without a previous block (genesis missing?).")
        return None

    previous_hash = last_block.hash
    next_index = last_block.index + 1

    # --- Transaction Selection and Validation ---
    valid_txs_for_block: List[Transaction] = []
    total_fees = 0.0
    temp_utxo_set = utxo_set.get_copy() # Validate against a temporary snapshot
    pending_txs = mempool.get_pending_transactions() # Get potential transactions
    spent_in_block_accumulator: Set[UTXOKey] = set() # Track UTXOs spent *within this block*

    logging.debug(f"Miner: Considering {len(pending_txs)} txs for block {next_index}.")

    for tx in pending_txs:
        if tx.is_coinbase(): # Should not be in mempool, but check anyway
            logging.warning(f"Miner: Skipping coinbase tx found in mempool: {tx.transaction_id}")
            continue

        # Validate the transaction against the *current state* of the temporary UTXO set
        # Pass the accumulator to check for double-spends within this potential block
        is_valid, tx_fee, spent_keys_in_this_tx = chain.validate_transaction(
            tx, temp_utxo_set, spent_in_block=spent_in_block_accumulator
        )

        if is_valid:
            # If valid according to validate_transaction (which checks accumulator), include it
            valid_txs_for_block.append(tx)
            total_fees += tx_fee

            # Update the temporary UTXO set state *for the next iteration*
            for key in spent_keys_in_this_tx:
                temp_utxo_set.remove_utxo(key[0], key[1])
            for out_idx, out in enumerate(tx.outputs):
                temp_utxo_set.add_utxo(tx.transaction_id, out_idx, out)

            # Add keys spent by this valid tx to the accumulator for the next tx check
            spent_in_block_accumulator.update(spent_keys_in_this_tx)
            # logging.debug(f"  Miner included tx {tx.transaction_id[:10]} fee {tx_fee:.8f}")
        # else: # Transaction failed validation
            # logging.debug(f"  Miner rejected tx {tx.transaction_id[:10]} during pre-validation.")
            pass # Just don't include it

    # --- Coinbase and Block Construction ---
    coinbase_output = TransactionOutput(amount=round(BLOCK_REWARD + total_fees, 8), lock_script=miner_address)
    coinbase_input = TransactionInput(
        transaction_id=COINBASE_TX_ID,
        output_index=COINBASE_OUTPUT_INDEX,
        unlock_script={"data": f"Block {next_index} reward mined by {miner_address[:10]}"} # Add miner address tag
    )
    coinbase_tx = Transaction(tx_inputs=[coinbase_input], tx_outputs=[coinbase_output])

    # Prepend coinbase transaction
    all_txs_for_block = [coinbase_tx] + valid_txs_for_block
    tx_ids = [tx.transaction_id for tx in all_txs_for_block]
    merkle_root = calculate_merkle_root(tx_ids)

    # --- Proof of Work ---
    timestamp = time.time()
    logging.debug(f"Miner: Starting PoW for block {next_index} ({len(all_txs_for_block)} txs)...")
    nonce = consensus.prove(next_index, timestamp, previous_hash, merkle_root)
    logging.debug(f"Miner: Found nonce {nonce} for block {next_index}")

    # --- Create Final Block ---
    new_block = Block(
        index=next_index,
        transactions=all_txs_for_block,
        timestamp=timestamp,
        previous_hash=previous_hash,
        merkle_root=merkle_root,
        nonce=nonce
    )
    # Block hash is calculated in Block's __post_init__ automatically

    return new_block
