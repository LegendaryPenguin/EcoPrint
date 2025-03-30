import json
from typing import List, Optional, TYPE_CHECKING, Dict, Tuple

from . import utils
from .transaction import Transaction, TransactionInput, TransactionOutput

if TYPE_CHECKING:
    from .utxo import UTXOSet, UTXOKey

class Wallet:
    def __init__(self):
        self.private_key_hex: str
        self.public_key_hex: str
        self.address: str # This is the REAL address derived from public key
        self._generate_new_keys()
        # Add a cache for generated anonymous IDs (optional, for consistency per session)
        self.current_anonymous_id: Optional[str] = None

    def _generate_new_keys(self):
        self.private_key_hex, self.public_key_hex = utils.generate_key_pair()
        self.address = utils.public_key_to_address(self.public_key_hex)

    def get_address(self) -> str:
        return self.address

    def get_anonymous_id(self, force_new=False) -> str:
         """Gets or generates a pseudo-anonymous ID for transactions."""
         # In a real system, this would involve interaction with a group manager or use derived keys.
         # Here, we just generate a simple one based on the real address.
         # Caching it helps if multiple transactions happen quickly, but real group sigs
         # often use one-time identifiers per transaction.
         if not self.current_anonymous_id or force_new:
              self.current_anonymous_id = utils.generate_anonymous_id(self.address)
         return self.current_anonymous_id

    def create_transaction(
        self,
        recipient_anon_id: str, # Recipient is now an anonymous ID
        amount: float,
        fee: float,
        utxo_set: 'UTXOSet',
        use_anonymous_sender: bool = True # Flag to enable group sig feature
    ) -> Optional[Transaction]:
        """
        Creates a signed transaction. If use_anonymous_sender is True,
        it uses an anonymous ID for outputs and includes a revocation token.
        """
        if amount <= 0 or fee < 0: return None

        # Determine sender and UTXOs to use based on anonymity flag
        sender_id_for_utxo_lookup: str
        sender_id_for_change: str
        if use_anonymous_sender:
             # We need to find UTXOs locked to PREVIOUS anonymous IDs used by this wallet.
             # This simple model breaks here - UTXO lookup needs adjusting.
             # Workaround: Assume for now UTXOs are still locked to the REAL address.
             # A better model would require linking anon IDs or a different UTXO lookup.
             sender_id_for_utxo_lookup = self.address # !!! WORKAROUND !!!
             sender_id_for_change = self.get_anonymous_id() # Change goes to current anon ID
        else:
             # Regular transaction, use real address
             sender_id_for_utxo_lookup = self.address
             sender_id_for_change = self.address

        available_utxos = utxo_set.find_utxos_for_address(sender_id_for_utxo_lookup)
        if not available_utxos: return None # No funds under that identifier

        # --- UTXO Selection (same as before, uses available_utxos) ---
        inputs_refs: List[Tuple[UTXOKey, TransactionOutput]] = []
        total_input_amount = 0.0
        target_amount = amount + fee
        sorted_utxos = sorted(available_utxos.items(), key=lambda item: item[1].amount)
        for utxo_key, utxo_output in sorted_utxos:
            inputs_refs.append((utxo_key, utxo_output))
            total_input_amount += utxo_output.amount
            if total_input_amount >= target_amount: break

        if total_input_amount < target_amount: return None # Insufficient funds

        # --- Create Outputs ---
        outputs: List[TransactionOutput] = []
        # Output to recipient uses their anonymous ID
        outputs.append(TransactionOutput(round(amount, 8), recipient_anon_id))
        # Change output uses sender's designated ID (real or anonymous)
        change_amount = round(total_input_amount - target_amount, 8)
        if change_amount > 0.00000001:
             outputs.append(TransactionOutput(change_amount, sender_id_for_change))

        # --- Prepare for Signing ---
        # Create dummy inputs just to get data structure for signing hash
        dummy_inputs = [TransactionInput(key[0], key[1], {}) for key, _ in inputs_refs]
        unsigned_tx_structure = Transaction(dummy_inputs, outputs)
        data_to_sign = unsigned_tx_structure.get_data_to_sign()

        # --- Sign Inputs and Add Revocation Token ---
        signed_inputs: List[TransactionInput] = []
        for (utxo_key, _), dummy_inp in zip(inputs_refs, dummy_inputs):
            # Sign with the REAL private key
            signature_hex = utils.sign(self.private_key_hex, data_to_sign)
            unlock_script: Dict[str, Any] = {
                "signature": signature_hex,
                "public_key": self.public_key_hex # Include REAL public key for verification
            }
            # If using anonymity, add the revocation token
            if use_anonymous_sender:
                 # Encrypt the REAL address (or public key) using the shared GOVT key
                 # WARNING: See security note in utils.py about symmetric key usage here
                 revocation_token = utils.encrypt_data(utils.GOVT_SYMMETRIC_KEY, self.address)
                 unlock_script["revocation_token"] = revocation_token

            signed_inputs.append(TransactionInput(utxo_key[0], utxo_key[1], unlock_script))

        # --- Create Final Transaction ---
        final_tx = Transaction(signed_inputs, outputs)
        return final_tx
