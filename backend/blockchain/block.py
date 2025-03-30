import time
import json
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime

# Assuming utils.py and transaction.py are accessible
from .utils import calculate_block_hash, calculate_merkle_root
from .transaction import Transaction

@dataclass
class Block:
    index: int
    transactions: List[Transaction] # Now holds actual Transaction objects
    timestamp: float = field(default_factory=time.time)
    previous_hash: str = "0"
    nonce: int = 0 # Proof-of-Work nonce
    merkle_root: Optional[str] = None
    hash: Optional[str] = None

    def __post_init__(self):
        # Calculate Merkle root if not provided
        if self.merkle_root is None:
            self.merkle_root = self._calculate_internal_merkle_root()
        # Calculate block hash if not provided
        if self.hash is None:
            self.hash = self._calculate_internal_hash()

    def _calculate_internal_merkle_root(self) -> str:
        """Calculates the Merkle root from the block's transactions."""
        transaction_ids = [tx.transaction_id for tx in self.transactions]
        return calculate_merkle_root(transaction_ids)

    def _calculate_internal_hash(self) -> str:
        """Calculates the block's header hash."""
        if self.merkle_root is None: # Should be calculated by now
            self.merkle_root = self._calculate_internal_merkle_root()
        return calculate_block_hash(
            self.index,
            self.timestamp,
            self.previous_hash,
            self.merkle_root,
            self.nonce
        )

    def formatted_timestamp(self) -> str:
        """Returns a human-readable timestamp."""
        return datetime.fromtimestamp(self.timestamp).strftime('%Y-%m-%d %H:%M:%S')

    def to_dict(self) -> Dict[str, Any]:
        """Serializes block to a dictionary."""
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "transactions": [tx.to_dict() for tx in self.transactions],
            "previous_hash": self.previous_hash,
            "hash": self.hash,
            "merkle_root": self.merkle_root,
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Block':
        """Deserializes block from a dictionary."""
        transactions = [Transaction.from_dict(tx_data) for tx_data in data['transactions']]
        block = cls(
            index=data['index'],
            timestamp=data['timestamp'],
            transactions=transactions,
            previous_hash=data['previous_hash'],
            merkle_root=data['merkle_root'], # Use stored merkle root
            nonce=data['nonce'],
            hash=data['hash'] # Use stored hash
        )
        # Optional: Verify loaded hash and merkle root
        # if block.merkle_root != block._calculate_internal_merkle_root():
        #     print(f"Warning: Merkle root mismatch for loaded block {block.index}")
        # if block.hash != block._calculate_internal_hash():
        #      print(f"Warning: Hash mismatch for loaded block {block.index}")
        return block