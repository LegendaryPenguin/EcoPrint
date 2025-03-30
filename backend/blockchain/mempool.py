from typing import Dict, List, Optional

# Assuming transaction.py and utils.py are accessible
from .transaction import Transaction
from .utils import verify

class Mempool:
    """Stores pending transactions."""
    def __init__(self, max_size: int = 1000):
        # Stores transaction_id -> Transaction object
        self.pending_transactions: Dict[str, Transaction] = {}
        self.max_size = max_size

    def add_transaction(self, transaction: Transaction) -> bool:
        """
        Adds a transaction to the mempool after basic validation.
        Returns True if added, False otherwise.
        """
        if transaction.transaction_id in self.pending_transactions:
            # print(f"Transaction {transaction.transaction_id[:10]}... already in mempool.")
            return False # Already exists

        if len(self.pending_transactions) >= self.max_size:
            print("Mempool is full. Transaction rejected.")
            # TODO: Implement eviction strategy (e.g., lowest fee)
            return False

        # Basic validation (structure, signatures) - NOT UTXO validity
        if not self._validate_transaction_basic(transaction):
            print(f"Transaction {transaction.transaction_id[:10]}... failed basic validation. Rejected.")
            return False

        self.pending_transactions[transaction.transaction_id] = transaction
        # print(f"Added transaction {transaction.transaction_id[:10]}... to mempool.")
        return True

    def _validate_transaction_basic(self, transaction: Transaction) -> bool:
        """Performs basic validation (signatures, format) before adding to mempool."""
        if transaction.is_coinbase():
            print("Error: Coinbase transaction submitted to mempool.")
            return False # Coinbase transactions are created by miners, not submitted

        if not transaction.inputs:
            print("Error: Transaction has no inputs.")
            return False
        if not transaction.outputs:
            print("Error: Transaction has no outputs.")
            return False

        data_to_sign = transaction.get_data_to_sign()
        for i, inp in enumerate(transaction.inputs):
            if not isinstance(inp.unlock_script, dict) or \
               'signature' not in inp.unlock_script or \
               'public_key' not in inp.unlock_script:
                print(f"Error: Input {i} has invalid unlock_script format.")
                return False

            sig_hex = inp.unlock_script['signature']
            pub_key_hex = inp.unlock_script['public_key']

            if not verify(pub_key_hex, data_to_sign, sig_hex):
                print(f"Error: Invalid signature for input {i} in transaction {transaction.transaction_id[:10]}...")
                return False

        # TODO: Add more checks like positive output amounts, non-negative fee etc.

        return True


    def get_pending_transactions(self, limit: int = 50) -> List[Transaction]:
        """Gets a list of pending transactions, up to a limit."""
        # TODO: Implement fee prioritization
        return list(self.pending_transactions.values())[:limit]

    def remove_transactions(self, transaction_ids: List[str]):
        """Removes transactions by ID, typically after they are mined."""
        removed_count = 0
        for tx_id in transaction_ids:
            if self.pending_transactions.pop(tx_id, None):
                removed_count += 1
        if removed_count > 0:
            print(f"Removed {removed_count} txs from mempool. {len(self.pending_transactions)} remaining.")

    def get_transaction(self, tx_id: str) -> Optional[Transaction]:
        """Gets a specific transaction by ID."""
        return self.pending_transactions.get(tx_id)

    def __len__(self) -> int:
        return len(self.pending_transactions)
