import hashlib
import json
from typing import List, Dict, Any, TYPE_CHECKING

# Import utils, potentially including encryption functions if needed directly
from . import utils

# Constants
COINBASE_TX_ID = "0" * 64
COINBASE_OUTPUT_INDEX = -1

class TransactionInput:
    def __init__(self, transaction_id: str, output_index: int, unlock_script: Dict[str, Any]):
        """
        unlock_script now contains:
        - For regular tx: {'signature': hex, 'public_key': hex, 'revocation_token': base64_str}
        - For coinbase: {'data': '...'}
        """
        self.transaction_id = transaction_id
        self.output_index = output_index
        self.unlock_script = unlock_script

    def to_dict(self) -> Dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "output_index": self.output_index,
            "unlock_script": self.unlock_script, # Must be JSON serializable
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TransactionInput':
        unlock_script = data.get('unlock_script', {})
        # Basic check, could add more validation
        if not isinstance(unlock_script, dict): unlock_script = {}
        return cls(data['transaction_id'], data['output_index'], unlock_script)


class TransactionOutput:
    def __init__(self, amount: float, lock_script: str):
        """
        lock_script is now the ANONYMOUS identifier/address of the recipient.
        """
        if amount < 0: raise ValueError("Transaction output amount cannot be negative")
        self.amount = amount
        # lock_script could be the recipient's "anonymous_id" in this model
        self.lock_script = lock_script

    def to_dict(self) -> Dict[str, Any]:
        return {"amount": self.amount, "lock_script": self.lock_script}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TransactionOutput':
        return cls(data['amount'], data['lock_script'])


class Transaction:
    def __init__(self, tx_inputs: List[TransactionInput], tx_outputs: List[TransactionOutput], tx_id: str = None):
        if not tx_inputs or not tx_outputs:
             raise ValueError("Transaction must have inputs and outputs")
        self.inputs = tx_inputs
        self.outputs = tx_outputs
        # Use static method for calculation during init
        self.transaction_id = tx_id if tx_id else Transaction._calculate_transaction_id(self.inputs, self.outputs)

    @staticmethod
    def _is_coinbase_data(tx_inputs: List[TransactionInput]) -> bool:
        return (
            len(tx_inputs) == 1 and
            tx_inputs[0].transaction_id == COINBASE_TX_ID and
            tx_inputs[0].output_index == COINBASE_OUTPUT_INDEX
        )

    @staticmethod
    def _calculate_transaction_id(tx_inputs: List[TransactionInput], tx_outputs: List[TransactionOutput]) -> str:
        """Calculates TXID. Includes unique coinbase data, excludes regular unlock scripts."""
        if Transaction._is_coinbase_data(tx_inputs):
            # Include full input (with unique data in unlock_script) for coinbase
             tx_data = {"inputs": [inp.to_dict() for inp in tx_inputs], "outputs": [out.to_dict() for out in tx_outputs]}
        else:
            # Regular: Only input refs and outputs (signature/token not part of TXID)
            tx_data = {
                "inputs": [{"transaction_id": inp.transaction_id, "output_index": inp.output_index} for inp in tx_inputs],
                "outputs": [out.to_dict() for out in tx_outputs],
            }
        tx_string = json.dumps(tx_data, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(tx_string).hexdigest()

    def is_coinbase(self) -> bool:
        return self._is_coinbase_data(self.inputs)

    def get_data_to_sign(self) -> str:
         """Data that the sender signs. Excludes unlock scripts."""
         tx_data = {
            "inputs": [{"transaction_id": inp.transaction_id, "output_index": inp.output_index} for inp in self.inputs],
            "outputs": [out.to_dict() for out in self.outputs],
         }
         return json.dumps(tx_data, sort_keys=True, separators=(',',':'))

    def to_dict(self) -> Dict[str, Any]:
        # Ensure ID exists
        if not hasattr(self, 'transaction_id') or not self.transaction_id:
             self.transaction_id = self._calculate_transaction_id(self.inputs, self.outputs)
        return {
            "transaction_id": self.transaction_id,
            "inputs": [inp.to_dict() for inp in self.inputs],
            "outputs": [out.to_dict() for out in self.outputs],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Transaction':
        try:
            inputs = [TransactionInput.from_dict(inp) for inp in data.get('inputs', [])]
            outputs = [TransactionOutput.from_dict(out) for out in data.get('outputs', [])]
            tx_id_from_data = data.get('transaction_id')
            if not tx_id_from_data: raise ValueError("Missing 'transaction_id'")
            return cls(inputs, outputs, tx_id=tx_id_from_data)
        except Exception as e: raise ValueError(f"Error deserializing transaction: {e}")
