"""Sample skill: prints the current time and hostname.

Usage: python3 skills/sample_skill.py
"""
import datetime
import socket

def main():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} @ {socket.gethostname()}")

if __name__ == "__main__":
    main()
