"""
UDP Debug Logger for tracing emulator execution
"""

import json
import queue
import socket
import sys
import threading
import time
from datetime import datetime
from typing import Dict


class UdpDebugLogger:
    """UDP debug logger for tracing emulator execution (async)"""

    def __init__(self, port: int = 64738, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self.sock = None
        self.enabled = False
        self.queue = queue.Queue(maxsize=1000000)  # Buffer up to 100k events (increased for 100% logging)
        self.worker_thread = None
        self.running = False

    def enable(self) -> None:
        """Enable UDP debug logging"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.enabled = True
            self.running = True
            # Start worker thread for async sending
            self.worker_thread = threading.Thread(target=self._worker, daemon=True)
            self.worker_thread.start()
        except Exception as e:
            print(f"Warning: Failed to create UDP socket for debug: {e}", file=sys.stderr)
            self.enabled = False

    def _worker(self) -> None:
        """Worker thread that sends UDP messages asynchronously"""
        while self.running:
            try:
                # Get message from queue with timeout
                message = self.queue.get(timeout=0.1)
                if message is None:  # Shutdown signal
                    # Before breaking, flush any remaining messages
                    while True:
                        try:
                            remaining = self.queue.get_nowait()
                            if remaining is None:
                                break
                            self.sock.sendto(remaining, (self.host, self.port))
                            self.queue.task_done()
                        except queue.Empty:
                            break
                    break
                self.sock.sendto(message, (self.host, self.port))
                self.queue.task_done()
            except queue.Empty:
                continue
            except Exception:
                pass  # Silently ignore UDP errors

    def send(self, event_type: str, data: Dict) -> None:
        """Queue debug event for async sending (non-blocking)"""
        if not self.enabled:
            return

        try:
            message = {
                'timestamp': datetime.now().isoformat(),
                'type': event_type,
                'data': data
            }
            json_msg = json.dumps(message)
            message_bytes = json_msg.encode('utf-8') + b"\n"

            # Try to put in queue (non-blocking if queue is full)
            try:
                self.queue.put_nowait(message_bytes)
            except queue.Full:
                # Queue is full, drop oldest message and add new one
                try:
                    self.queue.get_nowait()
                    self.queue.put_nowait(message_bytes)
                    # Debug: count dropped messages
                    if not hasattr(self, '_dropped_count'):
                        self._dropped_count = 0
                    self._dropped_count += 1
                    if self._dropped_count % 1000 == 0:
                        print(f"UDP debug: dropped {self._dropped_count} messages (queue full)")
                except queue.Empty:
                    pass
        except Exception:
            pass  # Silently ignore errors

    def close(self) -> None:
        """Close UDP socket and stop worker thread, flushing all pending messages"""
        self.running = False
        if self.queue:
            try:
                self.queue.put_nowait(None)  # Signal shutdown
            except queue.Full:
                pass
            # Flush all pending messages before closing
            # Wait for queue to empty (with timeout)
            timeout = 2.0  # Wait up to 2 seconds for messages to flush
            start_time = time.time()
            while not self.queue.empty() and (time.time() - start_time) < timeout:
                time.sleep(0.01)  # Small delay to let worker process messages
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2.0)  # Increased timeout for flushing
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self.enabled = False
