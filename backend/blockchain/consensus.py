import time
from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING

# Assuming utils.py is accessible
from .utils import calculate_block_hash

if TYPE_CHECKING:
    from .block import Block

class Consensus(ABC):
    """Abstract Base Class for consensus algorithms."""

    @abstractmethod
    def prove(self, index: int, timestamp: float, previous_hash: str, merkle_root: str) -> int:
        """Generate the proof (nonce) for a new block header."""
        pass

    @abstractmethod
    def validate_block_header(self, block: 'Block') -> bool:
        """Validate a block's header based on consensus rules (e.g., PoW)."""
        pass


class ProofOfWork(Consensus):
    """Simple Proof-of-Work implementation."""
    def __init__(self, difficulty: int = 4):
        if difficulty < 1:
            raise ValueError("Difficulty must be at least 1")
        self.difficulty = difficulty
        self.target_prefix = '0' * difficulty
        print(f"ProofOfWork initialized with difficulty {difficulty} (target: '{self.target_prefix}...')")

    def prove(self, index: int, timestamp: float, previous_hash: str, merkle_root: str) -> int:
        """Finds a nonce (integer proof) that results in a hash with leading zeros."""
        nonce = 0
        while True:
            hash_attempt = calculate_block_hash(index, timestamp, previous_hash, merkle_root, nonce)
            if hash_attempt.startswith(self.target_prefix):
                return nonce
            nonce += 1
            # Optional: Add a tiny sleep if running on a single thread to prevent UI freeze
            # if nonce % 100000 == 0: time.sleep(0.001)

    def validate_block_header(self, block: 'Block') -> bool:
        """Validates the block's hash meets the difficulty target."""
        # 1. Recalculate hash based on header fields including the nonce
        recalculated_hash = calculate_block_hash(
            block.index,
            block.timestamp,
            block.previous_hash,
            block.merkle_root,
            block.nonce
        )

        # 2. Check if calculated hash matches the block's stored hash
        if block.hash != recalculated_hash:
            print(f"Header Hash mismatch: Stored {block.hash}, Calculated {recalculated_hash}")
            return False

        # 3. Check if the hash meets the difficulty target
        if not block.hash.startswith(self.target_prefix):
            print(f"PoW Invalid: Hash {block.hash} does not start with {self.target_prefix}")
            return False

        return True

    def __str__(self):
        return f"ProofOfWork(difficulty={self.difficulty})"
