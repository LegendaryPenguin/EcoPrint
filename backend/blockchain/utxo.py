from typing import Dict, Tuple, Optional, List, TYPE_CHECKING
import copy

from .transaction import TransactionOutput, TransactionInput, Transaction, COINBASE_TX_ID # Relative import

UTXOKey = Tuple[str, int] # (transaction_id, output_index)

class UTXOSet:
    def __init__(self):
        # Stores UTXOKey -> TransactionOutput
        self.utxos: Dict[UTXOKey, TransactionOutput] = {}

    def find_utxos_for_address(self, address_or_anon_id: str) -> Dict[UTXOKey, TransactionOutput]:
        """
        Finds all UTXOs locked to a specific address or anonymous identifier.
        NOTE: In this simple model, 'address_or_anon_id' is the value in lock_script.
        """
        found = {}
        for utxo_key, utxo_output in self.utxos.items():
            if utxo_output.lock_script == address_or_anon_id:
                found[utxo_key] = utxo_output
        return found

    # ... (get_balance - calculates based on address/id passed) ...
    def get_balance(self, address_or_anon_id: str) -> float:
        """Calculates the total balance for a given address or anonymous ID."""
        utxos = self.find_utxos_for_address(address_or_anon_id)
        return sum(output.amount for output in utxos.values())

    # ... (add_utxo, remove_utxo, get_utxo remain the same) ...
    def add_utxo(self, tx_id: str, index: int, output: TransactionOutput):
        key = (tx_id, index)
        # Optional: Add check if key exists, but update_from_block relies on overwrite?
        # if key in self.utxos: print(f"Warning: UTXO {key} overwritten.") # Removed noisy warning
        self.utxos[key] = output

    def remove_utxo(self, tx_id: str, index: int) -> Optional[TransactionOutput]:
        key = (tx_id, index)
        return self.utxos.pop(key, None)

    def get_utxo(self, tx_id: str, index: int) -> Optional[TransactionOutput]:
         key = (tx_id, index)
         return self.utxos.get(key, None)

    # ... (update_from_block, rebuild, __len__, get_copy remain the same) ...
    def update_from_block(self, block: 'Block'):
        for tx in block.transactions:
            if not tx.is_coinbase():
                for inp in tx.inputs:
                    self.remove_utxo(inp.transaction_id, inp.output_index)
            for i, out in enumerate(tx.outputs):
                self.add_utxo(tx.transaction_id, i, out)

    def rebuild(self, chain: 'Chain'):
        # print("Rebuilding UTXO set...") # Less verbose
        self.utxos.clear()
        for block in chain.blocks:
            self.update_from_block(block)
        # print(f"UTXO set rebuilt. Size: {len(self.utxos)}")

    def __len__(self) -> int: return len(self.utxos)

    def get_copy(self) -> 'UTXOSet':
         new_set = UTXOSet(); new_set.utxos = copy.deepcopy(self.utxos); return new_set
