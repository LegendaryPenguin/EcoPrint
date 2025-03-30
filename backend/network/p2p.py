import socket
import threading
import json
import time
from typing import Set, Dict, Optional, Callable, List, Any

from .message import MessageType, create_message, parse_message

class P2PNode:
    """Handles P2P network connections and message passing."""

    def __init__(self, host: str, port: int, node_id: str, message_handler: Callable[[str, Dict[str, Any]], None]):
        self.host = host
        self.port = port
        self.node_id = node_id # For logging/identification
        self.peers: Set[tuple[str, int]] = set() # (host, port)
        self.connections: Dict[tuple[str, int], socket.socket] = {}
        self.server_socket: Optional[socket.socket] = None
        self.running = False
        self.lock = threading.Lock() # Protect peers and connections
        self.message_handler = message_handler # Callback function in Node class
        self.listen_thread: Optional[threading.Thread] = None
        self.ping_thread: Optional[threading.Thread] = None


    def start(self):
        """Starts the server thread to listen for incoming connections."""
        if self.running:
            print(f"P2P ({self.node_id}): Already running.")
            return

        self.running = True
        try:
             self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
             self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
             self.server_socket.bind((self.host, self.port))
             self.server_socket.listen(5)
             print(f"P2P ({self.node_id}): Listening on {self.host}:{self.port}")

             self.listen_thread = threading.Thread(target=self._listen_for_connections, daemon=True)
             self.listen_thread.start()

             self.ping_thread = threading.Thread(target=self._ping_peers_loop, daemon=True)
             self.ping_thread.start()

        except OSError as e:
             print(f"P2P Error ({self.node_id}): Could not start listener on {self.host}:{self.port}. {e}")
             self.running = False
             if self.server_socket:
                  self.server_socket.close()


    def stop(self):
        """Stops the server and closes connections."""
        print(f"P2P ({self.node_id}): Stopping network...")
        self.running = False
        if self.server_socket:
            self.server_socket.close() # Will interrupt accept() in listener thread
            print(f"P2P ({self.node_id}): Server socket closed.")

        with self.lock:
            peers_to_close = list(self.connections.keys()) # Copy keys before iterating
            for peer_addr in peers_to_close:
                conn = self.connections.pop(peer_addr, None)
                if conn:
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                        conn.close()
                        print(f"P2P ({self.node_id}): Closed connection to {peer_addr}")
                    except OSError as e:
                        print(f"P2P Warning ({self.node_id}): Error closing connection to {peer_addr}: {e}")

        if self.listen_thread and self.listen_thread.is_alive():
             self.listen_thread.join(timeout=1.0)
        if self.ping_thread and self.ping_thread.is_alive():
             self.ping_thread.join(timeout=1.0)
        print(f"P2P ({self.node_id}): Network stopped.")


    def _listen_for_connections(self):
        """Thread target: Listens for and handles incoming connections."""
        while self.running and self.server_socket:
            try:
                conn, addr = self.server_socket.accept()
                conn.settimeout(60.0) # Set a timeout for operations
                peer_addr = (addr[0], addr[1]) # Use tuple for consistency
                print(f"P2P ({self.node_id}): Accepted connection from {peer_addr}")

                with self.lock:
                    if peer_addr not in self.connections: # Avoid duplicate connections?
                         self.peers.add(peer_addr)
                         self.connections[peer_addr] = conn
                         # Start a thread to handle messages from this specific peer
                         handler_thread = threading.Thread(target=self._handle_peer, args=(conn, peer_addr), daemon=True)
                         handler_thread.start()
                    else:
                         print(f"P2P ({self.node_id}): Already connected to {peer_addr}, closing new connection.")
                         conn.close()

            except OSError as e:
                # This likely happens when the socket is closed by stop()
                if self.running:
                     print(f"P2P Error ({self.node_id}): Listener error: {e}")
                break # Exit loop if socket closed or error
            except Exception as e:
                if self.running:
                    print(f"P2P Error ({self.node_id}): Unexpected error accepting connection: {e}")


    def _handle_peer(self, conn: socket.socket, peer_addr: tuple[str, int]):
        """Thread target: Receives and processes messages from a single peer."""
        buffer = ""
        while self.running:
            try:
                # Receive data in chunks
                data = conn.recv(4096)
                if not data:
                    # Connection closed by peer
                    print(f"P2P ({self.node_id}): Connection closed by peer {peer_addr}")
                    break

                buffer += data.decode('utf-8', errors='ignore')

                # Process complete messages (separated by newline)
                while '\n' in buffer:
                    message_str, buffer = buffer.split('\n', 1)
                    message = parse_message(message_str)
                    if message:
                        # Use the callback to handle the message in the Node class
                        try:
                            # Construct peer_id string for handler
                            peer_id = f"{peer_addr[0]}:{peer_addr[1]}"
                            self.message_handler(peer_id, message)
                        except Exception as e:
                             print(f"P2P Error ({self.node_id}): Error in message handler for peer {peer_addr}: {e}")

            except socket.timeout:
                # print(f"P2P ({self.node_id}): Socket timeout for peer {peer_addr}")
                continue # Just means no data received recently
            except OSError as e:
                # Connection likely broken or closed
                print(f"P2P Error ({self.node_id}): Socket error with peer {peer_addr}: {e}")
                break
            except Exception as e:
                print(f"P2P Error ({self.node_id}): Unexpected error handling peer {peer_addr}: {e}")
                break # Exit on unexpected errors

        # Cleanup connection when loop ends
        self._remove_peer(peer_addr, conn)


    def connect_to_peer(self, host: str, port: int):
        """Establishes an outgoing connection to a peer."""
        if not self.running: return
        peer_addr = (host, port)
        # Avoid connecting to self or already connected peers
        if peer_addr == (self.host, self.port): return
        with self.lock:
             if peer_addr in self.connections: return

        try:
            print(f"P2P ({self.node_id}): Attempting to connect to {peer_addr}...")
            conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            conn.settimeout(10.0) # Connection timeout
            conn.connect(peer_addr)
            conn.settimeout(60.0) # Timeout for subsequent operations
            print(f"P2P ({self.node_id}): Connected to {peer_addr}")

            with self.lock:
                 self.peers.add(peer_addr)
                 self.connections[peer_addr] = conn

            # Start handler thread for this new outgoing connection
            handler_thread = threading.Thread(target=self._handle_peer, args=(conn, peer_addr), daemon=True)
            handler_thread.start()

            # Request peer list from newly connected peer
            self.send_message(peer_addr, create_message(MessageType.GET_PEERS))

        except socket.timeout:
             print(f"P2P ({self.node_id}): Connection attempt to {peer_addr} timed out.")
             conn.close()
        except OSError as e:
            print(f"P2P Error ({self.node_id}): Could not connect to {peer_addr}. {e}")
            conn.close()
        except Exception as e:
            print(f"P2P Error ({self.node_id}): Unexpected error connecting to {peer_addr}: {e}")
            conn.close()


    def send_message(self, peer_addr: tuple[str, int], message_str: str) -> bool:
        """Sends a raw message string to a specific peer."""
        with self.lock:
            conn = self.connections.get(peer_addr)
            if conn:
                try:
                    conn.sendall(message_str.encode('utf-8'))
                    return True
                except OSError as e:
                    print(f"P2P Error ({self.node_id}): Failed to send message to {peer_addr}. {e}")
                    # Assume connection is broken, initiate removal
                    # Needs to be done carefully to avoid deadlocks if called from _handle_peer
                    # Schedule removal or handle outside the lock if necessary
                    # For simplicity here, we'll remove directly but it's risky
                    self._remove_peer(peer_addr, conn, acquire_lock=False) # Already holding lock
                    return False
                except Exception as e:
                    print(f"P2P Error ({self.node_id}): Unexpected error sending to {peer_addr}: {e}")
                    self._remove_peer(peer_addr, conn, acquire_lock=False)
                    return False
            else:
                 # print(f"P2P ({self.node_id}): Cannot send message, not connected to {peer_addr}")
                 return False


    def broadcast(self, message_str: str, exclude_peer: Optional[tuple[str, int]] = None):
        """Sends a raw message string to all connected peers (optionally excluding one)."""
        with self.lock:
            # Create a list of peers to send to avoid issues if connections change during iteration
            peers_to_send = list(self.connections.keys())

        # print(f"P2P ({self.node_id}): Broadcasting message to {len(peers_to_send)} peers.")
        for peer_addr in peers_to_send:
             if peer_addr != exclude_peer:
                 if not self.send_message(peer_addr, message_str):
                      # Send failed, peer likely removed, continue broadcasting to others
                      pass

    def get_peer_list(self) -> List[tuple[str, int]]:
         """Returns a list of currently known peer addresses."""
         with self.lock:
              # Return addresses, not sockets
              return list(self.peers)


    def _remove_peer(self, peer_addr: tuple[str, int], conn: Optional[socket.socket] = None, acquire_lock: bool = True):
         """Removes a peer and closes its connection."""
         # print(f"P2P ({self.node_id}): Removing peer {peer_addr}")
         if acquire_lock:
              self.lock.acquire()
         try:
              self.peers.discard(peer_addr)
              removed_conn = self.connections.pop(peer_addr, None)
              actual_conn_to_close = conn if conn else removed_conn

              if actual_conn_to_close:
                    try:
                         # actual_conn_to_close.shutdown(socket.SHUT_RDWR) # Can cause issues if already closed
                         actual_conn_to_close.close()
                    except OSError:
                         pass # Ignore errors if already closed
         finally:
              if acquire_lock:
                   self.lock.release()

    def _ping_peers_loop(self):
         """Periodically sends PING messages to check connections."""
         while self.running:
              time.sleep(30) # Ping every 30 seconds
              if not self.running: break
              # print(f"P2P ({self.node_id}): Pinging peers...")
              ping_message = create_message(MessageType.PING)
              with self.lock:
                   peers_to_ping = list(self.connections.keys())

              disconnected_peers = []
              for peer_addr in peers_to_ping:
                   if not self.send_message(peer_addr, ping_message):
                        # Send failed, assume disconnected
                        disconnected_peers.append(peer_addr)
                        print(f"P2P ({self.node_id}): Peer {peer_addr} failed ping, marked for removal.")

              # Removing peers requires the lock again, do it after iteration
              # for peer_addr in disconnected_peers:
              #      self._remove_peer(peer_addr) # _remove_peer handles locking# p2p