"""Simulator dua NPort/D701 untuk uji lokal tanpa perangkat keras."""

import math
import socket
import threading
import time


def serve(port: int, phase: float) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    print(f"Simulator menunggu di 127.0.0.1:{port}")
    while True:
        client, address = server.accept()
        print(f"Port {port} terhubung dari {address}")
        try:
            index = 0
            while True:
                x = 0.0076 + 0.001 * math.sin(index / 20 + phase)
                y = -0.0623 + 0.001 * math.cos(index / 25 + phase)
                temperature = 26.35 + 0.05 * math.sin(index / 100 + phase)
                client.sendall(f"$ {x: .4f}, {y: .4f},{temperature:.2f},N2343\r\n".encode("ascii"))
                index += 1
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            client.close()


if __name__ == "__main__":
    threading.Thread(target=serve, args=(4001, 0.0), daemon=True).start()
    threading.Thread(target=serve, args=(4002, 1.0), daemon=True).start()
    print("Atur aplikasi: A=127.0.0.1:4001 dan B=127.0.0.1:4002")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Simulator berhenti")
