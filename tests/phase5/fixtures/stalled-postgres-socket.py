#!/usr/bin/env python3
import socket
import sys
import time

listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(sys.argv[1])
listener.listen(1)
connection, _ = listener.accept()
time.sleep(10)
connection.close()
listener.close()
