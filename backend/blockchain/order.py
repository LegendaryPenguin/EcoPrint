# blockchain/order.py
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal, Dict, Any

@dataclass
class Order:
    """Represents a buy or sell order for a specific market pair."""

    # Fields WITHOUT defaults first
    wallet_address: str # The REAL address of the wallet placing the order
    market_pair: str # e.g., "CARBON-USD", "NATIVE-USD"
    order_type: Literal['buy', 'sell'] # 'buy' or 'sell' the base asset (e.g., CARBON)
    amount: float # Amount of BASE asset (e.g., CARBON)
    price: float # Price per BASE asset in QUOTE asset (e.g., USD)

    # Fields WITH defaults last
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    filled: float = 0.0 # Amount of BASE asset filled

    # --- Methods ---
    def is_filled(self) -> bool:
        return self.filled >= self.amount - 0.00000001 # Float tolerance

    def remaining_amount(self) -> float:
        return max(0.0, round(self.amount - self.filled, 8))

    def to_dict(self) -> dict:
        # Make sure all fields are included
        return {
            "order_id": self.order_id,
            "wallet_address": self.wallet_address,
            "market_pair": self.market_pair,
            "order_type": self.order_type,
            "amount": self.amount,
            "price": self.price,
            "timestamp": self.timestamp,
            "filled": self.filled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Order':
        try:
            required = ['wallet_address', 'market_pair', 'order_type', 'amount', 'price']
            if not all(k in data for k in required):
                 raise ValueError(f"Missing required fields: {required}")
            # Basic type validation
            order_type = data['order_type']
            if order_type not in ['buy', 'sell']: raise ValueError("Invalid order_type")
            amount = float(data['amount']); price = float(data['price'])
            if amount <=0 or price <= 0: raise ValueError("Amount and price must be positive")

            return cls(
                 wallet_address=data['wallet_address'],
                 market_pair=data['market_pair'],
                 order_type=order_type,
                 amount=amount,
                 price=price,
                 order_id=data.get('order_id', str(uuid.uuid4())),
                 timestamp=data.get('timestamp', time.time()),
                 filled=float(data.get('filled', 0.0)) # Ensure filled is float
            )
        except (KeyError, ValueError, TypeError) as e:
             raise ValueError(f"Error deserializing order: {e} - Data: {data}") from e
