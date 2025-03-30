# node.py
import os
import time
import threading
import random
import logging
import heapq # For priority queue matching in order book
from typing import Optional, List, Dict, Any, Tuple, Set

# Blockchain components
from blockchain.wallet import Wallet
from blockchain.consensus import Consensus
from blockchain.chain import Chain
from blockchain.utxo import UTXOSet, UTXOKey
from blockchain.mempool import Mempool
from blockchain.block import Block
from blockchain.transaction import Transaction
from blockchain import miner
from blockchain import utils
from blockchain.order import Order # <<< Import Order class

# Networking components
from network.p2p import P2PNode
from network.message import MessageType, create_message, parse_message

# Constants
CHAIN_FILE_PREFIX = "chain_data_node_"
INITIAL_USD_BALANCE = 1000.0
INITIAL_CARBON_BALANCE = 500.0 # Give wallets some starting Carbon tokens too
ORDER_BOOK_MATCH_INTERVAL = 5.0 # How often to check for matches (seconds)
DEFAULT_TX_FEE = 0.01 # Default fee for automatically generated transactions (e.g., settlement)

# Define supported market pairs
SUPPORTED_MARKETS = ["CARBON-USD", "NATIVE-USD"] # Example

class Node:
    """Represents a node with P2P, API, multi-asset Order Book, off-chain balances, and mining."""
    def __init__(self, host: str, port: int, node_id: str, consensus: Consensus, bootstrap_peers: List[tuple[str, int]] = [], chain_file_base: str = CHAIN_FILE_PREFIX):
        self.id = node_id
        self.consensus = consensus
        self.mempool = Mempool()
        self.chain_file = f"{chain_file_base}{self.id}.json"

        # --- Blockchain State ---
        loaded_chain = Chain.load_from_file(self.chain_file, self.consensus) if os.path.exists(self.chain_file) else None
        self.chain = loaded_chain if loaded_chain else Chain(self.consensus)
        self.chain_lock = threading.Lock() # Protects chain and utxo_set
        self.utxo_set = UTXOSet()
        with self.chain_lock:
            self.utxo_set.rebuild(self.chain)
        # -----------------------

        # --- Wallet & Off-Chain Balance Management ---
        self.managed_wallets: Dict[str, Wallet] = {}
        self.usd_balances: Dict[str, float] = {}
        self.carbon_balances: Dict[str, float] = {} # <<< NEW Carbon Balance Store
        # Single lock protects managed_wallets and all off-chain balance dicts
        self.balance_lock = threading.Lock()
        self.node_wallet = self._create_or_load_node_wallet() # Creates primary wallet and sets initial balances
        # -------------------------------------------

        # --- Multi-Asset Order Book ---
        # Structure: { "market_pair": {"buys": heap, "sells": heap}, ... }
        self.order_books: Dict[str, Dict[str, List[Tuple[float, float, Order]]]] = {
            market: {"buys": [], "sells": []} for market in SUPPORTED_MARKETS
        }
        self.order_book_lock = threading.Lock() # Protects the entire order_books structure
        self.order_matching_thread: Optional[threading.Thread] = None
        self.stop_order_matching_flag = threading.Event()
        # --------------------------

        # P2P Networking
        self.p2p_node = P2PNode(host, port, self.id, self._handle_network_message)
        self.bootstrap_peers = bootstrap_peers

        # Mining control
        self.is_mining = False
        self.mining_thread: Optional[threading.Thread] = None
        self.stop_mining_flag = threading.Event()

        logging.info(f"Node {self.id} initialized. Node Wallet: {self.node_wallet.get_address()[:10]} Chain: {len(self.chain.blocks)} UTXOs: {len(self.utxo_set)}")

    # --- Wallet Management Methods (Assigns ALL initial balances) ---
    def _create_or_load_node_wallet(self) -> Wallet:
        # TODO: Load/Save primary key from a secure location instead of generating each time
        logging.info(f"Node {self.id}: Generating primary node wallet...")
        wallet = Wallet()
        addr = wallet.get_address()
        with self.balance_lock: # Use balance lock to update all related dicts atomically
             self.managed_wallets[addr] = wallet
             self.usd_balances[addr] = INITIAL_USD_BALANCE
             self.carbon_balances[addr] = INITIAL_CARBON_BALANCE # <<< Assign Carbon too
        logging.info(f"Node {self.id}: Primary Wallet Addr: {addr[:10]} (USD: {INITIAL_USD_BALANCE:.2f}, CARBON: {INITIAL_CARBON_BALANCE:.2f})")
        return wallet

    def create_managed_wallet(self) -> Wallet:
        """Creates a new wallet managed by this node with initial USD and Carbon balances."""
        wallet = Wallet()
        addr = wallet.get_address()
        with self.balance_lock: # Use balance lock
             if addr in self.managed_wallets:
                  logging.warning(f"Node {self.id}: Wallet {addr[:10]} already managed.")
                  return self.managed_wallets[addr] # Avoid duplicates, return existing
             self.managed_wallets[addr] = wallet
             self.usd_balances[addr] = INITIAL_USD_BALANCE
             self.carbon_balances[addr] = INITIAL_CARBON_BALANCE # <<< Assign Carbon too
        logging.info(f"Node {self.id}: Created wallet: {addr[:10]} (USD: {INITIAL_USD_BALANCE:.2f}, CARBON: {INITIAL_CARBON_BALANCE:.2f})")
        return wallet

    def get_managed_wallet(self, address: str) -> Optional[Wallet]:
        """Retrieves a managed wallet object by its address."""
        with self.balance_lock: # Reading managed_wallets requires lock
            return self.managed_wallets.get(address)

    def get_all_managed_wallet_addresses(self) -> List[str]:
        """Returns a list of addresses for all wallets managed by this node."""
        with self.balance_lock: # Reading managed_wallets requires lock
            return list(self.managed_wallets.keys())

    # --- Off-Chain Balance Methods ---
    def get_usd_balance(self, address: str) -> float:
        """Gets the off-chain USD balance for a given address managed by this node."""
        with self.balance_lock: # Reading requires lock
            return self.usd_balances.get(address, 0.0)

    def get_carbon_balance(self, address: str) -> float:
        """Gets the off-chain Carbon token balance for a given address managed by this node."""
        with self.balance_lock: # Reading requires lock
            return self.carbon_balances.get(address, 0.0)

    def get_all_usd_balances(self) -> Dict[str, float]:
        """Gets a copy of all off-chain USD balances managed by this node."""
        with self.balance_lock: # Reading requires lock
            return self.usd_balances.copy()

    def get_all_carbon_balances(self) -> Dict[str, float]:
        """Gets a copy of all off-chain Carbon token balances managed by this node."""
        with self.balance_lock: # Reading requires lock
            return self.carbon_balances.copy()

    def _update_balance(self, balance_dict: Dict[str, float], address: str, amount_change: float) -> bool:
        """
        Generic internal helper to update an off-chain balance (USD or Carbon).
        Assumes balance_lock is held by the caller. Checks for sufficient funds before debiting.
        Returns True on success, False on failure (insufficient funds).
        """
        # Requires balance_lock to be held by the caller
        current_balance = balance_dict.get(address, 0.0)
        # Check for sufficient funds *before* calculating new balance if it's a debit
        if amount_change < 0 and current_balance < abs(amount_change) - 0.00000001: # Tolerance for float compare
            return False # Insufficient funds

        # Use round to mitigate potential floating point inaccuracies
        new_balance = round(current_balance + amount_change, 8) # 8 decimal places precision
        # Double check after calculation (should be redundant if check above works, but safe)
        if new_balance < -0.00000001:
             # This case should ideally not be reached if the pre-check is correct
             logging.error(f"Internal Error: Balance for {address[:10]} would drop below zero ({new_balance:.8f}) despite pre-check. Aborting update.")
             return False
        balance_dict[address] = new_balance
        return True

    # --- Order Book Methods (Multi-Asset) ---
    def add_order(self, order: Order) -> bool:
        """Adds a new order to the appropriate market's order book."""
        # --- Validation ---
        if not isinstance(order, Order):
            logging.warning(f"Node {self.id}: Rejected item that is not an Order object.")
            return False
        if order.market_pair not in SUPPORTED_MARKETS:
             logging.warning(f"Node {self.id}: Rejected order {order.order_id[:8]} for unsupported market '{order.market_pair}'. Supported: {SUPPORTED_MARKETS}")
             return False
        if order.amount <= 0 or order.price <= 0:
            logging.warning(f"Node {self.id}: Rejected invalid order {order.order_id[:8]} (amount/price <= 0).")
            return False
        # Order must be placed by a wallet managed by this node for settlement to work
        if not self.get_managed_wallet(order.wallet_address):
            logging.warning(f"Node {self.id}: Rejected order {order.order_id[:8]} from unmanaged wallet {order.wallet_address[:10]}. Settlement would fail.")
            return False

        # --- Basic Fund Pre-Check (Optional but Recommended) ---
        # This helps reject orders early but adds complexity due to locking.
        # Checking native balance here is tricky as it requires chain_lock.
        funds_ok = False
        with self.balance_lock: # Need lock to check off-chain balances
            if order.order_type == 'buy':
                  # Buyer needs Quote currency (e.g., USD)
                  quote_asset = order.market_pair.split('-')[1]
                  required_quote = round(order.amount * order.price, 8)
                  current_quote_balance = 0.0
                  if quote_asset == "USD":
                      current_quote_balance = self.usd_balances.get(order.wallet_address, 0.0)
                  # Add other quote assets if needed

                  if current_quote_balance >= required_quote - 0.00000001: # Float tolerance
                      funds_ok = True
                  else:
                      logging.warning(f"Order Reject: Insufficient {quote_asset} ({current_quote_balance:.8f} < {required_quote:.8f}) for BUY order {order.order_id[:8]} by {order.wallet_address[:10]}")

            elif order.order_type == 'sell':
                  # Seller needs Base currency (e.g., CARBON or NATIVE)
                  base_asset = order.market_pair.split('-')[0]
                  required_base = order.amount
                  current_base_balance = 0.0

                  if base_asset == "CARBON":
                      current_base_balance = self.carbon_balances.get(order.wallet_address, 0.0)
                      if current_base_balance >= required_base - 0.00000001: # Float tolerance
                         funds_ok = True
                      else:
                         logging.warning(f"Order Reject: Insufficient {base_asset} ({current_base_balance:.8f} < {required_base:.8f}) for SELL order {order.order_id[:8]} by {order.wallet_address[:10]}")

                  elif base_asset == "NATIVE":
                      # Checking Native balance accurately requires chain_lock + utxo lookup.
                      # This is complex to do *here* without potential deadlocks if chain_lock is held elsewhere.
                      # For simplicity, we SKIP the pre-check for NATIVE sells.
                      # The check will happen during settlement attempt (_settle_match).
                      funds_ok = True # Assume OK for now, checked later
                      logging.debug(f"Skipping pre-check for NATIVE sell order {order.order_id[:8]}. Funds checked at settlement.")

                  # Add other base assets if needed

        if not funds_ok:
            return False # Fund check failed

        # --- Add Order to Heap ---
        with self.order_book_lock: # Lock the order book structure
             # Get the specific book for the market pair
             if order.market_pair not in self.order_books:
                 # This check should be redundant due to SUPPORTED_MARKETS check, but belt-and-suspenders
                 logging.error(f"Node {self.id}: Internal error - market pair {order.market_pair} not found in order_books dict.")
                 return False
             book = self.order_books[order.market_pair]

             if order.order_type == 'buy':
                 # Use negative price for max heap behavior (highest price first)
                 heapq.heappush(book['buys'], (-order.price, order.timestamp, order))
             elif order.order_type == 'sell':
                 # Use positive price for min heap behavior (lowest price first)
                 heapq.heappush(book['sells'], (order.price, order.timestamp, order))
             # Should not happen due to earlier validation:
             # else: logging.warning(...); return False

             logging.info(f"Node {self.id}: Added {order.order_type.upper()} Order {order.order_id[:8]} ({order.market_pair}: {order.amount:.8f} @ {order.price:.2f}) for {order.wallet_address[:10]}")

        # Optionally trigger matching immediately (can increase CPU usage)
        # threading.Thread(target=self._match_market, args=(order.market_pair,)).start()
        return True

    def get_order_book_snapshot(self, market_pair: str) -> Optional[Dict[str, List[Dict]]]:
        """Returns a display snapshot of a specific market's order book."""
        if market_pair not in self.order_books:
            logging.warning(f"Node {self.id}: Requested snapshot for unsupported market '{market_pair}'")
            return None

        with self.order_book_lock:
            # Check again inside lock in case market was removed dynamically (not currently possible)
            if market_pair not in self.order_books: return None
            book = self.order_books[market_pair]

            # Create sorted lists from heap data for display, filtering filled orders
            active_buys = [(price, ts, o) for price, ts, o in book['buys'] if not o.is_filled()]
            active_sells = [(price, ts, o) for price, ts, o in book['sells'] if not o.is_filled()]

            # Sort buys: highest price first (-price is used), then oldest timestamp
            sorted_buys = sorted(active_buys, key=lambda x: (x[0], x[1]), reverse=False) # Max heap -> sort normally on (-price, ts)
            # Sort sells: lowest price first, then oldest timestamp
            sorted_sells = sorted(active_sells, key=lambda x: (x[0], x[1]))

        # Convert orders to dictionaries
        buy_dicts = [order.to_dict() for _, _, order in sorted_buys]
        sell_dicts = [order.to_dict() for _, _, order in sorted_sells]

        return {"buy_orders": buy_dicts, "sell_orders": sell_dicts}

    def _match_orders_loop(self):
        """Background thread target: Periodically attempts to match orders across all markets."""
        logging.info(f"Node {self.id}: Order matching thread started (Interval: {ORDER_BOOK_MATCH_INTERVAL}s).")
        while not self.stop_order_matching_flag.is_set():
            start_time = time.time()
            try:
                # Iterate through each supported market's order book
                # Copy keys to avoid issues if SUPPORTED_MARKETS changes mid-iteration (unlikely here)
                markets_to_match = list(self.order_books.keys())
                # Optional: Randomize order to avoid starving markets?
                # random.shuffle(markets_to_match)
                for market in markets_to_match:
                     if self.stop_order_matching_flag.is_set(): break # Check flag between markets
                     self._match_market(market) # Attempt matching for this specific market
            except Exception as e:
                logging.error(f"Node {self.id}: Unhandled error in order matching loop: {e}", exc_info=True)

            if self.stop_order_matching_flag.is_set(): break # Check flag after all markets are processed

            # Calculate time elapsed and wait for the remainder of the interval
            elapsed_time = time.time() - start_time
            wait_time = max(0, ORDER_BOOK_MATCH_INTERVAL - elapsed_time)
            # Wait efficiently using the event's wait method
            self.stop_order_matching_flag.wait(wait_time)

        logging.info(f"Node {self.id}: Order matching thread stopped.")

    def _match_market(self, market_pair: str):
        """Matches orders within a specific market's order book."""
        # This function assumes it might be called concurrently for different markets if needed,
        # therefore it needs the order_book_lock.
        with self.order_book_lock:
             # Check market exists within lock
             if market_pair not in self.order_books:
                 logging.warning(f"Attempted to match non-existent market '{market_pair}'")
                 return
             book = self.order_books[market_pair]
             buys = book['buys'] # Max heap on (-price, ts)
             sells = book['sells'] # Min heap on ( price, ts)

             matches_made_this_cycle = 0
             # --- Matching Loop ---
             while buys and sells:
                 # Peek at best orders (highest bid, lowest ask)
                 best_buy_price_neg, buy_ts, best_buy_order = buys[0]
                 best_sell_price, sell_ts, best_sell_order = sells[0]
                 best_buy_price = -best_buy_price_neg # Convert back to positive

                 # Clean up filled orders at the top of the heaps defensively
                 cleaned_heap = False
                 if best_buy_order.is_filled():
                     heapq.heappop(buys)
                     cleaned_heap = True
                 if best_sell_order.is_filled():
                     heapq.heappop(sells)
                     cleaned_heap = True
                 if cleaned_heap:
                      continue # Restart loop iteration to get fresh top orders

                 # --- Check for Price Cross ---
                 if best_buy_price >= best_sell_price:
                     # Match Found!
                     # Determine execution price (simple model: use maker's price - the price that was there first)
                     # execution_price = best_buy_price if buy_ts < sell_ts else best_sell_price
                     # Simpler model: Use the seller's asking price (aggressor matches passive)
                     execution_price = best_sell_price

                     # Determine quantity
                     buy_remaining = best_buy_order.remaining_amount()
                     sell_remaining = best_sell_order.remaining_amount()
                     match_quantity = min(buy_remaining, sell_remaining)

                     # Avoid near-zero matches
                     if match_quantity <= 0.00000001:
                         logging.warning(f"Match quantity near zero ({match_quantity}) for {market_pair}. Skipping.")
                         # Should remove the effectively filled order(s)
                         if buy_remaining <= 0.00000001: heapq.heappop(buys)
                         if sell_remaining <= 0.00000001: heapq.heappop(sells)
                         continue # Try next potential match

                     logging.info(f"MATCH ({market_pair}): Buy {best_buy_order.order_id[:8]} ({buy_remaining:.4f}@{best_buy_price:.2f}) meets Sell {best_sell_order.order_id[:8]} ({sell_remaining:.4f}@{best_sell_price:.2f})")
                     logging.info(f"  -> Executing {match_quantity:.8f} Base @ {execution_price:.2f} Quote")

                     # --- Attempt Settlement (The critical part) ---
                     # This needs to be ATOMIC or have reliable rollback.
                     # The current _settle_match has limitations.
                     settled = self._settle_match(
                         best_buy_order, best_sell_order, match_quantity, execution_price
                     )

                     if settled:
                          matches_made_this_cycle += 1
                          # Update fill amounts on the order objects
                          best_buy_order.filled += match_quantity
                          best_sell_order.filled += match_quantity
                          logging.info(f"  -> Fill Update: Buy Order {best_buy_order.order_id[:8]} now {best_buy_order.filled:.8f}/{best_buy_order.amount:.8f}")
                          logging.info(f"  -> Fill Update: Sell Order {best_sell_order.order_id[:8]} now {best_sell_order.filled:.8f}/{best_sell_order.amount:.8f}")

                          # Remove fully filled orders from the heap
                          if best_buy_order.is_filled():
                              logging.info(f"  -> Buy Order {best_buy_order.order_id[:8]} fully filled. Removing.")
                              heapq.heappop(buys)
                          if best_sell_order.is_filled():
                              logging.info(f"  -> Sell Order {best_sell_order.order_id[:8]} fully filled. Removing.")
                              heapq.heappop(sells)
                          # Continue matching in the while loop
                     else:
                          # Settlement failed (e.g., insufficient funds discovered during settlement)
                          logging.warning(f"Settlement FAILED for match in {market_pair} between {best_buy_order.order_id[:8]} and {best_sell_order.order_id[:8]}. Stopping matching for this market cycle.")
                          # Problematic order(s) remain in the book.
                          # A more robust system might:
                          #   - Remove the order causing the failure (e.g., insufficient funds).
                          #   - Temporarily suspend the user's trading.
                          #   - Log detailed error for investigation.
                          # For simplicity, we just break the inner loop for this market.
                          break # Stop trying to match in THIS market for THIS cycle

                 else:
                     # No price cross (best_buy_price < best_sell_price)
                     # Highest bid is lower than lowest ask, no more matches possible.
                     break # Exit the while loop for this market cycle
             # End of while loop (matching attempts for this market)

             #if matches_made_this_cycle > 0:
             #     logging.debug(f"Finished matching cycle for {market_pair}, {matches_made_this_cycle} matches settled.")


    def _settle_match(self, buy_order: Order, sell_order: Order, quantity: float, price: float) -> bool:
        """
        Attempts to atomically settle a matched order involving off-chain balances (USD, Carbon)
        and potentially an on-chain component (Native coin fee or transfer).

        Handles locking of balances and potentially the chain/UTXO set.
        Submits an on-chain transaction for the fee component.

        **LIMITATIONS:**
        - True atomicity across off-chain DB/memory and on-chain state is hard.
        - Assumes taker (aggressor order) pays the native coin fee.
        - Native coin *transfers* as part of the settlement are NOT fully implemented here;
          only off-chain assets (USD, Carbon) are transferred directly, plus an on-chain fee.
        - Rollback on failure is complex and potentially manual.

        Returns True if settlement *appears* successful (balances updated, fee tx submitted).
        Returns False if any critical step fails.
        """
        market = buy_order.market_pair # e.g., "CARBON-USD" or "NATIVE-USD"
        try:
            base_asset, quote_asset = market.split('-') # e.g., ("CARBON", "USD")
        except ValueError:
            logging.error(f"Settle Fail: Invalid market pair format '{market}'")
            return False

        quote_amount = round(quantity * price, 8) # Total amount of quote asset (e.g., USD)
        native_fee = DEFAULT_TX_FEE # Fee for the on-chain transaction part

        buyer_addr = buy_order.wallet_address
        seller_addr = sell_order.wallet_address

        logging.debug(f"Attempting settlement: {quantity:.8f} {base_asset} for {quote_amount:.8f} {quote_asset} between Buyer:{buyer_addr[:6]} and Seller:{seller_addr[:6]} | Fee: {native_fee} NATIVE")

        # --- Acquire Locks: Balance lock first, then chain lock if needed ---
        # This order helps prevent deadlocks if other operations lock chain then balance.
        with self.balance_lock:
             # --- Phase 1: Check Funds and Prepare ---
             # Check Quote Asset (e.g., USD) for Buyer
             quote_debit_ok = False
             if quote_asset == "USD":
                  if self.usd_balances.get(buyer_addr, 0.0) >= quote_amount - 0.00000001:
                       quote_debit_ok = True
                  else: logging.warning(f"Settle Fail Check: Buyer {buyer_addr[:10]} insufficient {quote_asset} ({self.usd_balances.get(buyer_addr, 0.0):.8f} < {quote_amount:.8f})")
             # Add checks for other quote assets if necessary
             if not quote_debit_ok: return False # Abort early

             # Check Base Asset (e.g., Carbon, Native) for Seller
             base_debit_ok = False
             if base_asset == "CARBON":
                  if self.carbon_balances.get(seller_addr, 0.0) >= quantity - 0.00000001:
                       base_debit_ok = True
                  else: logging.warning(f"Settle Fail Check: Seller {seller_addr[:10]} insufficient {base_asset} ({self.carbon_balances.get(seller_addr, 0.0):.8f} < {quantity:.8f})")
             elif base_asset == "NATIVE":
                  # Cannot reliably check Native balance here without chain_lock.
                  # Assume OK for now; the fee tx creation below will implicitly check.
                  base_debit_ok = True # Placeholder - relies on fee tx succeeding
                  logging.debug(f"Native base asset check deferred to fee transaction creation.")
             # Add checks for other base assets if necessary
             if not base_debit_ok: return False # Abort early

             # Check Native Asset for Fee payment (Assume buyer pays fee)
             fee_payer_wallet = self.managed_wallets.get(buyer_addr)
             if not fee_payer_wallet:
                  logging.error(f"Settle Fail Check: Cannot find managed wallet for fee payer {buyer_addr[:10]}")
                  return False

             # --- Phase 2: Execute Off-Chain Balance Updates and Prepare On-Chain Tx ---
             # We need chain_lock now if Native coin is involved (for fee tx creation)
             # Acquire chain_lock *inside* balance_lock context
             with self.chain_lock:
                  # Create Fee Transaction (Requires UTXO set access)
                  # This implicitly checks if the buyer has enough native coin for the fee.
                  # Fee sent to the node's primary wallet (can be changed to burn address etc.)
                  fee_tx = fee_payer_wallet.create_transaction(
                      recipient_address_or_anon_id=self.node_wallet.get_address(), # Fee recipient
                      amount=0.0, # Fee-only transaction
                      fee=native_fee,
                      utxo_set=self.utxo_set, # Requires chain_lock
                      use_anonymous_sender=False # Fee tx should be clear
                  )

                  if not fee_tx:
                       logging.warning(f"Settle Fail: Buyer {buyer_addr[:10]} could not create fee transaction (Insufficient NATIVE for fee {native_fee}?)")
                       # No balances changed yet, just return False
                       return False

                  # *** Critical Section: Perform updates ***
                  # If fee_tx is created, we assume buyer has native fee. Now attempt balance changes.
                  # If any of these fail now (they shouldn't due to pre-checks), rollback is needed.

                  # 1. Debit Buyer Quote Asset (e.g., USD)
                  quote_debit_success = False
                  if quote_asset == "USD":
                      quote_debit_success = self._update_balance(self.usd_balances, buyer_addr, -quote_amount)
                  # Add other quote assets if needed
                  if not quote_debit_success:
                       logging.error("CRITICAL SETTLE FAIL: Failed to debit buyer QUOTE asset after pre-check!")
                       # Rollback attempt (tricky): Fee TX was created but not submitted.
                       return False # Indicates critical failure state

                  # 2. Debit Seller Base Asset (e.g., Carbon)
                  base_debit_success = False
                  if base_asset == "CARBON":
                      base_debit_success = self._update_balance(self.carbon_balances, seller_addr, -quantity)
                  elif base_asset == "NATIVE":
                      # Native coin transfer requires *another* transaction, not implemented here.
                      # For now, assume fee tx covers the "cost". This is incorrect for a real DEX.
                      base_debit_success = True # Assume OK
                      logging.warning("Native base asset debit skipped - settlement model incomplete.")
                  # Add other base assets if needed
                  if not base_debit_success:
                      logging.error(f"CRITICAL SETTLE FAIL: Failed to debit seller BASE asset ({base_asset}) after pre-check!")
                      # Attempt Rollback: Credit buyer quote asset back
                      if quote_asset == "USD": self._update_balance(self.usd_balances, buyer_addr, quote_amount)
                      return False # Indicates critical failure state

                  # 3. Credit Seller Quote Asset (e.g., USD)
                  quote_credit_success = False
                  if quote_asset == "USD":
                      quote_credit_success = self._update_balance(self.usd_balances, seller_addr, quote_amount)
                  # Add other quote assets if needed
                  if not quote_credit_success:
                       logging.error("CRITICAL SETTLE FAIL: Failed to credit seller QUOTE asset!")
                       # Attempt Rollback: Debit seller base, Credit buyer quote
                       if base_asset == "CARBON": self._update_balance(self.carbon_balances, seller_addr, quantity)
                       if quote_asset == "USD": self._update_balance(self.usd_balances, buyer_addr, quote_amount)
                       return False

                  # 4. Credit Buyer Base Asset (e.g., Carbon)
                  base_credit_success = False
                  if base_asset == "CARBON":
                      base_credit_success = self._update_balance(self.carbon_balances, buyer_addr, quantity)
                  elif base_asset == "NATIVE":
                      # Native credit requires on-chain tx, not done here.
                      base_credit_success = True # Assume OK
                      logging.warning("Native base asset credit skipped - settlement model incomplete.")
                  # Add other base assets if needed
                  if not base_credit_success:
                       logging.error("CRITICAL SETTLE FAIL: Failed to credit buyer BASE asset!")
                       # Attempt Rollback: Debit buyer base, Debit seller quote, Credit seller base, Credit buyer quote... complex!
                       # Simplest partial rollback:
                       if quote_asset == "USD": self._update_balance(self.usd_balances, seller_addr, -quote_amount) # Remove seller credit
                       if base_asset == "CARBON": self._update_balance(self.carbon_balances, seller_addr, quantity) # Return seller base
                       if quote_asset == "USD": self._update_balance(self.usd_balances, buyer_addr, quote_amount) # Return buyer quote
                       return False

                  # --- Phase 3: Submit On-Chain Transaction ---
                  # If all balance updates succeeded, submit the fee transaction.
                  submitted = self.submit_and_broadcast_transaction(fee_tx)
                  if not submitted:
                       logging.error(f"CRITICAL SETTLE FAIL: Fee tx {fee_tx.transaction_id[:10]} REJECTED by mempool after balance updates!")
                       # Attempt Rollback of all balance changes - very complex and error-prone
                       # Mark settlement as failed. Manual intervention might be needed.
                       # Simplest rollback (may fail if balances were spent):
                       try:
                           if base_asset == "CARBON": self._update_balance(self.carbon_balances, buyer_addr, -quantity)
                           if quote_asset == "USD": self._update_balance(self.usd_balances, seller_addr, -quote_amount)
                           if base_asset == "CARBON": self._update_balance(self.carbon_balances, seller_addr, quantity)
                           if quote_asset == "USD": self._update_balance(self.usd_balances, buyer_addr, quote_amount)
                           logging.warning("Attempted rollback of balances due to fee tx submission failure.")
                       except Exception as rollback_e:
                           logging.error(f"CRITICAL: Rollback attempt failed: {rollback_e}")
                       return False # Settlement ultimately failed

                  # If fee tx submitted successfully
                  logging.info(f"Settlement successful for match: {quantity:.8f} {base_asset} @ {price:.2f} {quote_asset}. Fee Tx Submitted: {fee_tx.transaction_id[:10]}")
                  return True
             # End chain_lock context
        # End balance_lock context

    def start_order_matching(self):
        """Starts the background thread for order matching across all markets."""
        if not self.order_matching_thread or not self.order_matching_thread.is_alive():
             self.stop_order_matching_flag.clear()
             self.order_matching_thread = threading.Thread(target=self._match_orders_loop, name=f"OrderMatcher-{self.id}", daemon=True)
             self.order_matching_thread.start()
             logging.info(f"Node {self.id}: Started order matching thread.")
        else:
             logging.warning(f"Node {self.id}: Order matching thread already running.")

    def stop_order_matching(self):
        """Signals the order matching thread to stop and waits for it to join."""
        if self.order_matching_thread and self.order_matching_thread.is_alive():
             logging.info(f"Node {self.id}: Stopping order matching thread...")
             self.stop_order_matching_flag.set()
             # Wait slightly longer than the interval to ensure it checks the flag
             self.order_matching_thread.join(timeout=ORDER_BOOK_MATCH_INTERVAL + 2.0)
             if self.order_matching_thread.is_alive():
                  logging.warning(f"Node {self.id}: Order matching thread join timed out.")
             else:
                  logging.info(f"Node {self.id}: Order matching thread stopped.")
             self.order_matching_thread = None
        # else: logging.debug(f"Node {self.id}: Order matching thread not running.")


    # --- Mining Methods (Mostly Unchanged) ---
    def _mining_loop(self):
        node_reward_address = self.node_wallet.get_address()
        logging.info(f"Node {self.id}: Mining thread started (Reward Addr: {node_reward_address[:10]}...).")
        while not self.stop_mining_flag.is_set():
            last_block_copy = None
            current_utxo_copy = None
            with self.chain_lock:
                 current_utxo_copy = self.utxo_set.get_copy()
                 last_block_copy = self.chain.get_last_block()

            if not last_block_copy:
                logging.debug(f"Node {self.id}: Mining paused - waiting for first block.")
                if self.stop_mining_flag.wait(5): break # Wait interruptibly
                continue

            new_block = miner.mine_new_block(
                self.mempool, current_utxo_copy, self.chain, node_reward_address, self.consensus
            )

            if self.stop_mining_flag.is_set(): break

            if new_block:
                block_added = False
                with self.chain_lock:
                    block_added = self.chain.add_block(new_block, self.utxo_set)

                if block_added:
                    logging.info(f"Node {self.id}: MINED and added new block {new_block.index} (Tx:{len(new_block.transactions)}, Nonce:{new_block.nonce}, Hash:{new_block.hash[:10]})")
                    block_msg = create_message(MessageType.NEW_BLOCK, payload=new_block.to_dict())
                    self.p2p_node.broadcast(block_msg)
                    mined_tx_ids = [tx.transaction_id for tx in new_block.transactions if not tx.is_coinbase()]
                    self.mempool.remove_transactions(mined_tx_ids)
                else:
                     logging.warning(f"Node {self.id}: Mined block {new_block.index} but rejected by local chain (likely outdated).")
            else:
                # Wait before trying again if no block mined
                 wait_time = random.uniform(1.0, 3.0)
                 if self.stop_mining_flag.wait(wait_time): break # Wait interruptibly

        logging.info(f"Node {self.id}: Mining thread finished.")

    def start_mining(self):
        if self.is_mining: return
        self.is_mining = True; self.stop_mining_flag.clear()
        self.mining_thread = threading.Thread(target=self._mining_loop, name=f"Miner-{self.id}", daemon=True); self.mining_thread.start()
        logging.info(f"Node {self.id}: Started mining.")

    def stop_mining(self):
        if not self.is_mining or not self.mining_thread: return
        logging.info(f"Node {self.id}: Signaling mining thread to stop..."); self.stop_mining_flag.set()
        self.mining_thread.join(timeout=5.0)
        if self.mining_thread.is_alive(): logging.warning(f"Node {self.id}: Mining thread join timed out.")
        else: logging.info(f"Node {self.id}: Mining thread stopped.")
        self.is_mining = False; self.mining_thread = None


    # --- Network Handling (Mostly Unchanged) ---
    def _handle_network_message(self, peer_addr_tuple: tuple[str, int], message: Dict[str, Any]):
        peer_id_str = f"{peer_addr_tuple[0]}:{peer_addr_tuple[1]}"
        try:
            msg_type_val = message.get("type"); payload = message.get("payload")
            msg_type = MessageType(msg_type_val) if msg_type_val is not None else None
            if not msg_type: logging.warning(f"Node {self.id}: Received msg with no type from {peer_id_str}"); return

            logging.debug(f"Node {self.id}: Received {msg_type.name} from {peer_id_str}")

            if msg_type == MessageType.NEW_TRANSACTION and payload:
                try:
                    tx = Transaction.from_dict(payload)
                    if self.mempool.add_transaction(tx):
                         logging.info(f"Node {self.id}: Added tx {tx.transaction_id[:10]} from {peer_id_str} to mempool.")
                         tx_msg = create_message(MessageType.NEW_TRANSACTION, payload=tx.to_dict())
                         self.p2p_node.broadcast(tx_msg, exclude_peer=peer_addr_tuple)
                except Exception as e: logging.error(f"Error processing NEW_TRANSACTION from {peer_id_str}: {e}", exc_info=False) # Less verbose log

            elif msg_type == MessageType.NEW_BLOCK and payload:
                try:
                    block = Block.from_dict(payload)
                    block_accepted = False
                    with self.chain_lock: block_accepted = self.chain.add_block(block, self.utxo_set)
                    if block_accepted:
                         logging.info(f"Node {self.id}: Accepted block {block.index} (Hash: {block.hash[:10]}) from {peer_id_str}.")
                         tx_ids = [tx.transaction_id for tx in block.transactions if not tx.is_coinbase()]; self.mempool.remove_transactions(tx_ids)
                         block_msg = create_message(MessageType.NEW_BLOCK, payload=block.to_dict()); self.p2p_node.broadcast(block_msg, exclude_peer=peer_addr_tuple)
                         if self.is_mining: # Restart mining if accepting external block
                              logging.info("Received new block, restarting mining.")
                              self.stop_mining(); self.start_mining()
                except Exception as e: logging.error(f"Error processing NEW_BLOCK from {peer_id_str}: {e}", exc_info=False) # Less verbose log

            elif msg_type == MessageType.GET_PEERS:
                 peer_list = self.p2p_node.get_peer_list(); peer_list_str = [f"{h}:{p}" for h, p in peer_list]
                 resp = create_message(MessageType.SEND_PEERS, payload={"peers": peer_list_str}); self.p2p_node.send_message(peer_addr_tuple, resp)

            elif msg_type == MessageType.SEND_PEERS and payload and "peers" in payload:
                 for peer_str in payload.get("peers", []):
                      try:
                          host, port_str = peer_str.split(':'); port = int(port_str)
                          self.p2p_node.connect_to_peer(host, port)
                      except Exception: pass # Ignore errors connecting to peers from list

            elif msg_type == MessageType.PING: self.p2p_node.send_message(peer_addr_tuple, create_message(MessageType.PONG))
            elif msg_type == MessageType.PONG: pass # Handle if needed (e.g., latency checks)

            elif msg_type == MessageType.GET_UTXOS and payload and "address" in payload:
                 addr = payload["address"]; utxos_payload = {}
                 with self.chain_lock: utxos = self.utxo_set.find_utxos_for_address(addr)
                 for (tid, idx), out in utxos.items(): utxos_payload[f"{tid}:{idx}"] = out.to_dict()
                 resp = create_message(MessageType.SEND_UTXOS, payload={"address": addr, "utxos": utxos_payload}); self.p2p_node.send_message(peer_addr_tuple, resp)

            elif msg_type == MessageType.GET_ALL_BALANCES: # Native balances
                  with self.chain_lock: balances = self.get_all_native_balances() # Renamed internal method
                  resp = create_message(MessageType.SEND_ALL_BALANCES, payload={"balances": balances}); self.p2p_node.send_message(peer_addr_tuple, resp)

            # Handle other message types... SEND_UTXOS, SEND_ALL_BALANCES etc. if needed

        except Exception as e: logging.error(f"Node {self.id}: Error handling msg from {peer_id_str}: {e}", exc_info=True)

    # --- Native Transaction Creation (Unchanged) ---
    def create_transaction_from_managed_wallet(
        self, sender_address: str, recipient_address_or_anon_id: str, amount: float, fee: float, use_anonymity: bool = False
    ) -> Optional[Transaction]:
        sender_wallet = self.get_managed_wallet(sender_address) # Uses balance_lock
        if not sender_wallet:
            logging.error(f"Node {self.id}: Sender wallet {sender_address[:10]}... not managed.")
            return None
        logging.info(f"Node {self.id}: Creating native tx: {amount:.8f} + {fee:.8f} fee from {sender_address[:10]} -> {recipient_address_or_anon_id[:10]} (Anon: {use_anonymity})...")
        tx = None
        with self.chain_lock: # Lock needed to access UTXO set
             try:
                 tx = sender_wallet.create_transaction(
                      recipient_address_or_anon_id=recipient_address_or_anon_id,
                      amount=amount, fee=fee, utxo_set=self.utxo_set, use_anonymous_sender=use_anonymity
                 )
                 if not tx: logging.warning(f"Node {self.id}: Failed to create native tx from {sender_address[:10]} (Insufficient funds or no UTXOs?).")
             except Exception as e: logging.error(f"Exception during tx creation for {sender_address[:10]}: {e}", exc_info=True)
        return tx

    def submit_and_broadcast_transaction(self, transaction: Transaction) -> bool:
         """Adds a transaction to the local mempool and broadcasts it to peers."""
         # Ensure transaction object is valid before proceeding
         if not isinstance(transaction, Transaction):
              logging.error(f"Node {self.id}: Attempted to submit invalid object as transaction: {type(transaction)}")
              return False
         # Validate tx with mempool rules before broadcasting
         if self.mempool.add_transaction(transaction):
              tx_msg = create_message(MessageType.NEW_TRANSACTION, payload=transaction.to_dict())
              self.p2p_node.broadcast(tx_msg)
              logging.info(f"Node {self.id}: Tx {transaction.transaction_id[:10]} added to mempool & broadcast.")
              return True
         else:
              logging.warning(f"Node {self.id}: Tx {transaction.transaction_id[:10]} rejected by mempool.")
              return False

    # --- Native Balance Methods (On-Chain) ---
    def get_native_balance(self, address_or_anon_id: Optional[str] = None) -> float:
        """Gets the confirmed native coin balance for a specific address or anonymous ID."""
        target_id = address_or_anon_id if address_or_anon_id else self.node_wallet.get_address()
        with self.chain_lock: # Reading UTXO set needs lock
            balance = self.utxo_set.get_balance(target_id)
        return balance

    def get_all_native_balances(self) -> Dict[str, float]:
        """Calculates confirmed native coin balances for all known addresses/IDs in the UTXO set."""
        balances = {}
        with self.chain_lock: # Reading UTXO set needs lock
            known_scripts = set(out.lock_script for out in self.utxo_set.utxos.values())
            # logging.debug(f"Calculating native balances for {len(known_scripts)} scripts.")
            for script in known_scripts:
                balances[script] = self.utxo_set.get_balance(script)
        return balances

    # --- Status Method ---
    def get_status(self) -> Dict[str, Any]:
         """Returns a dictionary containing the current status of the node."""
         with self.chain_lock:
             chain_len = len(self.chain.blocks); utxo_count = len(self.utxo_set)
             node_native_balance = self.get_native_balance(self.node_wallet.get_address()) # Use internal method

         with self.balance_lock:
              managed_wallet_count = len(self.managed_wallets)
              node_usd_balance = self.usd_balances.get(self.node_wallet.get_address(), 0.0)
              node_carbon_balance = self.carbon_balances.get(self.node_wallet.get_address(), 0.0)

         order_book_stats = {}
         with self.order_book_lock:
             for market, book in self.order_books.items():
                  order_book_stats[market] = {
                       "buy_orders": len([o for _, _, o in book['buys'] if not o.is_filled()]),
                       "sell_orders": len([o for _, _, o in book['sells'] if not o.is_filled()])
                  }

         mempool_size = self.mempool.get_transaction_count()
         peer_count = self.p2p_node.get_peer_count()

         status = {
             "node_id": self.id, "node_address": self.node_wallet.get_address(),
             "is_mining": self.is_mining, "peer_count": peer_count,
             "chain_length": chain_len, "mempool_size": mempool_size, "utxo_count": utxo_count,
             "managed_wallet_count": managed_wallet_count,
             "balances": { # Group balances
                  "native": node_native_balance,
                  "usd": node_usd_balance,
                  "carbon": node_carbon_balance,
             },
             "order_books": order_book_stats # Market-specific counts
         }
         return status

    # --- Lifecycle Methods ---
    def start(self):
        """Starts the node's services: P2P networking, order matching, and connects to bootstrap peers."""
        logging.info(f"Node {self.id}: Starting...")
        # Start P2P Networking
        logging.info(f"Node {self.id}: Starting P2P network...")
        self.p2p_node.start()
        time.sleep(1) # Allow P2P to initialize
        # Connect to Bootstrap Peers
        logging.info(f"Node {self.id}: Connecting to bootstrap peers: {self.bootstrap_peers}")
        for host, port in self.bootstrap_peers:
            self.p2p_node.connect_to_peer(host, port)
        # Start Order Matching Thread
        self.start_order_matching()
        logging.info(f"Node {self.id}: Startup sequence complete.")
        # Note: Mining is started separately via start_mining()

    def stop(self):
        """Stops the node's services gracefully."""
        logging.info(f"Node {self.id}: Stopping...")
        # Stop accepting new work / external interactions first
        self.stop_order_matching()
        self.stop_mining()
        # Stop network activity
        self.p2p_node.stop()
        # Persist state
        self.save_chain()
        # TODO: Persist other state: order book, USD/Carbon balances, managed wallet keys
        logging.info(f"Node {self.id}: Stopped.")

    def save_chain(self):
        """Saves the current blockchain state to its designated file."""
        logging.info(f"Node {self.id}: Saving chain to {self.chain_file}...")
        try:
            with self.chain_lock: # Ensure consistent state during save
                self.chain.save_to_file(self.chain_file)
            logging.info(f"Node {self.id}: Chain saved successfully.")
        except Exception as e:
            logging.error(f"Node {self.id}: Failed to save chain to {self.chain_file}: {e}", exc_info=True)

    # --- Revocation Method (Conceptual - Unchanged) ---
    def decrypt_revocation_token(self, token: str) -> Optional[str]:
        """Decrypts a revocation token using the GOVT key (defined in utils)."""
        logging.info(f"Node {self.id}: Attempting to decrypt revocation token...")
        try:
            # Assumes GOVT_SYMMETRIC_KEY is available in utils
            decrypted_data = utils.decrypt_data(utils.GOVT_SYMMETRIC_KEY, token)
            if decrypted_data:
                 logging.info(f"Node {self.id}: Revocation token decrypted successfully.")
                 return decrypted_data
            else:
                 logging.warning(f"Node {self.id}: Failed to decrypt revocation token (invalid token or key?).")
                 return None
        except Exception as e:
             logging.error(f"Node {self.id}: Error during revocation token decryption: {e}")
             return None
